"""Resource 4종 테스트 (`pytest -m db`, F7.1~F7.4·§8.5).

Resource 는 **툴이 아니다.** 필요할 때만 읽히므로 코드표를 여기 두면 LLM 이 매 검색마다
왕복 1회를 더 쓰지 않는다 (§8.5).

두 가지가 중요하다.

1. **지역 목록은 "물건이 실제로 있는" 조합**이다 (F7.1). 행정구역 전체를 주면 검색해도 0건인
   지역을 LLM 이 추천한다.
2. **`dataset/status` 는 신선도를 말한다** (F7.4·N2.3). 배치가 며칠 멈춰도 조회는 되므로,
   기준 시각이 없으면 낡은 데이터를 최신인 양 답한다.
"""

import json

import pytest

from onbid_mcp.resources import (
    RESOURCE_URIS,
    read_dataset_status,
    read_property_types,
    read_regions,
    read_usages,
)
from tests.conftest import Conn

pytestmark = pytest.mark.db


def test_uris_match_the_contract() -> None:
    assert set(RESOURCE_URIS) == {
        "onbid://codes/regions",
        "onbid://codes/usages",
        "onbid://codes/property-types",
        "onbid://dataset/status",
    }


# ── 지역 (F7.1) ────────────────────────────────────────────────────────


async def test_regions_are_grouped_by_district(conn: Conn) -> None:
    body = json.loads(await read_regions(conn))

    assert body["districts"]
    assert "강남구" in body["districts"]


async def test_regions_list_only_places_with_items(conn: Conn) -> None:
    """행정구역 전체를 주면 검색해도 0건인 지역을 LLM 이 추천한다 (F7.1)."""
    body = json.loads(await read_regions(conn))

    async with conn.cursor() as cur:
        await cur.execute("select count(distinct sgg_nm) from onbid_addr_map")
        found = await cur.fetchone()
    assert found is not None
    assert len(body["districts"]) == found[0]


async def test_regions_include_dongs(conn: Conn) -> None:
    body = json.loads(await read_regions(conn))

    assert body["districts"]["강남구"]


async def test_regions_state_the_reason(conn: Conn) -> None:
    """왜 이 목록이 전부가 아닌지 적어야 LLM 이 '강남구에 동이 이것뿐' 이라 오해하지 않는다."""
    body = json.loads(await read_regions(conn))

    assert "물건" in body["notice"]


# ── 용도 (F7.2) ────────────────────────────────────────────────────────


async def test_usages_are_a_three_level_tree(conn: Conn) -> None:
    body = json.loads(await read_usages(conn))

    depths = {node["depth"] for node in body["nodes"]}
    assert depths >= {1, 2, 3}


async def test_usages_carry_parents(conn: Conn) -> None:
    """부모를 모르면 중분류 검색을 소분류로 확장할 수 없다 (F6.12)."""
    body = json.loads(await read_usages(conn))

    leaves = [n for n in body["nodes"] if n["depth"] == 3]
    assert leaves and all(n["up_ctgr_id"] for n in leaves)


async def test_usages_explain_expansion(conn: Conn) -> None:
    """중분류로 그냥 걸면 0건이라는 사실을 알려야 한다."""
    body = json.loads(await read_usages(conn))

    assert "확장" in body["notice"] or "소분류" in body["notice"]


# ── 재산유형 (F7.3) ────────────────────────────────────────────────────


async def test_property_types_come_from_constants(conn: Conn) -> None:
    """§6.5 정적 상수다 — DB 를 타지 않는다."""
    body = json.loads(await read_property_types())

    assert len(body["types"]) >= 10
    assert any(t["name"] == "압류재산" for t in body["types"])


async def test_property_types_pair_code_and_name(conn: Conn) -> None:
    body = json.loads(await read_property_types())

    assert all({"code", "name"} == set(t) for t in body["types"])


# ── 데이터셋 상태 (F7.4·N2.3) ──────────────────────────────────────────


async def test_status_reports_freshness(conn: Conn) -> None:
    """배치가 며칠 멈춰도 조회는 된다 — 기준 시각이 없으면 낡은 값을 최신인 양 답한다."""
    body = json.loads(await read_dataset_status(conn))

    assert body["synced_at"]
    assert body["total_count"] > 0


async def test_status_breaks_down_by_property_type(conn: Conn) -> None:
    body = json.loads(await read_dataset_status(conn))

    assert body["by_property_type"]
    assert sum(body["by_property_type"].values()) == body["total_count"]


async def test_status_breaks_down_by_state(conn: Conn) -> None:
    body = json.loads(await read_dataset_status(conn))

    assert sum(body["by_status"].values()) == body["total_count"]


async def test_status_reports_geocoding_rate(conn: Conn) -> None:
    """좌표 없는 데이터로 지도 질문에 답하면 안 된다."""
    body = json.loads(await read_dataset_status(conn))

    assert 0.0 <= body["geocode_ok_rate"] <= 1.0


async def test_status_reports_the_last_batch(conn: Conn) -> None:
    """마지막 배치가 실패했는지 알아야 신선도를 판단할 수 있다."""
    body = json.loads(await read_dataset_status(conn))

    assert "last_batch" in body
