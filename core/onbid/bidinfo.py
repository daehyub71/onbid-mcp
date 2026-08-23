"""입찰정보 수집 — 회차별 유찰 이력 (F1.7·F1.11).

`getCltrBidInf2` 는 **물건당 1회 호출**이고 **일일 트래픽이 1,000건**이다.
서울 전량 6,910건을 매일 훑는 것은 불가능하므로 두 가지로 대응한다:

1. **대상 축소** — 두 부류를 제외한다.
   - **유찰 0회** — ``prcnBidClgList`` 가 비어 있다 (실측상 서울 물건의 68%).
   - **수의계약가능(``pvctTrgtYn=Y``)** — 수의계약은 입찰이 아니라 입찰정보가 없다.
     실측 18/18건이 ``03 NODATA_ERROR`` 였다.
   두 조건을 걸면 서울 대상은 2,267건 → **1,100여 건**으로 줄어 하루 예산 안에 들어온다.
2. **예산 롤링** — 하루 예산만큼만 처리하고 남은 대상은 다음 회차로 넘긴다.
   호출자가 `not_attempted` 를 이어받으면 3일이면 전량이 갱신된다.

``prcnBidClgList`` 는 **회차 이력을 통째로** 담고 있어, 유찰 물건은 첫 배치부터 전체 이력을
확보한다 — 자체 diff 누적을 기다릴 필요가 없다.
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Final

from core.onbid.client import (
    OnbidApiError,
    OnbidAuthError,
    OnbidClient,
    OnbidQuotaExceededError,
)
from core.onbid.collector import CollectedItem
from core.onbid.endpoints import ENDPOINTS
from core.onbid.parser import as_int, as_str, items_of, sub_list

logger = logging.getLogger(__name__)

HISTORY_FIELD: Final = "prcnBidClgList"
"""이전 회차 입찰 이력. 회차·개찰일시·결과·그 회차 최저입찰가를 담는다."""

DEFAULT_MIN_FAIL_COUNT: Final = 1
"""유찰 1회 이상만 대상으로 삼는다."""

PRIVATE_CONTRACT_GROUP: Final = "Y"
"""수의계약가능 그룹. 입찰정보 API가 항상 ``03`` 을 반환한다 (실측)."""

FAR_FUTURE: Final = "999999999999"
"""마감일이 없는 물건을 정렬 맨 뒤로 보내기 위한 값."""


@dataclass(frozen=True, slots=True)
class BidTarget:
    """입찰정보를 조회할 물건. 복합키가 필수다.

    Attributes:
        cltr_mng_no: 물건관리번호.
        pbct_cdtn_no: 공매조건번호.
    """

    cltr_mng_no: str
    pbct_cdtn_no: str


@dataclass(frozen=True, slots=True)
class BidDetail:
    """입찰정보 응답 한 건.

    Attributes:
        target: 조회 대상.
        raw: 응답 항목 원본. 손대지 않는다 (F1.3과 동일 원칙).
    """

    target: BidTarget
    raw: Mapping[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        """복합키."""
        return self.target.cltr_mng_no, self.target.pbct_cdtn_no

    @property
    def rounds(self) -> list[dict[str, Any]]:
        """회차별 이전 입찰 이력."""
        return sub_list(self.raw, HISTORY_FIELD)


@dataclass(frozen=True, slots=True)
class BidFailure:
    """조회에 실패한 대상.

    Attributes:
        target: 조회 대상.
        reason: 실패 사유.
    """

    target: BidTarget
    reason: str


@dataclass(slots=True)
class BidCollectResult:
    """입찰정보 수집 결과.

    Attributes:
        details: 수집한 입찰정보.
        failed: 재시도를 소진한 대상.
        not_attempted: 예산 소진·중단으로 처리하지 못한 대상. **다음 회차의 입력이 된다.**
        no_data_targets: 이력이 없어 ``03`` 으로 응답한 대상. **건수가 아니라 대상을 남긴다** —
            배치가 이들에게도 시도 시각을 찍어야 매일 다시 부르지 않는다 (F1.16).
        budget: 이번 실행의 호출 예산.
        aborted_reason: 전체 중단 사유. 쿼터·키 문제일 때만 채워진다.
        elapsed_sec: 소요 시간.
    """

    details: list[BidDetail] = field(default_factory=list)
    failed: list[BidFailure] = field(default_factory=list)
    not_attempted: list[BidTarget] = field(default_factory=list)
    no_data_targets: list[BidTarget] = field(default_factory=list)
    budget: int = 0
    aborted_reason: str | None = None
    elapsed_sec: float = 0.0

    @property
    def collected(self) -> int:
        """수집 건수."""
        return len(self.details)

    @property
    def no_data(self) -> int:
        """이력이 없어 ``03`` 으로 응답한 건수."""
        return len(self.no_data_targets)

    @property
    def rounds_collected(self) -> int:
        """수집한 회차 이력 행 수."""
        return sum(len(detail.rounds) for detail in self.details)

    @property
    def is_complete(self) -> bool:
        """대상을 남김없이 처리했는지 여부."""
        return not (self.failed or self.not_attempted or self.aborted_reason)

    def summary(self) -> str:
        """요약 문자열 — 건수·회차·실패·잔여·소요 시간."""
        parts = [
            f"{self.collected}건",
            f"회차 {self.rounds_collected}",
            f"이력없음 {self.no_data}",
            f"실패 {len(self.failed)}",
            f"잔여 {len(self.not_attempted)}",
            f"{self.elapsed_sec:.1f}초",
        ]
        if self.aborted_reason:
            parts.append(f"중단({self.aborted_reason})")
        return " · ".join(parts)


def select_bid_targets(
    items: Iterable[CollectedItem],
    *,
    min_fail_count: int = DEFAULT_MIN_FAIL_COUNT,
    exclude_private_contract: bool = True,
) -> list[BidTarget]:
    """수집한 물건에서 입찰정보 조회 대상을 골라 우선순위대로 정렬한다 (F1.11).

    두 부류를 제외한다 — 둘 다 호출해도 ``03`` 이 돌아와 쿼터만 소모한다.

    - 유찰 0회: 이력이 비어 있다.
    - 수의계약가능(``pvctTrgtYn=Y``): 입찰이 아니므로 입찰정보가 없다 (실측 18/18건 ``03``).

    우선순위는 **유찰 많은 순 → 마감 임박 순**이다.

    Args:
        items: 수집한 물건.
        min_fail_count: 최소 유찰횟수.
        exclude_private_contract: 수의계약가능 물건을 제외할지 여부.

    Returns:
        우선순위대로 정렬된 대상.
    """
    ranked: list[tuple[int, str, BidTarget]] = []
    for entry in items:
        mng = as_str(entry.raw.get("cltrMngNo"))
        cdtn = as_str(entry.raw.get("pbctCdtnNo"))
        if not (mng and cdtn):
            continue
        if exclude_private_contract and entry.group == PRIVATE_CONTRACT_GROUP:
            continue
        # 값이 없거나 숫자가 아니면 0으로 본다 — 넘겨짚어 호출하지 않는다.
        fail_count = as_int(entry.raw.get("usbdNft")) or 0
        if fail_count < min_fail_count:
            continue
        bid_end = as_str(entry.raw.get("cltrBidEndDt")) or FAR_FUTURE
        ranked.append((-fail_count, bid_end, BidTarget(mng, cdtn)))

    ranked.sort(key=lambda row: (row[0], row[1]))
    return [target for _, _, target in ranked]


async def collect_bid_details(
    client: OnbidClient,
    targets: Sequence[BidTarget],
    *,
    budget: int | None = None,
    on_item: Callable[[BidTarget, int], None] | None = None,
    time_fn: Callable[[], float] = monotonic,
) -> BidCollectResult:
    """대상들의 입찰정보를 예산 안에서 수집한다.

    Args:
        client: 온비드 클라이언트.
        targets: 우선순위대로 정렬된 대상.
        budget: 이번 실행의 호출 상한. 생략하면 엔드포인트의 일일 트래픽(1,000).
        on_item: 건마다 호출되는 진행 콜백 ``(대상, 회차 수)``.
        time_fn: 소요 시간 측정용 단조 시계.

    Returns:
        수집 결과. **실패해도 예외를 던지지 않는다** — 남은 대상을 `not_attempted` 로
        돌려주어 다음 회차가 이어받게 한다.
    """
    default_budget = ENDPOINTS["bid_detail"].daily_traffic or len(targets)
    limit = budget if budget is not None else default_budget
    result = BidCollectResult(budget=limit)
    started = time_fn()

    for index, target in enumerate(targets):
        if index >= limit:
            result.not_attempted.extend(targets[index:])
            break
        try:
            response = await client.call(
                "bid_detail", pageNo=1, numOfRows=10,
                cltrMngNo=target.cltr_mng_no, pbctCdtnNo=target.pbct_cdtn_no,
            )
        except (OnbidQuotaExceededError, OnbidAuthError) as exc:
            result.aborted_reason = f"[{exc.result_code}] {exc.result_msg}"
            result.not_attempted.extend(targets[index:])
            break
        except OnbidApiError as exc:
            result.failed.append(BidFailure(target, str(exc)))
            continue

        rows = items_of(response.payload)
        if not rows:
            # 이력이 없는 물건. 실패가 아니다 — 다만 '불렀다' 는 사실은 남긴다.
            result.no_data_targets.append(target)
            if on_item is not None:
                on_item(target, 0)
            continue

        detail = BidDetail(target=target, raw=rows[0])
        result.details.append(detail)
        if on_item is not None:
            on_item(target, len(detail.rounds))

    result.elapsed_sec = time_fn() - started
    _log_summary(result)
    return result


def _log_summary(result: BidCollectResult) -> None:
    """요약 로깅. 실패·중단이 있으면 성공처럼 읽히지 않게 레벨을 올린다."""
    message = f"입찰정보 수집: {result.summary()}"
    if result.aborted_reason:
        logger.error(message)
    elif result.failed:
        logger.warning(message)
    elif result.not_attempted:
        logger.info("%s (다음 회차로 이월)", message)
    else:
        logger.info(message)
