"""회차 이력 배치 (F1.7·F1.11·F1.16).

입찰정보 API 는 **일일 트래픽 1,000건**인데 대상은 1,100여 건이다. 한 번에 다 돌 수 없다.
그래서 **오래 안 본 것부터** 예산만큼 처리하고, 며칠에 걸쳐 한 바퀴를 돈다.

**시도 시각을 남기는 것이 이 배치의 핵심이다** (F1.16). 남기지 않으면 매일 같은 앞머리
1,000건만 호출하고 뒤쪽은 영원히 갱신되지 않는다. 성공한 것만 남겨도 안 된다 — 이력이 없어
``03`` 이 돌아온 물건을 매일 다시 부르게 되고, 실패를 빼면 고장난 한 건이 매일 예산을 선점한다.
**부르지 못한 것(예산 소진·중단)만 남기지 않는다.**

재개 토큰은 쓰지 않는다. 대상이 *집합*이라 스칼라로 표현할 수 없고, 시도 시각순 정렬이
상태 없이 같은 일을 한다 — 배치가 죽어도 다음 실행이 알아서 이어간다.

커밋 경계는 `core.pipeline.batch` 와 같다 (F4.16): 메타는 즉시, 데이터는 한 트랜잭션.
**회차 적재와 시도 기록은 같은 트랜잭션이다** — 적재가 실패했는데 시도만 남으면 그 물건은
이력 없이 순번만 뒤로 밀린다.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import psycopg

from core.onbid.bidinfo import BidCollectResult, collect_bid_details
from core.onbid.client import OnbidClient
from core.onbid.endpoints import ENDPOINTS
from core.pipeline import require_transactional
from core.store.batch_run import BatchCounts, finish_run, start_run
from core.store.bid_round import (
    mark_round_attempts,
    select_round_targets,
    to_round_rows,
    upsert_bid_rounds,
)

logger = logging.getLogger(__name__)

CollectFn = Callable[..., Awaitable[BidCollectResult]]
"""`collect_bid_details` 자리에 끼울 수 있는 수집 함수."""

MODE: Final = "rounds"
"""물건 배치(`full`/`delta`)와 재개 지점을 공유하지 않도록 모드를 분리한다."""

NOTE_LIMIT: Final = 500


@dataclass(frozen=True, slots=True)
class RoundBatchOutcome:
    """회차 배치 결과.

    Attributes:
        run_id: 배치 식별자.
        targets: 이번 회차에 고른 대상 수.
        collected: 이력을 받은 물건 수.
        rounds: 적재한 회차 행 수.
        no_data: 이력이 없어 ``03`` 이 온 수.
        failed: 재시도를 소진한 수.
        carried_over: 예산 소진·중단으로 부르지 못해 다음 회차로 넘긴 수.
        attempted: 시도 시각을 남긴 수.
        status: ``ok`` | ``partial``.
    """

    run_id: int
    targets: int
    collected: int
    rounds: int
    no_data: int
    failed: int
    carried_over: int
    attempted: int
    status: str


def _default_budget() -> int:
    """엔드포인트의 일일 트래픽. 명시하지 않으면 이 값을 쓴다."""
    return ENDPOINTS["bid_detail"].daily_traffic or 0


async def run_round_batch(
    conn: psycopg.AsyncConnection[Any],
    client: OnbidClient,
    *,
    budget: int | None = None,
    collect: CollectFn = collect_bid_details,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    min_fail_count: int = 1,
) -> RoundBatchOutcome:
    """대상을 예산만큼 골라 회차 이력을 갱신한다.

    Args:
        conn: 열린 연결. **이 함수가 커밋한다.**
        client: 온비드 클라이언트.
        budget: 이번 회차의 호출 상한. 생략하면 엔드포인트의 일일 트래픽(1,000).
        collect: 입찰정보 수집 함수. 기본은 `collect_bid_details`.
        now_fn: 시도 시각. **배치 전체가 같은 값을 쓴다** — 호출마다 다르면 같은 배치 안에서
            순번이 갈린다.
        min_fail_count: 최소 유찰횟수. 기본 1 (유찰 0회는 이력이 없다).

    Returns:
        배치 결과.

    Raises:
        Exception: 적재 중 발생한 예외를 그대로 올린다. 올리기 전에 되돌리고 ``failed`` 로 닫는다.
    """
    require_transactional(conn)
    limit = budget if budget is not None else _default_budget()
    attempted_at = now_fn()

    run_id = await start_run(conn, mode=MODE)
    await conn.commit()  # 메타는 즉시 커밋 (F4.16)

    try:
        targets = await select_round_targets(conn, limit=limit, min_fail_count=min_fail_count)
        if not targets:
            # 빈 호출로 쿼터를 태우지 않는다.
            result = BidCollectResult(budget=limit)
        else:
            result = await collect(client, targets, budget=limit)

        outcome = await _load(conn, result, run_id=run_id, targets=len(targets),
                              attempted_at=attempted_at)
    except Exception as exc:
        await conn.rollback()
        await finish_run(conn, run_id, status="failed", note=str(exc)[:NOTE_LIMIT])
        await conn.commit()
        logger.exception("회차 배치 실패: run_id=%d", run_id)
        raise

    await finish_run(
        conn, run_id,
        status=outcome.status,
        counts=BatchCounts(collected=outcome.collected, upserted=outcome.rounds),
        note=result.summary()[:NOTE_LIMIT],
    )
    await conn.commit()

    logger.info(
        "회차 배치 완료: run_id=%d %s · 대상 %d · 회차 %d · 이월 %d",
        run_id, outcome.status, outcome.targets, outcome.rounds, outcome.carried_over,
    )
    return outcome


async def _load(
    conn: psycopg.AsyncConnection[Any],
    result: BidCollectResult,
    *,
    run_id: int,
    targets: int,
    attempted_at: datetime,
) -> RoundBatchOutcome:
    """회차 적재와 시도 기록을 **한 트랜잭션**으로 처리한다."""
    rows = [row for detail in result.details for row in to_round_rows(detail)]
    loaded = await upsert_bid_rounds(conn, rows)

    # 부르지 못한 것만 뺀다 — 성공·이력없음·실패는 모두 시도다 (F1.16).
    skipped = set(result.not_attempted)
    attempted = [
        target
        for target in (
            [detail.target for detail in result.details]
            + [failure.target for failure in result.failed]
            + list(result.no_data_targets)
        )
        if target not in skipped
    ]
    marked = await mark_round_attempts(conn, attempted, attempted_at=attempted_at)

    await conn.commit()

    return RoundBatchOutcome(
        run_id=run_id,
        targets=targets,
        collected=result.collected,
        rounds=loaded,
        no_data=result.no_data,
        failed=len(result.failed),
        carried_over=len(result.not_attempted),
        attempted=marked,
        status="ok" if result.is_complete else "partial",
    )
