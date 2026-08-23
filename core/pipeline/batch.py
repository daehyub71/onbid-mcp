"""물건 배치 오케스트레이션 (F4.16·F4.17).

적재 계층(`core/store`)은 전부 "커밋은 호출자가 한다" 로 만들었다. **그 호출자가 여기고,
커밋 지점은 이 모듈 하나뿐이다.** 경계를 나누는 기준은 두 가지다.

- **메타는 즉시 커밋** — `onbid_batch_run` 행을 열자마자 커밋한다. 데이터와 같은
  트랜잭션에 묶으면 배치가 죽었을 때 메타까지 함께 사라져 F4.6이 무의미해진다.
  "시작했는데 끝나지 않은 배치" 가 남아야 다음 날 원인을 찾을 수 있다.
- **데이터는 한 트랜잭션** — 이력·적재·tombstone 을 한 번에 커밋한다. 나눠 커밋하면
  이력만 남고 적재가 실패한 상태가 생긴다.

**모드는 `ListingFilter` 하나에서 파생한다.** 모드를 따로 받으면 수집 조건과 tombstone
범위가 어긋날 수 있다 — 그 어긋남이 정확히 데이터를 뒤집는 방식이다.

**tombstone 을 판정해도 되는 조건은 셋이다.** 하나라도 어기면 멀쩡한 물건이 종료 처리된다.

1. 전량 모드일 것 (F4.2) — 증분의 "응답에 없음" 은 "변경 없음" 이다
2. 수집 범위와 판정 범위가 같을 것 (F4.13) — 필터에서 그대로 파생시킨다
3. **수집이 완주했을 것 (F4.17)** — 페이지 실패·상한·중단이 있으면 못 본 물건이지
   사라진 물건이 아니다
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import psycopg

from core.onbid.client import OnbidClient
from core.onbid.collector import CollectResult, ListingFilter, collect_listings
from core.pipeline import require_transactional
from core.store.batch_run import BatchCounts, finish_run, start_run
from core.store.history import upsert_with_history
from core.store.mapping import to_cltr_row
from core.store.tombstone import TombstoneScope, mark_tombstones

logger = logging.getLogger(__name__)

CollectFn = Callable[..., Awaitable[CollectResult]]
"""`collect_listings` 자리에 끼울 수 있는 수집 함수. 테스트가 대역을 넣는다."""

NOTE_LIMIT: Final = 500
"""메모 길이 상한. 요약이 길어져도 메타 행이 비대해지지 않게 한다."""


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """배치 결과.

    Attributes:
        run_id: 배치 식별자.
        mode: ``full`` 또는 ``delta``.
        collected: 수집 건수.
        skipped: 복합키가 없어 적재하지 못한 건수.
        upserted: 적재 건수.
        changes: 남긴 변경 이력 행 수.
        tombstoned: `종료추정` 으로 표시한 수. 판정을 건너뛰었으면 0.
        status: ``ok`` | ``partial``.
        resume_token: 중단 지점. 완주했으면 None.
    """

    run_id: int
    mode: str
    collected: int
    skipped: int
    upserted: int
    changes: int
    tombstoned: int
    status: str
    resume_token: str | None

    @property
    def is_complete(self) -> bool:
        """완주 여부."""
        return self.status == "ok"


def _resume_token(result: CollectResult) -> str | None:
    """중단 지점을 ``그룹:페이지`` 로 적는다. 다음 실행이 여기서 이어받는다 (N2.2)."""
    if result.stopped_at is None:
        return None
    group, page = result.stopped_at
    return f"{group}:{page}"


async def run_listing_batch(
    conn: psycopg.AsyncConnection[Any],
    client: OnbidClient,
    *,
    listing_filter: ListingFilter | None = None,
    collect: CollectFn = collect_listings,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    **collect_options: Any,
) -> BatchOutcome:
    """물건목록을 수집해 적재하고 배치 메타를 남긴다.

    전량·증분을 모두 처리한다 (F1.8). 어느 쪽인지는 `listing_filter` 가 결정한다.

    Args:
        conn: 열린 연결. **이 함수가 커밋한다** — 호출자는 트랜잭션을 관리하지 않는다.
        client: 온비드 클라이언트.
        listing_filter: 수집 조건. 생략하면 SPEC §2.1 기본 범위(서울·매각 전량).
        collect: 수집 함수. 기본은 `collect_listings`.
        now_fn: 배치 시작 시각. tombstone 판정 기준이 된다.
        **collect_options: 수집 함수에 그대로 넘길 옵션 (`page_size` 등).

    Returns:
        배치 결과.

    Raises:
        Exception: 적재 중 발생한 예외를 그대로 올린다. 올리기 전에 데이터를 되돌리고
            배치를 ``failed`` 로 닫는다.
    """
    require_transactional(conn)
    listing_filter = listing_filter or ListingFilter()
    mode = "delta" if listing_filter.is_incremental else "full"
    # 증분에서는 판정 자체를 하지 않으므로 범위를 만들지 않는다 (F4.2).
    scope = None if listing_filter.is_incremental else TombstoneScope.from_filter(listing_filter)
    started = now_fn()

    run_id = await start_run(conn, mode=mode)
    await conn.commit()  # 메타는 즉시 커밋 — 죽어도 흔적이 남아야 한다 (F4.16)

    try:
        result = await collect(client, listing_filter=listing_filter, **collect_options)
        outcome = await _load(conn, result, run_id=run_id, mode=mode,
                              scope=scope, started=started)
    except Exception as exc:
        await conn.rollback()
        await finish_run(conn, run_id, status="failed", note=str(exc)[:NOTE_LIMIT])
        await conn.commit()
        logger.exception("배치 실패: run_id=%d", run_id)
        raise

    await finish_run(
        conn, run_id,
        status=outcome.status,
        counts=BatchCounts(collected=outcome.collected, upserted=outcome.upserted,
                           tombstoned=outcome.tombstoned),
        resume_token=outcome.resume_token,
        note=result.summary()[:NOTE_LIMIT],
    )
    await conn.commit()

    logger.info(
        "배치 완료: run_id=%d %s %s · 적재 %d · 이력 %d · tombstone %d",
        run_id, mode, outcome.status, outcome.upserted, outcome.changes, outcome.tombstoned,
    )
    return outcome


async def _load(
    conn: psycopg.AsyncConnection[Any],
    result: CollectResult,
    *,
    run_id: int,
    mode: str,
    scope: TombstoneScope | None,
    started: datetime,
) -> BatchOutcome:
    """이력·적재·tombstone 을 **한 트랜잭션**으로 처리한다."""
    rows = [row for row in (to_cltr_row(entry) for entry in result.items) if row is not None]
    skipped = len(result.items) - len(rows)
    if skipped:
        logger.warning("복합키가 없어 %d건을 적재하지 못했다", skipped)

    loaded = await upsert_with_history(conn, rows)

    tombstoned = 0
    if scope is not None and result.is_complete:
        tombstoned = await mark_tombstones(conn, seen_before=started, scope=scope)
    elif scope is not None:
        # 못 본 것과 사라진 것을 구분할 수 없다 — 판정을 건너뛴다 (F4.17).
        logger.warning("수집이 완주하지 못해 tombstone 판정을 건너뛴다: %s", result.summary())

    await conn.commit()

    return BatchOutcome(
        run_id=run_id,
        mode=mode,
        collected=len(result.items),
        skipped=skipped,
        upserted=loaded.upserted,
        changes=loaded.changes,
        tombstoned=tombstoned,
        status="ok" if result.is_complete else "partial",
        resume_token=None if result.is_complete else _resume_token(result),
    )
