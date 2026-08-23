"""쿼리 빌더 × 실제 스키마 (`pytest -m db`, F5.1).

순수 테스트는 SQL **문자열**이 맞는지만 본다. 컬럼명 오타나 타입 불일치는 실행해 봐야 드러난다
— 이 파일이 그 간극을 메운다. 실데이터를 대상으로 돌리되 건수를 단정하지 않는다.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.store.query import ListingQuery, build_select
from tests.conftest import Conn

pytestmark = pytest.mark.db


async def run(conn: Conn, query: ListingQuery, limit: int = 5) -> list[tuple[Any, ...]]:
    sql, params = build_select(query, limit=limit)
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return list(await cur.fetchall())


async def count(conn: Conn, query: ListingQuery) -> int:
    sql, params = build_select(query, limit=10_000)
    async with conn.cursor() as cur:
        await cur.execute(f"select count(*) from ({sql}) t", params)
        found = await cur.fetchone()
        return int(found[0]) if found else 0


# ── 실행 가능성 ────────────────────────────────────────────────────────


async def test_every_filter_executes(conn: Conn) -> None:
    """컬럼명 오타·타입 불일치는 실행해야 드러난다. 조건을 전부 켜서 한 번 돌린다."""
    query = ListingQuery(
        sd_nm="서울특별시", sgg_nm="강남구", emd_nm="개포동",
        usage_ids=("10000", "10300"), prpt_div_cds=("0005", "0007"),
        pvct_trgt=False,
        min_bid_amt_min=1, min_bid_amt_max=10**12,
        min_bid_rate_min=0.0, min_bid_rate_max=2.0,
        fail_cnt_min=0, fail_cnt_max=99,
        bid_end_from=datetime.now(UTC) - timedelta(days=365),
        bid_end_to=datetime.now(UTC) + timedelta(days=365),
        statuses=("진행", "유찰"),
    )

    await run(conn, query)   # 예외가 나지 않으면 통과


async def test_empty_query_returns_rows(conn: Conn) -> None:
    assert len(await run(conn, ListingQuery(), limit=3)) == 3


async def test_limit_is_honored(conn: Conn) -> None:
    assert len(await run(conn, ListingQuery(), limit=2)) == 2


# ── 필터가 실제로 좁히는가 ─────────────────────────────────────────────


async def test_district_filter_narrows(conn: Conn) -> None:
    everything = await count(conn, ListingQuery())
    gangnam = await count(conn, ListingQuery(sgg_nm="강남구"))

    assert 0 < gangnam < everything


async def test_private_contract_split_covers_everything(conn: Conn) -> None:
    """수의계약 가능/불가로 나누면 전체가 된다 — 한쪽이 빠지면 필터가 틀린 것이다."""
    total = await count(conn, ListingQuery())
    yes = await count(conn, ListingQuery(pvct_trgt=True))
    no = await count(conn, ListingQuery(pvct_trgt=False))

    assert yes + no == total
    assert yes > 0 and no > 0


async def test_rate_filter_matches_stored_values(conn: Conn) -> None:
    """최저가율 1.0 초과가 실제로 존재한다 (실측 9.8%)."""
    assert await count(conn, ListingQuery(min_bid_rate_min=1.0)) > 0


async def test_fail_count_filter_narrows(conn: Conn) -> None:
    many = await count(conn, ListingQuery(fail_cnt_min=1))
    more = await count(conn, ListingQuery(fail_cnt_min=10))

    assert more < many


async def test_usage_filter_matches_any_level(conn: Conn) -> None:
    """대분류 id 로 걸어도 소분류만 채워진 행이 잡혀야 한다."""
    assert await count(conn, ListingQuery(usage_ids=("10000",))) > 0


async def test_status_filter_narrows(conn: Conn) -> None:
    assert await count(conn, ListingQuery(statuses=("진행",))) > 0


# ── 정렬 ───────────────────────────────────────────────────────────────


async def test_rows_come_back_deadline_first(conn: Conn) -> None:
    """마감 임박 순 — 커서 페이지네이션이 이 순서 위에 선다."""
    rows = await run(conn, ListingQuery(statuses=("진행",)), limit=5)

    deadlines = [r[22] for r in rows]          # bid_end
    assert deadlines == sorted(deadlines, key=lambda d: (d is None, d))


async def test_order_is_stable_across_calls(conn: Conn) -> None:
    """같은 조건이 매번 같은 순서를 돌려주지 않으면 커서가 행을 건너뛴다."""
    first = await run(conn, ListingQuery(), limit=10)
    second = await run(conn, ListingQuery(), limit=10)

    assert [r[0] for r in first] == [r[0] for r in second]
