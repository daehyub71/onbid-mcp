"""MCP Resource 4종 (F7.1~F7.4·§8.5).

**Resource 는 툴이 아니다.** 필요할 때만 읽히므로 코드표를 여기 두면 LLM 이 매 검색마다
왕복 1회를 더 쓰지 않는다. 툴 표면도 좁게 유지된다.

두 가지가 이 모듈의 핵심이다.

- **지역 목록은 "물건이 실제로 있는" 조합**이다 (F7.1). 온비드의 주소 API 는 행정구역
  코드표가 아니라 **등록 물건의 주소 목록**이라, 결과가 곧 "검색해도 0건인 지역" 을 걸러 준다.
  행정구역 전체를 주면 LLM 이 물건 없는 동네를 추천한다.
- **`dataset/status` 는 신선도를 말한다** (F7.4·N2.3). 배치가 며칠 멈춰도 조회는 되므로,
  기준 시각이 없으면 낡은 데이터를 최신인 양 답한다.
"""

import json
import logging
from typing import Any, Final

import psycopg

from core.codes.constants import PRPT_DIV_NAMES
from core.store.codes import load_address_entries, load_usage_codes

logger = logging.getLogger(__name__)

RESOURCE_URIS: Final = {
    "onbid://codes/regions": "물건이 존재하는 서울 시군구·읍면동 조합",
    "onbid://codes/usages": "부동산 용도 3단 계층 트리 (대/중/소분류)",
    "onbid://codes/property-types": "재산유형 코드표",
    "onbid://dataset/status": "최근 배치 시각·건수·지오코딩 성공률",
}

REGION_NOTICE: Final = (
    "물건이 실제로 등록된 지역만 담았습니다. 행정구역 전체 목록이 아니므로, "
    "여기 없는 지역은 현재 공매 물건이 없다는 뜻입니다."
)

USAGE_NOTICE: Final = (
    "물건에는 소분류 코드만 들어 있습니다. 중분류로 검색하면 0건이 나오므로 "
    "하위 소분류까지 확장해 조회해야 합니다 — search_auction_items 가 자동으로 확장합니다."
)


def _dump(payload: dict[str, Any]) -> str:
    """Resource 본문은 문자열이다. 한글을 이스케이프하지 않는다."""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


async def read_regions(conn: psycopg.AsyncConnection[Any]) -> str:
    """`onbid://codes/regions` — 물건이 존재하는 시군구·읍면동 (F7.1).

    Args:
        conn: 열린 연결.

    Returns:
        JSON 문자열. 시군구를 키로 읍면동을 묶는다.
    """
    entries = await load_address_entries(conn)

    districts: dict[str, list[str]] = {}
    for entry in entries:
        districts.setdefault(entry.sgg_nm, []).append(entry.emd_nm)

    return _dump({
        "districts": {name: sorted(set(dongs)) for name, dongs in sorted(districts.items())},
        "district_count": len(districts),
        "combination_count": len(entries),
        "notice": REGION_NOTICE,
    })


async def read_usages(conn: psycopg.AsyncConnection[Any]) -> str:
    """`onbid://codes/usages` — 용도 3단 트리 (F7.2).

    Args:
        conn: 열린 연결.

    Returns:
        JSON 문자열. 부모 id 를 함께 담아 확장이 가능하게 한다.
    """
    nodes = await load_usage_codes(conn)

    return _dump({
        "nodes": [
            {"ctgr_id": n.ctgr_id, "ctgr_nm": n.ctgr_nm, "depth": n.depth,
             "up_ctgr_id": n.up_ctgr_id, "up_ctgr_nm": n.up_ctgr_nm}
            for n in nodes
        ],
        "node_count": len(nodes),
        "notice": USAGE_NOTICE,
    })


async def read_property_types() -> str:
    """`onbid://codes/property-types` — 재산유형 (F7.3).

    §6.5 정적 상수라 DB 를 타지 않는다.

    Returns:
        JSON 문자열.
    """
    return _dump({
        "types": [{"code": code, "name": name}
                  for code, name in PRPT_DIV_NAMES.items()],
        "notice": ("유형마다 감정가 산정과 유찰 저감 체계가 달라 "
                   "통계는 유형별로 보는 것이 정확합니다."),
    })


async def read_dataset_status(conn: psycopg.AsyncConnection[Any]) -> str:
    """`onbid://dataset/status` — 데이터 신선도와 규모 (F7.4·N2.3).

    Args:
        conn: 열린 연결.

    Returns:
        JSON 문자열. 기준 시각·총 건수·유형별·상태별 건수·지오코딩 성공률.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "select count(*), max(synced_at),"
            " count(*) filter (where geocode_status = 'ok'),"
            " count(*) filter (where lat is not null)"
            " from onbid_cltr")
        summary = await cur.fetchone()
        assert summary is not None   # 집계 질의는 항상 한 행을 준다
        total, synced_at, ok_count, located = summary

        await cur.execute(
            "select coalesce(prpt_div_nm, '미상'), count(*) from onbid_cltr"
            " group by 1 order by 2 desc")
        by_type = {str(r[0]): int(r[1]) for r in await cur.fetchall()}

        await cur.execute(
            "select coalesce(status, '미상'), count(*) from onbid_cltr"
            " group by 1 order by 2 desc")
        by_status = {str(r[0]): int(r[1]) for r in await cur.fetchall()}

        # 마지막 배치가 실패했는지 알아야 신선도를 판단할 수 있다.
        await cur.execute(
            "select mode, status, finished_at from onbid_batch_run"
            " where finished_at is not null order by finished_at desc limit 1")
        last = await cur.fetchone()

    return _dump({
        "synced_at": synced_at,
        "total_count": int(total or 0),
        "by_property_type": by_type,
        "by_status": by_status,
        "geocode_ok_rate": round(int(ok_count or 0) / total, 4) if total else 0.0,
        "located_count": int(located or 0),
        "last_batch": (
            {"mode": last[0], "status": last[1], "finished_at": last[2]} if last else None
        ),
        "notice": "배치로 수집한 데이터입니다. 실시간이 아니며 입찰 전 온비드 원문을 확인하세요.",
    })
