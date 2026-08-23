"""용도 트리·주소 목록 수집 테스트 (F1.2).

용도 트리의 재귀 종료 조건은 **`03 NODATA_ERROR`** 다 (실측). 빈 `upCtgrId` 는 `99` 오류라
루트는 반드시 값을 줘야 한다.
"""

import urllib.parse
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from core.codes.address import AddressEntry, fetch_address_list
from core.codes.usage import REALESTATE_ROOT, UsageCode, fetch_usage_tree
from core.onbid.client import OnbidClient, RateLimiter
from core.onbid.endpoints import ENDPOINTS

USAGE_URL = ENDPOINTS["usage_code"].url
ADDRESS_URL = ENDPOINTS["address"].url

#: 실측 구조의 축소판. 10000 → 2개 중분류 → 각 2개 소분류.
USAGE_TREE: dict[str, list[tuple[str, str]]] = {
    "10000": [("10100", "토지"), ("10200", "주거용건물")],
    "10100": [("10101", "대지"), ("10102", "임야")],
    "10200": [("10201", "아파트"), ("10202", "단독주택")],
}
UP_NAMES = {"10000": "부동산", "10100": "토지", "10200": "주거용건물"}


class NoSleep:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def client() -> Iterator[OnbidClient]:
    clock = NoSleep()
    yield OnbidClient(
        service_key="test-key",
        rate_limiter=RateLimiter(0, time_fn=clock.time, sleep_fn=clock.sleep),
        sleep_fn=clock.sleep,
    )


def no_data() -> httpx.Response:
    """리프 노드의 실제 응답 — body 없이 result 봉투만 온다."""
    return httpx.Response(200, json={"result": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}})


def usage_server(request: httpx.Request) -> httpx.Response:
    params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
    up = params["upCtgrId"]
    children = USAGE_TREE.get(up)
    if not children:
        return no_data()
    rows = [{"ctgrId": cid, "ctgrNm": nm, "upCtgrId": up, "upCtgrNm": UP_NAMES.get(up)}
            for cid, nm in children]
    return httpx.Response(200, json={
        "header": {"resultCode": "00"},
        "body": {"items": {"item": rows}, "totalCount": len(rows), "pageNo": 1,
                 "numOfRows": int(params["numOfRows"])},
    })


# ── 용도 트리 ────────────────────────────────────────────────────────────


@respx.mock
async def test_usage_tree_includes_root_node(client: OnbidClient) -> None:
    """물건목록의 `cltrUsgLclsCtgrId` 가 10000 이므로 루트도 트리에 있어야 한다."""
    respx.get(USAGE_URL).mock(side_effect=usage_server)

    tree = await fetch_usage_tree(client)

    root = next(node for node in tree if node.ctgr_id == REALESTATE_ROOT)
    assert root.depth == 1
    assert root.up_ctgr_id is None


@respx.mock
async def test_usage_tree_walks_three_levels(client: OnbidClient) -> None:
    respx.get(USAGE_URL).mock(side_effect=usage_server)

    tree = await fetch_usage_tree(client)

    by_depth: dict[int, list[str]] = {}
    for node in tree:
        by_depth.setdefault(node.depth, []).append(node.ctgr_id)
    assert by_depth[1] == ["10000"]
    assert sorted(by_depth[2]) == ["10100", "10200"]
    assert sorted(by_depth[3]) == ["10101", "10102", "10201", "10202"]


@respx.mock
async def test_usage_tree_stops_at_no_data_leaf(client: OnbidClient) -> None:
    """리프에서 03 이 오면 예외가 아니라 재귀 종료다."""
    route = respx.get(USAGE_URL).mock(side_effect=usage_server)

    tree = await fetch_usage_tree(client)

    # 루트 1 + 중분류 2 + 소분류 4 = 7 노드, 호출은 리프 4개 포함 7회
    assert len(tree) == 7
    assert route.call_count == 7


@respx.mock
async def test_usage_tree_keeps_parent_names(client: OnbidClient) -> None:
    respx.get(USAGE_URL).mock(side_effect=usage_server)

    tree = await fetch_usage_tree(client)

    apt = next(node for node in tree if node.ctgr_id == "10201")
    assert apt.ctgr_nm == "아파트"
    assert apt.up_ctgr_id == "10200"
    assert apt.up_ctgr_nm == "주거용건물"


@respx.mock
async def test_usage_tree_guards_against_cycles(client: OnbidClient) -> None:
    """자기 자신을 자식으로 돌려주는 응답에도 멈춰야 한다."""
    def looping(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        up = params["upCtgrId"]
        rows = [{"ctgrId": "10000", "ctgrNm": "부동산", "upCtgrId": up, "upCtgrNm": "x"}]
        return httpx.Response(200, json={
            "header": {"resultCode": "00"},
            "body": {"items": {"item": rows}, "totalCount": 1, "pageNo": 1, "numOfRows": 100},
        })

    route = respx.get(USAGE_URL).mock(side_effect=looping)
    tree = await fetch_usage_tree(client)

    assert len(tree) == 1
    assert route.call_count == 1


@respx.mock
async def test_usage_tree_respects_max_depth(client: OnbidClient) -> None:
    """깊이가 예상보다 깊어도 무한히 파고들지 않는다."""
    def endless(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        up = params["upCtgrId"]
        rows = [{"ctgrId": up + "0", "ctgrNm": "x", "upCtgrId": up, "upCtgrNm": "y"}]
        return httpx.Response(200, json={
            "header": {"resultCode": "00"},
            "body": {"items": {"item": rows}, "totalCount": 1, "pageNo": 1, "numOfRows": 100},
        })

    respx.get(USAGE_URL).mock(side_effect=endless)
    tree = await fetch_usage_tree(client, max_depth=3)

    assert max(node.depth for node in tree) == 3


def test_usage_code_is_immutable() -> None:
    node = UsageCode(ctgr_id="1", ctgr_nm="a", up_ctgr_id=None, up_ctgr_nm=None, depth=1)
    with pytest.raises((AttributeError, TypeError)):
        node.ctgr_nm = "b"  # type: ignore[misc]


# ── 주소 목록 ────────────────────────────────────────────────────────────


def address_server(rows: list[dict[str, Any]], page_size: int = 2) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        page = int(params["pageNo"])
        window = rows[(page - 1) * page_size: page * page_size]
        if not window:
            return no_data()
        return httpx.Response(200, json={
            "header": {"resultCode": "00"},
            "body": {"items": {"item": window}, "totalCount": len(rows),
                     "pageNo": page, "numOfRows": page_size},
        })
    return handler


def addr(sgg: str, emd: str, detail: str) -> dict[str, Any]:
    return {"sdnm": "서울특별시", "sggnm": sgg, "emdNm": emd, "dtlAddr": detail}


@respx.mock
async def test_address_list_deduplicates_to_district_level(client: OnbidClient) -> None:
    """응답은 물건별 상세주소라 같은 읍면동이 여러 번 나온다. 조합 단위로 접는다."""
    rows = [addr("강남구", "개포동", "155"), addr("강남구", "개포동", "174"),
            addr("강남구", "논현동", "222-30"), addr("서초구", "방배동", "1")]
    respx.get(ADDRESS_URL).mock(side_effect=address_server(rows))

    entries = await fetch_address_list(client, sd_nm="서울특별시", page_size=2)

    assert entries == [
        AddressEntry("서울특별시", "강남구", "개포동"),
        AddressEntry("서울특별시", "강남구", "논현동"),
        AddressEntry("서울특별시", "서초구", "방배동"),
    ]


@respx.mock
async def test_address_list_paginates(client: OnbidClient) -> None:
    """실측: 서울만 1,636건이라 한 페이지에 안 들어간다."""
    rows = [addr("강남구", f"동{i:02d}", str(i)) for i in range(5)]
    route = respx.get(ADDRESS_URL).mock(side_effect=address_server(rows))

    entries = await fetch_address_list(client, sd_nm="서울특별시", page_size=2)

    assert len(entries) == 5
    assert route.call_count == 3


@respx.mock
async def test_address_list_sends_filters(client: OnbidClient) -> None:
    rows = [addr("강남구", "개포동", "155")]
    route = respx.get(ADDRESS_URL).mock(side_effect=address_server(rows, page_size=10))

    await fetch_address_list(client, sd_nm="서울특별시", sgg_nm="강남구")

    sent = dict(urllib.parse.parse_qsl(str(route.calls[0].request.url.query, "utf-8")))
    assert sent["sdnm"] == "서울특별시"
    assert sent["sggnm"] == "강남구"


@respx.mock
async def test_address_list_handles_no_data(client: OnbidClient) -> None:
    respx.get(ADDRESS_URL).mock(return_value=no_data())

    assert await fetch_address_list(client, sd_nm="없는시도") == []


@respx.mock
async def test_address_list_skips_rows_missing_district(client: OnbidClient) -> None:
    """읍면동이 비면 조합을 만들 수 없다. 조용히 빈 문자열로 넣지 않는다."""
    rows = [addr("강남구", "개포동", "155"), {"sdnm": "서울특별시", "sggnm": "강남구",
                                            "emdNm": "", "dtlAddr": "x"}]
    respx.get(ADDRESS_URL).mock(side_effect=address_server(rows, page_size=10))

    entries = await fetch_address_list(client, sd_nm="서울특별시")

    assert entries == [AddressEntry("서울특별시", "강남구", "개포동")]
