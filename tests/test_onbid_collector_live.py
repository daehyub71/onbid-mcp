"""수집기가 실제 온비드에서 전량을 긁어오는지 확인한다 (`pytest -m live`).

가짜 서버는 우리가 상상한 페이징만 검증한다. `totalCount` 와 실제 반환 건수가 맞는지,
Y/N 두 그룹이 실제로 갈리는지는 실호출로만 알 수 있다.
"""

import os
import pathlib
from collections.abc import AsyncGenerator

import pytest

from core.onbid.client import OnbidClient
from core.onbid.collector import ListingFilter, collect_listings

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


async def test_collector_gathers_one_district(client: OnbidClient) -> None:
    """한 개 구를 전량 수집한다. 그룹별 totalCount 와 실제 건수가 맞아야 한다."""
    result = await collect_listings(
        client,
        listing_filter=ListingFilter(region_sgg="강남구"),
        page_size=1000,
    )

    expected = sum(result.total_by_group.values())
    assert result.collected + result.duplicates_dropped >= expected * 0.95, (
        f"수집 {result.collected}건이 totalCount 합 {expected}건에 크게 못 미친다"
    )
    assert result.is_complete, f"수집이 온전하지 않다: {result.summary()}"


async def test_collector_groups_are_disjoint(client: OnbidClient) -> None:
    """한 물건은 한 그룹에만 속해야 한다. 중복 제거가 그룹 경계를 넘지 않는지 확인."""
    result = await collect_listings(
        client,
        listing_filter=ListingFilter(region_sgg="강남구"),
        page_size=1000,
    )

    keys_by_group: dict[str, set[tuple[str, str]]] = {}
    for item in result.items:
        keys_by_group.setdefault(item.group, set()).add(item.key)

    if len(keys_by_group) == 2:
        assert not (keys_by_group["N"] & keys_by_group["Y"])


async def test_collector_incremental_returns_subset(client: OnbidClient) -> None:
    """증분 모드는 전량보다 적거나 같아야 한다 (F1.8)."""
    full = await collect_listings(
        client, listing_filter=ListingFilter(region_sgg="강남구"), page_size=1000
    )
    delta = await collect_listings(
        client,
        listing_filter=ListingFilter(region_sgg="강남구", modified_from="20260801",
                                     modified_to="20260822"),
        page_size=1000,
    )

    assert delta.collected <= full.collected


async def test_collector_fail_count_filter_narrows_result(client: OnbidClient) -> None:
    """유찰 ≥1회 필터가 입찰정보 대상 선별에 쓸 만한지 확인한다 (F1.11)."""
    everything = await collect_listings(
        client, listing_filter=ListingFilter(region_sgg="강남구"), page_size=1000
    )
    failed = await collect_listings(
        client,
        listing_filter=ListingFilter(region_sgg="강남구", fail_count_min=1),
        page_size=1000,
    )

    assert failed.collected <= everything.collected
    assert all(int(item.raw["usbdNft"]) >= 1 for item in failed.items)
