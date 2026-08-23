"""용도 트리·주소 목록의 실제 동작 확인 (`pytest -m live`).

전체 트리 순회는 리프마다 1회씩 불러 **116회 호출**이 든다. 회귀 테스트로 매번 돌리기엔
비싸므로 여기서는 `max_depth=2` 로 얕게만 확인한다. 전량 수치는 `docs/API_FINDINGS.md` 참조.
"""

import os
import pathlib
from collections.abc import AsyncGenerator

import pytest

from core.codes.address import fetch_address_list
from core.codes.usage import REALESTATE_ROOT, fetch_usage_tree
from core.onbid.client import OnbidClient

pytestmark = pytest.mark.live


@pytest.fixture
async def client() -> AsyncGenerator[OnbidClient, None]:
    raw = os.environ.get("ONBID_SERVICE_KEY", "")
    if not raw:
        env = pathlib.Path(__file__).resolve().parents[1] / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("ONBID_SERVICE_KEY="):
                    raw = line.split("=", 1)[1].strip()
                    break
    if not raw:
        pytest.skip("ONBID_SERVICE_KEY 가 없어 live 테스트를 건너뛴다")
    api = OnbidClient(service_key=raw)
    yield api
    await api.aclose()


async def test_usage_tree_root_has_five_mid_categories(client: OnbidClient) -> None:
    """실측: 부동산(10000) 아래 중분류는 토지·주거용·상가업무용·산업용·용도복합 5개다."""
    tree = await fetch_usage_tree(client, max_depth=2)

    mids = [node for node in tree if node.depth == 2]
    assert len(mids) == 5
    assert {node.ctgr_id for node in mids} == {"10100", "10200", "10300", "10400", "10500"}
    assert all(node.up_ctgr_id == REALESTATE_ROOT for node in mids)


async def test_usage_tree_ids_match_listing_fields(client: OnbidClient) -> None:
    """트리의 중분류 ID 가 물건목록의 `cltrUsgMclsCtgrId` 와 같은 체계여야 한다."""
    tree = await fetch_usage_tree(client, max_depth=2)

    ids = {node.ctgr_id for node in tree}
    assert REALESTATE_ROOT in ids       # cltrUsgLclsCtgrId
    assert "10300" in ids               # cltrUsgMclsCtgrId (상가용및업무용건물)


async def test_address_list_covers_all_seoul_districts(client: OnbidClient) -> None:
    """서울 25개 자치구 전부에 물건이 있다 (실측)."""
    entries = await fetch_address_list(client, sd_nm="서울특별시")

    districts = {entry.sgg_nm for entry in entries}
    assert len(districts) == 25
    assert all(entry.sd_nm == "서울특별시" for entry in entries)
    assert entries == sorted(entries)


async def test_address_list_narrows_by_district(client: OnbidClient) -> None:
    everything = await fetch_address_list(client, sd_nm="서울특별시")
    gangnam = await fetch_address_list(client, sd_nm="서울특별시", sgg_nm="강남구")

    assert 0 < len(gangnam) < len(everything)
    assert {entry.sgg_nm for entry in gangnam} == {"강남구"}
