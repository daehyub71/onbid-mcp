"""물건목록 수집 — 페이지 순회와 그룹 순회 (F1.1·F1.3·F1.4·F1.5·F1.8·F1.9·F1.10).

온비드 물건목록의 두 가지 순회 축:

1. **페이지** — ``totalCount`` 로 종료를 판정한다. ``numOfRows=5000`` 이 동작하므로
   서울 전량이 그룹당 1~2페이지로 끝난다 (실측 6,910건 / 3회 호출).
2. **``pvctTrgtYn`` 그룹** — 수의계약가능여부는 **단일값 필수 파라미터**라
   ``Y``·``N`` 을 각각 돌아야 전량이 나온다 (F1.9).

수집기는 **응답 행을 손대지 않는다** (F1.3). 온비드는 ``pvctTrgtYn`` 을 응답에도 담아 보내므로,
우리가 어느 그룹 순회에서 얻었는지는 `CollectedItem.group` 에 따로 둔다 — 행에 써넣으면
``raw_payload`` 가 원본이 아니게 되고 API 값과의 불일치가 숨겨진다.

실패 처리는 두 갈래다 (F1.4):

- **페이지 단위 실패** — 재시도를 소진하면 그 페이지만 기록하고 다음 페이지를 계속한다.
- **쿼터 소진·키 문제** — 계속할 이유가 없다. 즉시 멈추고 재개 지점을 남긴다 (N2.2).
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Final

from core.codes.constants import DSPS_MTHOD_SALE, PRPT_DIV_ALL, PVCT_TRGT_VALUES
from core.onbid.client import (
    OnbidApiError,
    OnbidAuthError,
    OnbidClient,
    OnbidQuotaExceededError,
)
from core.onbid.parser import items_of, page_info

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE: Final = 5000
"""실측상 5000까지 동작한다. 상한은 발견하지 못했다."""

DEFAULT_MAX_PAGES: Final = 100
"""그룹당 페이지 상한. `totalCount` 가 실제와 어긋날 때의 방어선이다."""

DEFAULT_MAX_PAGE_FAILURES: Final = 2
"""그룹당 연속 실패 허용 횟수. 넘으면 그 그룹을 포기한다."""

ProgressCallback = Callable[[str, int, int], None]
"""(그룹, 페이지 번호, 그 페이지의 건수)."""


@dataclass(frozen=True, slots=True)
class CollectedItem:
    """수집한 물건 한 건.

    Attributes:
        raw: 온비드 응답 행. **손대지 않은 원본**이며 그대로 ``raw_payload`` 에 적재한다 (F1.3).
        group: 이 행을 얻은 ``pvctTrgtYn`` 순회 그룹.
    """

    raw: Mapping[str, Any]
    group: str

    @property
    def key(self) -> tuple[str, str]:
        """복합키 ``(cltrMngNo, pbctCdtnNo)``. DB 기본키와 같다."""
        return str(self.raw.get("cltrMngNo")), str(self.raw.get("pbctCdtnNo"))


@dataclass(frozen=True, slots=True)
class PageFailure:
    """재시도를 소진한 페이지.

    Attributes:
        group: ``pvctTrgtYn`` 그룹.
        page: 페이지 번호.
        reason: 실패 사유.
    """

    group: str
    page: int
    reason: str


@dataclass(slots=True)
class CollectResult:
    """수집 결과. F1.5 요약 로깅에 필요한 수치를 함께 담는다.

    Attributes:
        items: 수집한 물건.
        total_by_group: 그룹별 ``totalCount``.
        pages_fetched: 실제 호출한 페이지 수 (실패 포함).
        duplicates_dropped: 복합키 중복으로 버린 행 수.
        failed_pages: 재시도를 소진한 페이지 목록.
        truncated: 페이지 상한에 걸려 중단됐는지 여부.
        aborted_reason: 전체 중단 사유. 쿼터·키 문제일 때만 채워진다.
        stopped_at: 중단 지점 ``(그룹, 페이지)``. 다음 실행의 재개 기준 (N2.2).
        elapsed_sec: 소요 시간.
    """

    items: list[CollectedItem] = field(default_factory=list)
    total_by_group: dict[str, int] = field(default_factory=dict)
    pages_fetched: int = 0
    duplicates_dropped: int = 0
    failed_pages: list[PageFailure] = field(default_factory=list)
    truncated: bool = False
    aborted_reason: str | None = None
    stopped_at: tuple[str, int] | None = None
    elapsed_sec: float = 0.0

    @property
    def collected(self) -> int:
        """중복 제거 후 수집 건수."""
        return len(self.items)

    @property
    def is_complete(self) -> bool:
        """전량을 받았는지 여부. 실패·중단·상한 도달이 하나라도 있으면 거짓이다."""
        return not (self.failed_pages or self.truncated or self.aborted_reason)

    def summary(self) -> str:
        """F1.5 요약 문자열 — 수집 건수·페이지 수·실패 수·소요 시간."""
        parts = [
            f"{self.collected}건",
            f"페이지 {self.pages_fetched}",
            f"실패 {len(self.failed_pages)}",
            f"중복 {self.duplicates_dropped}",
            f"{self.elapsed_sec:.1f}초",
            f"그룹별 {self.total_by_group}",
        ]
        if self.truncated:
            parts.append("상한도달")
        if self.aborted_reason:
            parts.append(f"중단({self.aborted_reason}) @ {self.stopped_at}")
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class ListingFilter:
    """물건목록 수집 조건. 기본값이 SPEC §2.1 수집 범위다.

    Attributes:
        region_sd: 시도명. **온비드는 법정동코드를 쓰지 않으므로 문자열이다** (F1.10).
        region_sgg: 시군구명. 지정하면 범위를 좁힌다.
        dsps_mthod_cd: 처분방식. 기본 매각 — 임대는 최저가율 가설이 성립하지 않는다.
        prpt_div_cds: 재산유형코드. 필수 파라미터라 전 유형을 쉼표로 나열한다.
        modified_from: 최종수정일 시작(``yyyyMMdd``). 지정하면 증분 모드다 (F1.8).
        modified_to: 최종수정일 종료.
        fail_count_min: 최소 유찰횟수. 입찰정보 대상 선별에 쓴다 (F1.11).
    """

    region_sd: str = "서울특별시"
    region_sgg: str | None = None
    dsps_mthod_cd: str = DSPS_MTHOD_SALE
    prpt_div_cds: tuple[str, ...] = PRPT_DIV_ALL
    modified_from: str | None = None
    modified_to: str | None = None
    fail_count_min: int | None = None

    @property
    def is_incremental(self) -> bool:
        """증분 모드인지 여부. 전량 모드에서만 tombstone 을 판정한다 (F4.2)."""
        return self.modified_from is not None or self.modified_to is not None

    def to_params(self) -> dict[str, Any]:
        """온비드 요청 파라미터로 변환한다. ``None`` 항목은 넣지 않는다."""
        params: dict[str, Any] = {
            "lctnSdnm": self.region_sd,
            "dspsMthodCd": self.dsps_mthod_cd,
            "prptDivCd": ",".join(self.prpt_div_cds),
        }
        optional = {
            "lctnSggnm": self.region_sgg,
            "mdfcnYmdStart": self.modified_from,
            "mdfcnYmdEnd": self.modified_to,
            "usbdNftStart": self.fail_count_min,
        }
        params.update({k: v for k, v in optional.items() if v is not None})
        return params


class _AbortedError(Exception):
    """쿼터·키 문제로 전체 수집을 중단한다."""

    def __init__(self, reason: str, group: str, page: int) -> None:
        self.reason = reason
        self.stopped_at = (group, page)
        super().__init__(reason)


async def collect_listings(
    client: OnbidClient,
    *,
    listing_filter: ListingFilter | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_page_failures: int = DEFAULT_MAX_PAGE_FAILURES,
    on_page: ProgressCallback | None = None,
    time_fn: Callable[[], float] = monotonic,
) -> CollectResult:
    """물건목록을 전량 수집한다.

    Args:
        client: 온비드 클라이언트.
        listing_filter: 수집 조건. 생략하면 SPEC §2.1 기본 범위.
        page_size: 페이지 크기.
        max_pages: 그룹당 페이지 상한. 넘으면 `truncated` 를 세운다.
        max_page_failures: 그룹당 연속 실패 허용 횟수.
        on_page: 페이지마다 호출되는 진행 콜백.
        time_fn: 소요 시간 측정용 단조 시계.

    Returns:
        수집 결과. **실패해도 예외를 던지지 않는다** — 이미 받은 데이터를 버리지 않기 위해
        결과에 실패 내역을 담아 돌려준다.
    """
    listing_filter = listing_filter or ListingFilter()
    base_params = listing_filter.to_params()
    result = CollectResult()
    seen: set[tuple[str, str]] = set()
    started = time_fn()

    try:
        for group in PVCT_TRGT_VALUES:
            result.total_by_group[group] = await _collect_group(
                client, group, base_params, page_size, max_pages,
                max_page_failures, on_page, result, seen,
            )
    except _AbortedError as abort:
        result.aborted_reason = abort.reason
        result.stopped_at = abort.stopped_at

    result.elapsed_sec = time_fn() - started
    _log_summary(result)
    return result


def _log_summary(result: CollectResult) -> None:
    """F1.5 요약 로깅. 실패가 있으면 성공처럼 읽히지 않게 경고로 올린다."""
    message = f"물건목록 수집: {result.summary()}"
    if result.aborted_reason:
        logger.error(message)
    elif result.failed_pages or result.truncated:
        logger.warning(message)
    else:
        logger.info(message)


async def _collect_group(
    client: OnbidClient,
    group: str,
    base_params: dict[str, Any],
    page_size: int,
    max_pages: int,
    max_page_failures: int,
    on_page: ProgressCallback | None,
    result: CollectResult,
    seen: set[tuple[str, str]],
) -> int:
    """한 `pvctTrgtYn` 그룹의 전 페이지를 돈다.

    Returns:
        그룹의 `totalCount`. 0건 응답이면 0.

    Raises:
        _AbortedError: 쿼터 소진·키 문제. 전체 수집을 멈춰야 한다.
    """
    total = 0
    consecutive_failures = 0

    for page in range(1, max_pages + 1):
        result.pages_fetched += 1
        try:
            response = await client.call(
                "realestate_list",
                pageNo=page, numOfRows=page_size, pvctTrgtYn=group, **base_params,
            )
        except (OnbidQuotaExceededError, OnbidAuthError) as exc:
            raise _AbortedError(f"[{exc.result_code}] {exc.result_msg}", group, page) from exc
        except OnbidApiError as exc:
            # 재시도를 소진한 페이지. 기록하고 다음 페이지를 계속한다 (F1.4).
            result.failed_pages.append(PageFailure(group, page, str(exc)))
            consecutive_failures += 1
            if consecutive_failures >= max_page_failures:
                logger.warning(
                    "pvctTrgtYn=%s: 연속 %d회 실패로 그룹을 포기한다", group, consecutive_failures
                )
                return total
            # 더 받을 페이지가 있다고 믿을 근거가 없으면 멈춘다.
            if not total or page * page_size >= total:
                return total
            continue

        consecutive_failures = 0
        rows = items_of(response.payload)
        info = page_info(response.payload)
        total = max(total, info.total_count)

        if on_page is not None:
            on_page(group, page, len(rows))

        for row in rows:
            item = CollectedItem(raw=row, group=group)
            if item.key in seen:
                result.duplicates_dropped += 1
                continue
            seen.add(item.key)
            result.items.append(item)

        # 빈 페이지는 종료 신호다. totalCount 가 실제보다 커도 여기서 멈춘다.
        if not rows or not info.has_more:
            return total

    result.truncated = True
    logger.warning("pvctTrgtYn=%s: 페이지 상한 %d 도달 — 전량을 받지 못했다", group, max_pages)
    return total
