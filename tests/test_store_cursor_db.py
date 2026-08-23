"""커서 페이지네이션 × 실데이터 (`pytest -m db`, F6.8).

**이 파일이 진짜 검증이다.** 순수 테스트는 SQL 문자열을 볼 뿐, 페이지를 넘기며 행이 새거나
겹치는지는 실제로 돌려 봐야 안다. 특히 일정 미정(`bid_end` null) 행이 섞인 경계에서 깨지기 쉽다.
"""

from typing import Any

import pytest

from core.store.query import (
    COLUMNS,
    DEFAULT_SORT,
    SORTABLE,
    ListingQuery,
    Sort,
    build_select,
    encode_cursor,
)
from tests.conftest import Conn

pytestmark = pytest.mark.db

PAGE = 25


async def page(conn: Conn, query: ListingQuery, *, sort: Sort,
               cursor: str | None = None, limit: int = PAGE) -> list[dict[str, Any]]:
    sql, params = build_select(query, limit=limit, sort=sort, cursor=cursor)
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return [dict(zip(COLUMNS, row, strict=True)) for row in await cur.fetchall()]


async def walk(conn: Conn, query: ListingQuery, *, sort: Sort,
               pages: int = 8) -> list[tuple[str, str]]:
    """커서로 여러 페이지를 넘기며 키를 모은다."""
    keys: list[tuple[str, str]] = []
    cursor: str | None = None
    for _ in range(pages):
        rows = await page(conn, query, sort=sort, cursor=cursor)
        if not rows:
            break
        keys.extend((r["cltr_mng_no"], r["pbct_cdtn_no"]) for r in rows)
        cursor = encode_cursor(rows[-1], sort)
    return keys


# ── 누락·중복 ──────────────────────────────────────────────────────────


async def test_paging_never_repeats_a_row(conn: Conn) -> None:
    keys = await walk(conn, ListingQuery(sgg_nm="강남구"), sort=DEFAULT_SORT)

    assert len(keys) == len(set(keys))


async def test_paging_matches_a_single_query(conn: Conn) -> None:
    """커서로 넘긴 결과가 한 번에 뽑은 것과 **순서까지** 같아야 한다."""
    query = ListingQuery(sgg_nm="강남구")
    walked = await walk(conn, query, sort=DEFAULT_SORT, pages=4)

    straight = await page(conn, query, sort=DEFAULT_SORT, limit=len(walked))

    assert [(r["cltr_mng_no"], r["pbct_cdtn_no"]) for r in straight] == walked


async def test_paging_terminates(conn: Conn) -> None:
    """끝에 닿으면 빈 페이지가 나와야 한다 — 아니면 무한 루프다."""
    query = ListingQuery(sgg_nm="강남구", fail_cnt_min=20)
    keys = await walk(conn, query, sort=DEFAULT_SORT, pages=50)

    total = await page(conn, query, sort=DEFAULT_SORT, limit=10_000)
    assert len(keys) == len(total)


# ── 일정 미정 경계 (핵심 함정) ─────────────────────────────────────────


async def test_undated_rows_are_reachable(conn: Conn) -> None:
    """`bid_end` 가 null 인 행은 맨 뒤에 있다. 튜플 비교에 null 이 섞이면 통째로 사라진다."""
    undated = await page(conn, ListingQuery(), sort=DEFAULT_SORT, limit=10_000)
    expected = sum(1 for r in undated if r["bid_end"] is None)
    assert expected > 0, "일정 미정 행이 없으면 이 테스트가 무의미하다"

    async with conn.cursor() as cur:
        await cur.execute("select count(*) from onbid_cltr where bid_end is null")
        found = await cur.fetchone()
    assert found is not None and found[0] == expected


async def test_cursor_at_the_null_boundary_continues(conn: Conn) -> None:
    """값이 있는 마지막 행에서 끊고 이어도 null 꼬리로 넘어가야 한다."""
    rows = await page(conn, ListingQuery(), sort=DEFAULT_SORT, limit=10_000)
    dated = [r for r in rows if r["bid_end"] is not None]
    assert dated, "마감일이 있는 행이 있어야 한다"

    following = await page(conn, ListingQuery(), sort=DEFAULT_SORT,
                           cursor=encode_cursor(dated[-1], DEFAULT_SORT), limit=5)

    assert all(r["bid_end"] is None for r in following)


async def test_cursor_inside_the_null_tail_does_not_go_back(conn: Conn) -> None:
    """꼬리에 들어온 뒤 값이 있는 행으로 되돌아가면 같은 행을 두 번 준다."""
    rows = await page(conn, ListingQuery(), sort=DEFAULT_SORT, limit=10_000)
    undated = [r for r in rows if r["bid_end"] is None]
    if len(undated) < 2:
        pytest.skip("일정 미정 행이 2건 미만이라 꼬리를 나눌 수 없다")

    following = await page(conn, ListingQuery(), sort=DEFAULT_SORT,
                           cursor=encode_cursor(undated[0], DEFAULT_SORT), limit=5)

    assert all(r["bid_end"] is None for r in following)


# ── 정렬별 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", sorted(SORTABLE))
async def test_every_sortable_field_pages_cleanly(conn: Conn, field: str) -> None:
    """정렬 컬럼마다 null 분포가 달라 경계가 다르게 깨진다 — 전부 돌려 본다."""
    for descending in (False, True):
        keys = await walk(conn, ListingQuery(sgg_nm="강남구"),
                          sort=Sort(field, descending=descending), pages=4)
        assert len(keys) == len(set(keys)), f"{field} descending={descending} 중복"


async def test_descending_order_is_actually_reversed(conn: Conn) -> None:
    rows = await page(conn, ListingQuery(min_bid_amt_min=1),
                      sort=Sort("min_bid_amt", descending=True), limit=10)

    amounts = [r["min_bid_amt"] for r in rows]
    assert amounts == sorted(amounts, reverse=True)
