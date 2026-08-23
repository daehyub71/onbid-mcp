"""코드표 갱신 배치 (F6.12·F7.2·F7.1).

용도 3단 트리와 주소 조합을 DB 에 둔다. 조회마다 온비드에 물어보면 일일 트래픽을 검색에
쓰게 되고, 조회 지연이 외부 API 가용성에 묶인다. 코드표는 거의 변하지 않으므로 주간 갱신으로
충분하다.

**두 소스는 서로 독립이다.** 용도(`getOnbidUsgCodeInfo`)와 주소(`getAddrInfo`)는 다른
엔드포인트이고 다른 이유로 죽는다. 하나가 실패했다고 나머지까지 버리면 멀쩡한 갱신을 잃는다
— 각각 따로 잡아 각각 기록한다.

**빈 응답을 정상 갱신으로 기록하지 않는다.** 장애로 0건이 와도 upsert 는 아무 일도 하지
않아 조용히 성공처럼 보인다. `synced_at` 만 새것이 되어 "코드표는 최신" 이라 믿게 되는데,
실제로는 몇 주 전 값이다. 0건은 실패로 취급한다 — **다만 기존 행은 건드리지 않는다**
(지우고 다시 넣지 않는 이유와 같다).

**API 실패는 기록하고 넘어가지만 DB 실패는 전파한다.** 전자는 다음 주에 다시 받으면 되고,
후자는 적재 계층이 깨졌다는 뜻이다.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

import psycopg

from core.codes.address import AddressEntry, fetch_address_list
from core.codes.usage import UsageCode, fetch_usage_tree
from core.onbid.client import OnbidClient, OnbidError
from core.pipeline import require_transactional
from core.store.batch_run import BatchCounts, finish_run, start_run
from core.store.codes import upsert_address_map, upsert_usage_codes

logger = logging.getLogger(__name__)

FetchUsageFn = Callable[..., Awaitable[Sequence[UsageCode]]]
FetchAddressFn = Callable[..., Awaitable[Sequence[AddressEntry]]]

MODE: Final = "codes"
"""물건·회차 배치와 재개 지점을 공유하지 않도록 모드를 분리한다."""

DEFAULT_REGION: Final = "서울특별시"
"""수집 범위와 같은 지역만 노출한다 — 밖의 지역을 보여주면 0건 조회를 유도한다 (§2.1)."""

EMPTY_RESPONSE: Final = "빈 응답 — 장애로 보고 갱신을 인정하지 않는다"

NOTE_LIMIT: Final = 500


@dataclass(frozen=True, slots=True)
class CodeSourceResult:
    """코드 소스 하나의 결과.

    Attributes:
        name: 소스 이름 (``usage`` | ``address``).
        fetched: 받은 항목 수.
        loaded: 적재한 행 수.
        error: 실패 사유. 성공이면 None.
    """

    name: str
    fetched: int = 0
    loaded: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        """성공 여부."""
        return self.error is None


@dataclass(frozen=True, slots=True)
class CodeBatchOutcome:
    """코드표 배치 결과.

    Attributes:
        run_id: 배치 식별자.
        usage: 용도 트리 결과.
        address: 주소 조합 결과.
        status: ``ok`` | ``partial`` | ``failed``.
    """

    run_id: int
    usage: CodeSourceResult
    address: CodeSourceResult
    status: str

    @property
    def fetched(self) -> int:
        """받은 항목 수 합계."""
        return self.usage.fetched + self.address.fetched

    @property
    def loaded(self) -> int:
        """적재한 행 수 합계."""
        return self.usage.loaded + self.address.loaded


def _status(*sources: CodeSourceResult) -> str:
    """소스별 성패에서 배치 상태를 정한다."""
    succeeded = sum(1 for source in sources if source.ok)
    if succeeded == len(sources):
        return "ok"
    return "partial" if succeeded else "failed"


def _note(*sources: CodeSourceResult) -> str | None:
    """무엇이 왜 실패했는지 남긴다. 없으면 None."""
    reasons = [f"{s.name}: {s.error}" for s in sources if not s.ok]
    return " · ".join(reasons)[:NOTE_LIMIT] if reasons else None


async def _fetch(name: str, fetch: Callable[[], Awaitable[Sequence[Any]]]) -> tuple[
    CodeSourceResult, Sequence[Any]
]:
    """한 소스를 받아온다. **API 오류는 잡아서 결과로 돌려준다.**"""
    try:
        items = await fetch()
    except OnbidError as exc:
        logger.warning("코드표 %s 수집 실패: %s", name, exc)
        return CodeSourceResult(name=name, error=str(exc)), []

    if not items:
        logger.warning("코드표 %s: %s", name, EMPTY_RESPONSE)
        return CodeSourceResult(name=name, error=EMPTY_RESPONSE), []

    return CodeSourceResult(name=name, fetched=len(items)), items


async def run_code_batch(
    conn: psycopg.AsyncConnection[Any],
    client: OnbidClient,
    *,
    region_sd: str | None = DEFAULT_REGION,
    fetch_usage: FetchUsageFn = fetch_usage_tree,
    fetch_address: FetchAddressFn = fetch_address_list,
) -> CodeBatchOutcome:
    """용도 트리와 주소 조합을 갱신한다.

    Args:
        conn: 열린 연결. **이 함수가 커밋한다.**
        client: 온비드 클라이언트.
        region_sd: 주소를 받아올 시도. 수집 범위와 같게 둔다.
        fetch_usage: 용도 트리 수집 함수.
        fetch_address: 주소 조합 수집 함수.

    Returns:
        배치 결과. **API 실패로는 예외를 던지지 않는다** — 소스별 사유를 결과에 담는다.

    Raises:
        Exception: 적재 중 발생한 예외를 그대로 올린다. 올리기 전에 되돌리고 ``failed`` 로 닫는다.
    """
    require_transactional(conn)
    run_id = await start_run(conn, mode=MODE)
    await conn.commit()  # 메타는 즉시 커밋 (F4.16)

    usage, usage_items = await _fetch(
        "usage", lambda: fetch_usage(client))
    address, address_items = await _fetch(
        "address", lambda: fetch_address(client, sd_nm=region_sd))

    try:
        # 빈 목록이면 upsert 가 0을 돌려준다 — 기존 행은 그대로다.
        usage_loaded = await upsert_usage_codes(conn, usage_items)
        address_loaded = await upsert_address_map(conn, address_items)
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        await finish_run(conn, run_id, status="failed", note=str(exc)[:NOTE_LIMIT])
        await conn.commit()
        logger.exception("코드표 배치 실패: run_id=%d", run_id)
        raise

    outcome = CodeBatchOutcome(
        run_id=run_id,
        usage=CodeSourceResult(name=usage.name, fetched=usage.fetched,
                               loaded=usage_loaded, error=usage.error),
        address=CodeSourceResult(name=address.name, fetched=address.fetched,
                                 loaded=address_loaded, error=address.error),
        status=_status(usage, address),
    )

    await finish_run(
        conn, run_id,
        status=outcome.status,
        counts=BatchCounts(collected=outcome.fetched, upserted=outcome.loaded),
        note=_note(usage, address),
    )
    await conn.commit()

    logger.info(
        "코드표 배치 완료: run_id=%d %s · 용도 %d · 주소 %d",
        run_id, outcome.status, outcome.usage.loaded, outcome.address.loaded,
    )
    return outcome
