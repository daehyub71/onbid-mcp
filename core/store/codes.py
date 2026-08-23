"""코드표 적재 — 용도 트리·주소 (F6.12).

용도 3단 트리와 시도·시군구·읍면동을 DB에 둔다. 조회 때마다 온비드에 물어보면 일일
트래픽을 검색에 쓰게 되고, 조회 지연이 외부 API 가용성에 묶인다.

**용도 트리에는 깊이가 필요하다.** 중분류로 검색하면 온비드는 0건을 준다 — 실측으로
3,506건 대 0건을 확인했다. 소분류로 확장해서 물어봐야 하고, 그 확장은 부모-자식 관계가
DB에 있어야 가능하다.

코드표는 거의 변하지 않지만 사라지지도 않는다. 지우고 다시 넣지 않고 upsert 한다 —
갱신 중 조회가 들어오면 빈 표를 보게 된다.
"""

import logging
from collections.abc import Iterable
from typing import Any, Final

import psycopg

from core.codes.address import AddressEntry
from core.codes.usage import UsageCode

logger = logging.getLogger(__name__)

USAGE_TABLE: Final = "onbid_usg_code"
ADDRESS_TABLE: Final = "onbid_addr_map"

_UPSERT_USAGE: Final = f"""
    insert into {USAGE_TABLE} (ctgr_id, ctgr_nm, up_ctgr_id, up_ctgr_nm, depth, synced_at)
    values (%(ctgr_id)s, %(ctgr_nm)s, %(up_ctgr_id)s, %(up_ctgr_nm)s, %(depth)s, now())
    on conflict (ctgr_id) do update set
        ctgr_nm = excluded.ctgr_nm,
        up_ctgr_id = excluded.up_ctgr_id,
        up_ctgr_nm = excluded.up_ctgr_nm,
        depth = excluded.depth,
        synced_at = now()
"""

_UPSERT_ADDRESS: Final = f"""
    insert into {ADDRESS_TABLE} (sd_nm, sgg_nm, emd_nm, synced_at)
    values (%(sd_nm)s, %(sgg_nm)s, %(emd_nm)s, now())
    on conflict (sd_nm, sgg_nm, emd_nm) do update set synced_at = now()
"""


async def upsert_usage_codes(
    conn: psycopg.AsyncConnection[Any], codes: Iterable[UsageCode]
) -> int:
    """용도 코드를 적재한다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        codes: `fetch_usage_tree` 가 돌려준 노드들.

    Returns:
        처리한 행 수.
    """
    rows: list[dict[str, Any]] = [
        {
            "ctgr_id": code.ctgr_id, "ctgr_nm": code.ctgr_nm,
            "up_ctgr_id": code.up_ctgr_id, "up_ctgr_nm": code.up_ctgr_nm,
            "depth": code.depth,
        }
        for code in codes
    ]
    if not rows:
        return 0

    async with conn.cursor() as cur:
        await cur.executemany(_UPSERT_USAGE, rows)

    logger.info("용도 코드표 적재: %d건", len(rows))
    return len(rows)


async def upsert_address_map(
    conn: psycopg.AsyncConnection[Any], entries: Iterable[AddressEntry]
) -> int:
    """주소 코드표를 적재한다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        entries: `fetch_address_list` 가 돌려준 항목들. set 도 받는다.

    Returns:
        처리한 행 수.
    """
    rows: list[dict[str, Any]] = [
        {"sd_nm": entry.sd_nm, "sgg_nm": entry.sgg_nm, "emd_nm": entry.emd_nm}
        for entry in entries
    ]
    if not rows:
        return 0

    async with conn.cursor() as cur:
        await cur.executemany(_UPSERT_ADDRESS, rows)

    logger.info("주소 코드표 적재: %d건", len(rows))
    return len(rows)
