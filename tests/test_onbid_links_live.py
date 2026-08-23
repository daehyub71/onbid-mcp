"""온비드 원문 링크가 실제로 열리는지 확인한다 (`pytest -m live`).

URL 규칙은 활용가이드에 없어 실호출로 알아냈다. 온비드가 경로나 파라미터를 바꾸면
사용자에게 깨진 링크를 주게 되므로, 그 변화를 여기서 잡는다.

온비드 웹사이트를 호출하며 **API 쿼터는 쓰지 않는다.**
"""

import os
import pathlib
from collections.abc import AsyncGenerator

import httpx
import pytest

from core.onbid.client import OnbidClient
from core.onbid.collector import ListingFilter, collect_listings
from core.onbid.links import DETAIL_URL, detail_url

pytestmark = pytest.mark.live

USER_AGENT = "Mozilla/5.0 (compatible; onbid-mcp/0.1)"


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


@pytest.fixture
async def web() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as session:
        yield session


async def test_detail_url_opens_the_right_property(
    client: OnbidClient, web: httpx.AsyncClient
) -> None:
    """수집한 물건 몇 건의 링크가 실제로 그 물건 페이지를 연다.

    재산유형과 수의계약 그룹이 섞이도록 표본을 고른다.
    """
    listing = await collect_listings(
        client, listing_filter=ListingFilter(region_sgg="강남구"), page_size=1000
    )
    assert listing.items, "표본을 얻지 못했다"

    picks, seen = [], set()
    for item in listing.items:
        tag = (item.group, item.raw.get("prptDivNm"))
        if tag in seen:
            continue
        seen.add(tag)
        picks.append(item)
        if len(picks) >= 3:
            break

    for item in picks:
        url = detail_url(item.raw)
        assert url is not None, f"링크를 만들지 못했다: {item.key}"

        response = await web.get(url)
        assert response.status_code == 200, (
            f"{item.key}: HTTP {response.status_code} — 온비드가 URL 규칙을 바꿨을 수 있다"
        )
        assert str(item.raw["cltrMngNo"]) in response.text, (
            f"{item.key}: 페이지가 열렸지만 해당 물건이 아니다"
        )


async def test_detail_url_requires_all_four_identifiers(
    client: OnbidClient, web: httpx.AsyncClient
) -> None:
    """식별자를 하나 빼면 온비드가 오류를 낸다 — 부분 URL 을 만들지 않는 근거."""
    listing = await collect_listings(
        client, listing_filter=ListingFilter(region_sgg="강남구"), page_size=100
    )
    row = listing.items[0].raw

    partial = (
        f"{DETAIL_URL}?onbidCltrno={row['onbidCltrno']}"
        f"&onbidPbancNo={row['onbidPbancNo']}&pbctNo={row['pbctNo']}"
    )
    response = await web.get(partial)

    assert response.status_code != 200 or str(row["cltrMngNo"]) not in response.text, (
        "식별자 3개로도 물건이 열린다면 REQUIRED_ID_FIELDS 를 줄일 수 있다"
    )
