"""코드표 적재 테스트 (`pytest -m db`, F6.12).

용도 3단 트리와 시도·시군구·읍면동을 DB에 둔다. 매 조회마다 온비드에 물어보면 트래픽을
쓰고, 조회 응답 지연이 API 가용성에 묶인다.

코드표는 **거의 변하지 않지만 사라지지도 않는다** — 새로 생긴 코드만 더해지고,
기존 행은 덮어써도 값이 같아야 한다 (AC2).
"""

import pytest

from core.codes.address import AddressEntry
from core.codes.usage import UsageCode
from core.store.codes import upsert_address_map, upsert_usage_codes
from tests.conftest import Conn

pytestmark = pytest.mark.db

REALTY = UsageCode(ctgr_id="T-10000", ctgr_nm="부동산", up_ctgr_id=None,
                   up_ctgr_nm=None, depth=1)
SHOPS = UsageCode(ctgr_id="T-10300", ctgr_nm="상가용및업무용건물", up_ctgr_id="T-10000",
                  up_ctgr_nm="부동산", depth=2)
NEIGHBOR = UsageCode(ctgr_id="T-10301", ctgr_nm="근린생활시설", up_ctgr_id="T-10300",
                     up_ctgr_nm="상가용및업무용건물", depth=3)

GAEPO = AddressEntry(sd_nm="테스트특별시", sgg_nm="강남구", emd_nm="개포동")
SEOCHO = AddressEntry(sd_nm="테스트특별시", sgg_nm="서초구", emd_nm="반포동")


async def usage_row(conn: Conn, ctgr_id: str) -> tuple[object, ...] | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "select ctgr_nm, up_ctgr_id, up_ctgr_nm, depth from onbid_usg_code "
            "where ctgr_id = %s", (ctgr_id,))
        return await cur.fetchone()


async def address_count(conn: Conn, sd_nm: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "select count(*) from onbid_addr_map where sd_nm = %s", (sd_nm,))
        found = await cur.fetchone()
        return int(found[0]) if found else 0


# ── 용도 코드표 ─────────────────────────────────────────────────────────


async def test_usage_codes_are_loaded_with_depth(conn: Conn) -> None:
    """대·중·소 깊이가 있어야 중분류 검색을 소분류로 확장할 수 있다 (F6.12)."""
    assert await upsert_usage_codes(conn, [REALTY, SHOPS, NEIGHBOR]) == 3

    assert await usage_row(conn, "T-10301") == (
        "근린생활시설", "T-10300", "상가용및업무용건물", 3)


async def test_usage_root_has_no_parent(conn: Conn) -> None:
    await upsert_usage_codes(conn, [REALTY])
    row = await usage_row(conn, "T-10000")
    assert row is not None
    assert row[1] is None


async def test_usage_codes_are_idempotent(conn: Conn) -> None:
    """주간 갱신이 코드표를 부풀리면 안 된다 (AC2)."""
    await upsert_usage_codes(conn, [REALTY, SHOPS])
    before = await usage_row(conn, "T-10300")

    await upsert_usage_codes(conn, [REALTY, SHOPS])

    assert await usage_row(conn, "T-10300") == before


async def test_usage_code_rename_is_applied(conn: Conn) -> None:
    """온비드가 이름을 바꾸면 따라간다 — id 가 키다."""
    await upsert_usage_codes(conn, [SHOPS])

    renamed = UsageCode(ctgr_id="T-10300", ctgr_nm="상가·업무용", up_ctgr_id="T-10000",
                        up_ctgr_nm="부동산", depth=2)
    await upsert_usage_codes(conn, [renamed])

    row = await usage_row(conn, "T-10300")
    assert row is not None
    assert row[0] == "상가·업무용"


async def test_usage_codes_of_empty_list(conn: Conn) -> None:
    assert await upsert_usage_codes(conn, []) == 0


# ── 주소 코드표 ─────────────────────────────────────────────────────────


async def test_address_map_is_loaded(conn: Conn) -> None:
    assert await upsert_address_map(conn, [GAEPO, SEOCHO]) == 2
    assert await address_count(conn, "테스트특별시") == 2


async def test_address_map_is_idempotent(conn: Conn) -> None:
    await upsert_address_map(conn, [GAEPO, SEOCHO])
    await upsert_address_map(conn, [GAEPO, SEOCHO])

    assert await address_count(conn, "테스트특별시") == 2


async def test_address_map_accepts_an_iterable(conn: Conn) -> None:
    """`fetch_address_list` 는 set 을 준다 — 시퀀스를 요구하면 호출부가 변환해야 한다."""
    assert await upsert_address_map(conn, {GAEPO}) == 1


async def test_address_map_of_empty_list(conn: Conn) -> None:
    assert await upsert_address_map(conn, []) == 0
