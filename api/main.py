"""조회 API (F5.1~F5.5).

**MCP 툴의 내부 의존이다.** 여기서 정한 응답 형태가 그대로 LLM 에게 간다.

- `meta` 는 모든 응답에 붙는다 (§8.6). `is_realtime: false` 로 배치 수집분임을 밝히고,
  `count`/`truncated` 로 "전부 보여줬다" 는 오해를 막는다.
- `query_echo` 는 **실제 적용된 값**이다 (F6.4). 요청값을 그대로 되돌려주면 상한에 걸려 잘린
  것을 알 수 없다.
- **로컬 바인딩만** 허용한다 (F5.5). 외부에 열면 조회형이 아니라 게시형이 되어 §2.4 에 걸린다.

집계 응답은 **개별 물건을 담지 않는다** (§8.3).
"""

import dataclasses
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from ipaddress import ip_address
from typing import Annotated, Any, Final

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query

from core.stats.distribution import aggregate
from core.store.connection import Database
from core.store.query import (
    COLUMNS,
    ListingQuery,
    Sort,
    build_select,
    encode_cursor,
)

logger = logging.getLogger(__name__)

MAX_LIMIT: Final = 100
"""한 번에 돌려줄 상한. 넘겨 받으면 잘라내고 `query_echo` 에 적용값을 싣는다."""

DEFAULT_LIMIT: Final = 20

SOURCE: Final = "온비드(한국자산관리공사) / 공공데이터포털"
NOTICE: Final = "정보 제공 목적입니다. 입찰 전 온비드 원문을 확인하세요."

app = FastAPI(
    title="onbid-mcp 조회 API",
    description="공매 물건 조회. 로컬 전용이며 외부에 노출하지 않는다 (F5.5).",
)

_database = Database()


async def get_connection() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """요청당 연결을 내준다. 테스트는 이 의존성을 갈아 끼운다."""
    yield await _database.connect()


#: 라우트가 공유하는 연결 의존성.
Conn = Annotated[psycopg.AsyncConnection[Any], Depends(get_connection)]


def resolve_host(host: str) -> str:
    """바인딩 주소를 검증한다 (F5.5).

    Args:
        host: 바인딩할 주소.

    Returns:
        검증된 주소.

    Raises:
        ValueError: 루프백이 아닐 때. 외부에 열면 조회형의 경계를 벗어난다 (§2.4).
    """
    if not ip_address(host).is_loopback:
        raise ValueError(
            f"로컬(루프백) 주소만 허용한다: {host!r} — 이 서버는 외부에 노출하지 않는다 (F5.5)"
        )
    return host


def _meta(count: int, *, truncated: bool, synced_at: datetime | None,
          **extra: Any) -> dict[str, Any]:
    """공통 `meta` 블록 (§8.6)."""
    meta: dict[str, Any] = {
        "source": SOURCE,
        "synced_at": synced_at.isoformat() if synced_at else None,
        "is_realtime": False,
        "count": count,
        "truncated": truncated,
        "notice": NOTICE,
    }
    meta.update(extra)
    return meta


async def _synced_at(conn: psycopg.AsyncConnection[Any]) -> datetime | None:
    """마지막 적재 시각. 신선도를 알리는 값이다 (N2.3)."""
    async with conn.cursor() as cur:
        await cur.execute("select max(synced_at) from onbid_cltr")
        found = await cur.fetchone()
    return found[0] if found else None


def _listing_query(
    sd_nm: str | None, sgg_nm: str | None, emd_nm: str | None,
    usage: str | None, prpt_div: str | None, pvct_trgt: bool | None,
    min_amt: int | None, max_amt: int | None,
    min_rate: float | None, max_rate: float | None,
    fail_cnt_min: int | None, fail_cnt_max: int | None,
    status: str | None,
) -> ListingQuery:
    """질의 파라미터를 조회 조건으로 바꾼다."""
    return ListingQuery(
        sd_nm=sd_nm, sgg_nm=sgg_nm, emd_nm=emd_nm,
        usage_ids=tuple(usage.split(",")) if usage else (),
        prpt_div_cds=tuple(prpt_div.split(",")) if prpt_div else (),
        pvct_trgt=pvct_trgt,
        min_bid_amt_min=min_amt, min_bid_amt_max=max_amt,
        min_bid_rate_min=min_rate, min_bid_rate_max=max_rate,
        fail_cnt_min=fail_cnt_min, fail_cnt_max=fail_cnt_max,
        statuses=(status,) if status else (),
    )


def _rows_to_items(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [dict(zip(COLUMNS, row, strict=True)) for row in rows]


def _echo(query: ListingQuery) -> dict[str, Any]:
    """적용된 조건만 되돌려준다 — 비어 있는 필터까지 실으면 무엇을 걸었는지 가려진다."""
    return {k: v for k, v in dataclasses.asdict(query).items() if v not in (None, (), "")}


@app.get("/api/items")
async def list_items(  # noqa: PLR0913 — 필터가 곧 계약이라 묶으면 오히려 가려진다
    conn: Conn,
    sd_nm: str | None = None,
    sgg_nm: str | None = None,
    emd_nm: str | None = None,
    usage: str | None = Query(None, description="용도 id. 쉼표로 여러 개"),
    prpt_div: str | None = Query(None, description="재산유형코드. 쉼표로 여러 개"),
    pvct_trgt: bool | None = Query(None, description="수의계약 가능 여부"),
    min_amt: int | None = None,
    max_amt: int | None = None,
    min_rate: float | None = None,
    max_rate: float | None = None,
    fail_cnt_min: int | None = None,
    fail_cnt_max: int | None = None,
    status: str | None = None,
    sort: str = "bid_end",
    order: str = "asc",
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """조건에 맞는 물건을 조회한다 (F5.1).

    Raises:
        HTTPException: 정렬·상태·커서가 잘못됐을 때 400.
    """
    applied_limit = max(1, min(limit, MAX_LIMIT))
    query = _listing_query(sd_nm, sgg_nm, emd_nm, usage, prpt_div, pvct_trgt,
                           min_amt, max_amt, min_rate, max_rate,
                           fail_cnt_min, fail_cnt_max, status)
    sort_by = Sort(field=sort, descending=order.lower() == "desc")

    try:
        # 한 건 더 떠 본다 — limit 만큼 왔다고 끝이 아니다 (§8.6 truncated).
        sql, params = build_select(query, limit=applied_limit + 1,
                                   sort=sort_by, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = list(await cur.fetchall())

    truncated = len(rows) > applied_limit
    items = _rows_to_items(rows[:applied_limit])

    return {
        "items": items,
        "next_cursor": encode_cursor(items[-1], sort_by) if truncated and items else None,
        "query_echo": {
            **_echo(query),
            "sort": sort_by.field, "order": sort_by.direction, "limit": applied_limit,
        },
        "meta": _meta(len(items), truncated=truncated, synced_at=await _synced_at(conn)),
    }


@app.get("/api/items/{cltr_mng_no}")
async def get_item(
    cltr_mng_no: str,
    conn: Conn,
    pbct_cdtn_no: str | None = None,
) -> dict[str, Any]:
    """물건 단건을 조회한다 (F5.2).

    물건관리번호 하나에 공매조건번호가 최대 10개 붙으므로(F4.1), 조건을 지정하지 않으면
    첫 건을 주되 **형제 조건이 몇 개인지 `meta.count` 로 알린다.**

    Raises:
        HTTPException: 물건이 없을 때 404.
    """
    where = "where cltr_mng_no = %(mng)s"
    params: dict[str, Any] = {"mng": cltr_mng_no}
    if pbct_cdtn_no:
        where += " and pbct_cdtn_no = %(cdtn)s"
        params["cdtn"] = pbct_cdtn_no

    async with conn.cursor() as cur:
        await cur.execute(
            f"select {', '.join(COLUMNS)} from onbid_cltr {where}"
            " order by pbct_cdtn_no", params)
        rows = list(await cur.fetchall())

    if not rows:
        raise HTTPException(status_code=404, detail=f"물건을 찾을 수 없다: {cltr_mng_no}")

    items = _rows_to_items(rows)
    return {
        "item": items[0],
        "query_echo": {"cltr_mng_no": cltr_mng_no, "pbct_cdtn_no": pbct_cdtn_no},
        "meta": _meta(len(items), truncated=False, synced_at=await _synced_at(conn),
                      sibling_conditions=[i["pbct_cdtn_no"] for i in items]),
    }


@app.get("/api/stats")
async def get_stats(  # noqa: PLR0913
    conn: Conn,
    group_by: str = Query(..., description="집계 축"),
    sd_nm: str | None = None,
    sgg_nm: str | None = None,
    emd_nm: str | None = None,
    usage: str | None = None,
    prpt_div: str | None = None,
    pvct_trgt: bool | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """분포를 집계한다 (F5.3·§8.3).

    **개별 물건을 담지 않는다.**

    Raises:
        HTTPException: 축·상태가 잘못됐을 때 400.
    """
    query = _listing_query(sd_nm, sgg_nm, emd_nm, usage, prpt_div, pvct_trgt,
                           None, None, None, None, None, None, status)
    try:
        result = await aggregate(conn, group_by=group_by, query=query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    extra: dict[str, Any] = {}
    if result.caveat:
        extra["caveat"] = result.caveat
        extra["prpt_div_breakdown"] = result.prpt_div_breakdown

    return {
        "group_by": result.group_by,
        "buckets": [{"key": b.key, "label": b.label, "count": b.count}
                    for b in result.buckets],
        "n": result.n,
        "query_echo": _echo(query),
        "meta": _meta(len(result.buckets), truncated=False,
                      synced_at=await _synced_at(conn), **extra),
    }


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """개발용 실행기. **루프백 밖으로는 열지 않는다** (F5.5)."""
    import uvicorn  # noqa: PLC0415

    uvicorn.run(app, host=resolve_host(host), port=port)
