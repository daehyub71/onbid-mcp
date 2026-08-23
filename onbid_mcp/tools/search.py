"""`search_auction_items` — 조건 조회 툴 (SPEC §8.1·F6.5~F6.12).

**툴 계층이 하는 일은 조회가 아니라 통역이다.** 사람이 "강남구 아파트" 라고 말하면 코드로
바꾸고, 없는 이름이면 후보를 돌려주고, 0건이면 빈 배열이 아니라 `no_result` 로 알린다.

네 가지가 이 층의 책임이다.

- **용도 중분류 확장** (F6.12) — 물건에는 소분류 코드만 들어 있어(실측 35종) 확장하지 않으면
  `주거용건물` 검색이 **0건**이 된다(확장 시 3,506건). 쿼리 빌더는 펼쳐진 id 만 받는다.
- **명칭 매칭 실패 시 후보 제공** (F6.7) — 빈 결과보다 "무엇을 고를 수 있는지" 가 유용하다.
- **0건은 `no_result`** (§8.7) — 빈 배열은 '없다' 와 '실패했다' 를 구분하지 못한다.
- **`pvct_trgt` 로 두 모집단을 가른다** (§8.1) — 수의계약 물건은 전량이 유찰 경험자라
  "유찰 많은 물건" 을 찾으면 상당수가 이미 입찰 대상이 아니다.
"""

import dataclasses
import logging
from datetime import date, datetime
from typing import Any, Final

import psycopg

from core.codes.index import UsageIndex
from core.codes.resolve import RegionIndex, resolve_property_type, resolve_usage
from core.store.codes import load_address_entries, load_usage_codes
from core.store.query import (
    COLUMNS,
    ListingQuery,
    Sort,
    build_select,
    build_where,
    encode_cursor,
)
from onbid_mcp.common import ToolError, ok_response

logger = logging.getLogger(__name__)

DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 50

#: 정렬 화이트리스트 (§8.1). 점수·추천 항목은 만들지 않는다 (§2.4).
SORTS: Final = {
    "bid_end_asc": Sort("bid_end"),
    "bid_end_desc": Sort("bid_end", descending=True),
    "min_bid_amt_asc": Sort("min_bid_amt"),
    "min_bid_amt_desc": Sort("min_bid_amt", descending=True),
    "fail_cnt_desc": Sort("fail_cnt", descending=True),
}
DEFAULT_SORT_KEY: Final = "bid_end_asc"

#: 수의계약 여부. **성격이 다른 두 모집단**이라 기본은 전체다 (§8.1).
PVCT_CHOICES: Final = {"입찰": False, "수의계약": True, "전체": None}

#: 상태. 기본은 진행 — 끝난 물건이 섞이면 "지금 살 수 있는 것" 질문에 잘못 답한다.
STATUS_CHOICES: Final = {"진행": ("진행",), "종료추정": ("종료추정",), "전체": ()}
DEFAULT_STATUS: Final = "진행"


def _choice(value: str | None, choices: dict[str, Any], label: str, default: str) -> Any:
    """열거값을 고른다.

    Raises:
        ToolError: 목록에 없을 때. 후보를 함께 준다 (F6.7).
    """
    picked = value or default
    if picked not in choices:
        raise ToolError("invalid_param", f"{label} 값이 올바르지 않습니다: {picked!r}",
                        candidates=list(choices))
    return choices[picked]


def _parse_date(value: str | None, label: str) -> datetime | None:
    """`YYYY-MM-DD` 를 읽는다.

    Raises:
        ToolError: 형식이 틀렸을 때.
    """
    if not value:
        return None
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    except ValueError as exc:
        raise ToolError("invalid_param",
                        f"{label} 은 YYYY-MM-DD 형식이어야 합니다: {value!r}") from exc


async def _region_filter(
    conn: psycopg.AsyncConnection[Any], region: str | None
) -> tuple[str | None, str | None]:
    """지역명을 시군구·읍면동으로 옮긴다.

    온비드는 지역 코드를 쓰지 않으므로 **명칭 전용**이다 (F6.6).

    Raises:
        ToolError: 매칭 실패 시 후보와 함께.
    """
    if not region:
        return None, None

    index = RegionIndex(await load_address_entries(conn))
    resolution = index.resolve(region)
    if not resolution.is_resolved:
        raise ToolError(
            "invalid_param", f"지역을 찾을 수 없습니다: {region!r}",
            candidates=[str(c) for c in resolution.candidates] or list(index.districts[:10]),
        )

    # `matched` 는 튜플이다 — 모호한 경우를 담기 위해서다. `is_resolved` 면 하나뿐이다.
    matched = resolution.matched[0]
    return matched.sgg_nm, matched.emd_nm


async def _usage_ids(
    conn: psycopg.AsyncConnection[Any], usage: str | None
) -> tuple[str, ...]:
    """용도명·코드를 **소분류까지 펼친** id 목록으로 옮긴다 (F6.12).

    Raises:
        ToolError: 매칭 실패 시 후보와 함께.
    """
    if not usage:
        return ()

    index = UsageIndex(await load_usage_codes(conn))
    resolution = resolve_usage(usage, index, expand=True)
    # `is_resolved`(하나로 좁혀짐)를 쓰면 안 된다 — 확장은 **일부러 여러 개**를 돌려준다.
    if not resolution.matched:
        raise ToolError(
            "invalid_param", f"용도를 찾을 수 없습니다: {usage!r}",
            candidates=[node.ctgr_nm for node in resolution.candidates][:10]
            or [node.ctgr_nm for node in index.at_depth(1)],
        )

    # `expand=True` 면 하위 소분류까지 `matched` 에 담겨 온다 (F6.12).
    return tuple(node.ctgr_id for node in resolution.matched)


def _property_types(prpt_div: str | None) -> tuple[str, ...]:
    """재산유형명·코드를 코드 목록으로 옮긴다. 쉼표 복수 지정을 받는다 (F6.6).

    Raises:
        ToolError: 매칭 실패 시 후보와 함께.
    """
    if not prpt_div:
        return ()

    resolution = resolve_property_type(prpt_div)
    # 쉼표 복수 지정을 받으므로 여러 개가 정상이다 (F6.6).
    if not resolution.matched:
        raise ToolError(
            "invalid_param", f"재산유형을 찾을 수 없습니다: {prpt_div!r}",
            candidates=[str(c) for c in resolution.candidates][:10],
        )

    return tuple(node.code for node in resolution.matched)


async def _total_count(
    conn: psycopg.AsyncConnection[Any], query: ListingQuery
) -> int:
    """조건에 맞는 전체 건수. 몇 건 중 몇 건인지 알아야 '전부' 라고 말하지 않는다."""
    clauses, params = build_where(query)
    where = f" where {' and '.join(clauses)}" if clauses else ""
    async with conn.cursor() as cur:
        await cur.execute(f"select count(*) from onbid_cltr{where}", params)
        found = await cur.fetchone()
    return int(found[0]) if found else 0


async def _synced_at(conn: psycopg.AsyncConnection[Any]) -> datetime | None:
    async with conn.cursor() as cur:
        await cur.execute("select max(synced_at) from onbid_cltr")
        found = await cur.fetchone()
    return found[0] if found else None


async def search_auction_items(  # noqa: PLR0913 — 필터가 곧 툴 계약이다
    conn: psycopg.AsyncConnection[Any],
    *,
    region: str | None = None,
    usage: str | None = None,
    prpt_div: str | None = None,
    pvct_trgt: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_rate: float | None = None,
    max_rate: float | None = None,
    min_fail_cnt: int | None = None,
    bid_end_after: str | None = None,
    bid_end_before: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """조건에 맞는 공매 물건을 조회한다 (§8.1).

    Args:
        conn: 열린 연결.
        region: 시군구명 또는 읍면동명. **명칭 전용**이다 (F6.6).
        usage: 용도명 또는 코드. 중분류면 소분류까지 확장한다 (F6.12).
        prpt_div: 재산유형명 또는 코드.
        pvct_trgt: `입찰` | `수의계약` | `전체`(기본).
        min_price: 최저입찰가 하한(원).
        max_price: 최저입찰가 상한(원).
        min_rate: 최저가율 하한. **상한은 1.0 이 아니다** (실측 최대 1.502).
        max_rate: 최저가율 상한.
        min_fail_cnt: 최소 유찰횟수.
        bid_end_after: 마감일 시작 (`YYYY-MM-DD`).
        bid_end_before: 마감일 종료.
        status: `진행`(기본) | `종료추정` | `전체`.
        sort: `SORTS` 의 키. 기본 `bid_end_asc`.
        limit: 기본 20, 최대 50.
        cursor: 다음 페이지 커서.

    Returns:
        `items` · `total_count` · `next_cursor` · `query_echo` · `meta`.

    Raises:
        ToolError: 조건이 올바르지 않거나(`invalid_param`) 결과가 0건일 때(`no_result`).
    """
    applied_limit = max(1, min(limit, MAX_LIMIT))
    sort_key = sort or DEFAULT_SORT_KEY
    if sort_key not in SORTS:
        raise ToolError("invalid_param", f"정렬 값이 올바르지 않습니다: {sort_key!r}",
                        candidates=list(SORTS))
    sort_by = SORTS[sort_key]

    sgg_nm, emd_nm = await _region_filter(conn, region)
    query = ListingQuery(
        sgg_nm=sgg_nm, emd_nm=emd_nm,
        usage_ids=await _usage_ids(conn, usage),
        prpt_div_cds=_property_types(prpt_div),
        pvct_trgt=_choice(pvct_trgt, PVCT_CHOICES, "pvct_trgt", "전체"),
        min_bid_amt_min=min_price, min_bid_amt_max=max_price,
        min_bid_rate_min=min_rate, min_bid_rate_max=max_rate,
        fail_cnt_min=min_fail_cnt,
        bid_end_from=_parse_date(bid_end_after, "bid_end_after"),
        bid_end_to=_parse_date(bid_end_before, "bid_end_before"),
        statuses=_choice(status, STATUS_CHOICES, "status", DEFAULT_STATUS),
    )

    try:
        # 한 건 더 떠 본다 — limit 만큼 왔다고 끝이 아니다 (§8.6).
        sql, params = build_select(query, limit=applied_limit + 1,
                                   sort=sort_by, cursor=cursor)
    except ValueError as exc:
        raise ToolError("invalid_param", str(exc)) from exc

    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = list(await cur.fetchall())

    if not rows:
        # 빈 배열로 주지 않는다 (§8.7).
        raise ToolError("no_result",
                        "조건에 맞는 물건이 없습니다. 조건을 완화해 보세요.")

    truncated = len(rows) > applied_limit
    items = [dict(zip(COLUMNS, row, strict=True)) for row in rows[:applied_limit]]

    return ok_response(
        {
            "items": items,
            "total_count": await _total_count(conn, query),
            "next_cursor": encode_cursor(items[-1], sort_by) if truncated else None,
        },
        query_echo=_echo(query, sort_key, applied_limit),
        count=len(items),
        truncated=truncated,
        synced_at=await _synced_at(conn),
    )


def _echo(query: ListingQuery, sort_key: str, limit: int) -> dict[str, Any]:
    """**실제 적용된** 조건만 되돌려준다 (F6.4)."""
    applied = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in dataclasses.asdict(query).items()
        if value not in (None, (), "")
    }
    applied["sort"] = sort_key
    applied["limit"] = limit
    return applied
