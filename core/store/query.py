"""조회 쿼리 빌더 (F5.1·F6.7).

MCP 툴과 조회 API 가 함께 쓰는 필터를 SQL 로 바꾼다. **순수 함수다** — DB 없이 조건 조합을
검증할 수 있어야 조합 폭발을 감당할 수 있다.

세 가지를 지킨다.

- **값은 전부 파라미터 바인딩.** 문자열로 이어 붙이면 주입 통로가 된다.
- **빈 필터는 조건을 만들지 않는다.** 조건 하나를 잘못 붙이면 전체 조회가 조용히 0건이 된다.
- **정렬은 결정적이어야 한다.** 마감일만으로 정렬하면 동률에서 순서가 흔들려 커서
  페이지네이션이 행을 건너뛰거나 중복시킨다 (F6.8).

**용도 확장은 여기서 하지 않는다.** 중분류로 그냥 걸면 0건이므로(실측 3,506건 대 0건, F6.12)
호출자가 `UsageIndex` 로 소분류까지 펼친 id 목록을 넘긴다 — 코드표는 DB 에 있고 이 모듈은
SQL 만 만든다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from core.normalizer.status import AuctionStatus

TABLE: Final = "onbid_cltr"

#: 조회에 노출하는 컬럼. `raw_payload` 는 크고 조회에 쓰이지 않아 뺀다.
COLUMNS: Final = (
    "cltr_mng_no", "pbct_cdtn_no", "cltr_nm",
    "jibun_addr", "road_addr", "sd_nm", "sgg_nm", "emd_nm", "lat", "lng",
    "geocode_status", "geocode_level",
    "prpt_div_cd", "prpt_div_nm",
    "usg_lcls_nm", "usg_mcls_nm", "usg_scls_nm",
    "appraisal_amt", "min_bid_amt", "min_bid_amt_text", "min_bid_rate",
    "fail_cnt", "bid_start", "bid_end", "bid_date_tbd",
    "status", "pbct_stat_nm", "pvct_trgt_yn", "share_yn",
    "land_sqms", "bld_sqms", "org_nm", "onbid_url", "synced_at",
)

VALID_STATUSES: Final = frozenset(status.value for status in AuctionStatus)

#: 동률에서 순서가 흔들리면 커서가 행을 건너뛴다 — 복합키까지 넣어 결정적으로 만든다.
ORDER_BY: Final = "bid_end asc nulls last, cltr_mng_no asc, pbct_cdtn_no asc"


@dataclass(frozen=True, slots=True)
class ListingQuery:
    """물건 조회 조건. 모든 항목이 선택이며, 없으면 좁히지 않는다.

    Attributes:
        sd_nm: 시도명.
        sgg_nm: 시군구명.
        emd_nm: 읍면동명.
        usage_ids: 용도 카테고리 id. **호출자가 소분류까지 펼쳐서** 넘긴다 (F6.12).
        prpt_div_cds: 재산유형코드.
        pvct_trgt: 수의계약 가능 여부. `None` 은 무관, `False` 는 **불가만**이다.
        min_bid_amt_min: 최저입찰가 하한(원).
        min_bid_amt_max: 최저입찰가 상한(원).
        min_bid_rate_min: 최저가율 하한. 1.0 을 넘을 수 있다 (F4.5).
        min_bid_rate_max: 최저가율 상한.
        fail_cnt_min: 유찰횟수 하한.
        fail_cnt_max: 유찰횟수 상한.
        bid_end_from: 마감일 시작. **일정 미정 행은 `bid_end` 가 null 이라 빠진다.**
        bid_end_to: 마감일 종료.
        statuses: 상태. 허용값을 검증한다.
    """

    sd_nm: str | None = None
    sgg_nm: str | None = None
    emd_nm: str | None = None
    usage_ids: tuple[str, ...] = ()
    prpt_div_cds: tuple[str, ...] = ()
    pvct_trgt: bool | None = None
    min_bid_amt_min: int | None = None
    min_bid_amt_max: int | None = None
    min_bid_rate_min: float | None = None
    min_bid_rate_max: float | None = None
    fail_cnt_min: int | None = None
    fail_cnt_max: int | None = None
    bid_end_from: datetime | None = None
    bid_end_to: datetime | None = None
    statuses: tuple[str, ...] = ()


def _validate(query: ListingQuery) -> None:
    """조용히 0건을 만드는 입력을 미리 거른다.

    Raises:
        ValueError: 알 수 없는 상태값이 섞였을 때.
    """
    unknown = [s for s in query.statuses if s not in VALID_STATUSES]
    if unknown:
        raise ValueError(
            f"알 수 없는 상태: {unknown} (가능: {sorted(VALID_STATUSES)})"
        )


def build_where(query: ListingQuery) -> tuple[list[str], dict[str, Any]]:
    """조건절 목록과 파라미터를 만든다.

    Args:
        query: 조회 조건.

    Returns:
        ``(조건절 목록, 파라미터)``. 조건이 없으면 빈 목록이다.

    Raises:
        ValueError: 허용되지 않은 값이 섞였을 때.
    """
    _validate(query)

    clauses: list[str] = []
    params: dict[str, Any] = {}

    def equals(column: str, value: Any) -> None:
        if value is None:
            return
        clauses.append(f"{column} = %({column})s")
        params[column] = value

    def any_of(column: str, name: str, values: Sequence[str]) -> None:
        if not values:
            return
        clauses.append(f"{column} = any(%({name})s)")
        params[name] = list(values)

    def at_least(column: str, name: str, value: Any) -> None:
        if value is None:
            return
        clauses.append(f"{column} >= %({name})s")
        params[name] = value

    def at_most(column: str, name: str, value: Any) -> None:
        if value is None:
            return
        clauses.append(f"{column} <= %({name})s")
        params[name] = value

    equals("sd_nm", query.sd_nm)
    equals("sgg_nm", query.sgg_nm)
    equals("emd_nm", query.emd_nm)

    # 대·중·소 어느 단계에 맞아도 통과시킨다. 호출자가 이미 펼친 id 목록이다 (F6.12).
    if query.usage_ids:
        clauses.append(
            "(usg_lcls_id = any(%(usage_ids)s)"
            " or usg_mcls_id = any(%(usage_ids)s)"
            " or usg_scls_id = any(%(usage_ids)s))"
        )
        params["usage_ids"] = list(query.usage_ids)

    any_of("prpt_div_cd", "prpt_div_cds", query.prpt_div_cds)
    any_of("status", "statuses", query.statuses)

    # None(무관) 과 False(불가만) 를 구분해야 한다 — `equals` 가 None 만 건너뛴다.
    if query.pvct_trgt is not None:
        clauses.append("pvct_trgt_yn = %(pvct_trgt)s")
        params["pvct_trgt"] = query.pvct_trgt

    at_least("min_bid_amt", "min_bid_amt_min", query.min_bid_amt_min)
    at_most("min_bid_amt", "min_bid_amt_max", query.min_bid_amt_max)
    at_least("min_bid_rate", "min_bid_rate_min", query.min_bid_rate_min)
    at_most("min_bid_rate", "min_bid_rate_max", query.min_bid_rate_max)
    at_least("fail_cnt", "fail_cnt_min", query.fail_cnt_min)
    at_most("fail_cnt", "fail_cnt_max", query.fail_cnt_max)
    at_least("bid_end", "bid_end_from", query.bid_end_from)
    at_most("bid_end", "bid_end_to", query.bid_end_to)

    return clauses, params


def build_select(
    query: ListingQuery,
    *,
    limit: int,
    columns: Sequence[str] = COLUMNS,
) -> tuple[str, Mapping[str, Any]]:
    """조회 SQL 과 파라미터를 만든다.

    Args:
        query: 조회 조건.
        limit: 최대 건수. **바인딩**한다.
        columns: 선택할 컬럼.

    Returns:
        ``(sql, params)``.

    Raises:
        ValueError: 허용되지 않은 값이 섞였을 때.
    """
    clauses, params = build_where(query)
    params["limit"] = limit

    where = f"\n where {' and '.join(clauses)}" if clauses else ""
    return (
        f"select {', '.join(columns)}\n  from {TABLE}{where}\n"
        f" order by {ORDER_BY}\n limit %(limit)s",
        params,
    )
