"""조회 API 테스트 (`pytest -m db`, F5.1~F5.5·§8.6).

이 API 는 **MCP 툴의 내부 의존**이다. 여기서 만든 응답 형태가 그대로 LLM 에게 간다.

세 가지를 못박는다.

1. **`truncated` 가 거짓말하지 않는다.** limit 만큼 왔다고 끝이 아니다 — 한 건 더 떠 봐야 안다.
   "전부 보여줬다" 는 오해가 여기서 시작된다 (§8.6).
2. **`query_echo` 는 실제 적용된 값**이다. 요청값을 그대로 되돌려주면 상한에 걸려 잘린 것을
   알 수 없다 (F6.4).
3. **로컬 바인딩만** 허용한다 (F5.5). 이 서버는 외부에 열리면 안 된다 (§2.4 게시형 금지).
"""

from typing import Any

import httpx
import pytest

from api.main import app, get_connection, resolve_host
from tests.conftest import Conn

pytestmark = pytest.mark.db


@pytest.fixture
async def client(conn: Conn) -> Any:
    """테스트용 클라이언트. 롤백되는 연결을 주입해 흔적을 남기지 않는다."""
    async def override() -> Any:
        yield conn

    app.dependency_overrides[get_connection] = override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── 로컬 바인딩 (F5.5) ─────────────────────────────────────────────────


def test_loopback_is_allowed() -> None:
    assert resolve_host("127.0.0.1") == "127.0.0.1"


def test_public_bind_is_refused() -> None:
    """`0.0.0.0` 으로 열면 같은 네트워크의 누구나 조회할 수 있다 — §2.4 게시형 금지에 걸린다."""
    with pytest.raises(ValueError, match="로컬"):
        resolve_host("0.0.0.0")


def test_external_address_is_refused() -> None:
    with pytest.raises(ValueError, match="로컬"):
        resolve_host("192.168.0.10")


# ── /api/items ─────────────────────────────────────────────────────────


async def test_items_returns_rows(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/items", params={"limit": 3})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3
    assert body["items"][0]["cltr_mng_no"]


async def test_items_carry_meta(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/items", params={"limit": 3})).json()

    meta = body["meta"]
    assert meta["is_realtime"] is False       # 배치 수집분임을 명시 (§8.6)
    assert meta["count"] == 3
    assert meta["notice"]
    assert meta["source"]


async def test_truncated_is_true_when_more_remain(client: httpx.AsyncClient) -> None:
    """limit 만큼 왔다고 끝이 아니다. 한 건 더 떠 봐야 안다."""
    body = (await client.get("/api/items", params={"limit": 2})).json()

    assert body["meta"]["truncated"] is True
    assert body["next_cursor"]


async def test_truncated_is_false_at_the_end(client: httpx.AsyncClient) -> None:
    body = (await client.get(
        "/api/items", params={"sgg_nm": "강남구", "fail_cnt_min": 40, "limit": 100})).json()

    assert body["meta"]["truncated"] is False
    assert body["next_cursor"] is None


async def test_cursor_continues(client: httpx.AsyncClient) -> None:
    first = (await client.get("/api/items", params={"limit": 3})).json()

    second = (await client.get(
        "/api/items", params={"limit": 3, "cursor": first["next_cursor"]})).json()

    assert {i["cltr_mng_no"] for i in first["items"]} != {
        i["cltr_mng_no"] for i in second["items"]}


async def test_filters_apply(client: httpx.AsyncClient) -> None:
    body = (await client.get(
        "/api/items", params={"sgg_nm": "강남구", "limit": 5})).json()

    assert all(i["sgg_nm"] == "강남구" for i in body["items"])


async def test_query_echo_reflects_applied_values(client: httpx.AsyncClient) -> None:
    """요청값을 그대로 되돌려주면 상한에 걸려 잘린 것을 알 수 없다 (F6.4)."""
    body = (await client.get("/api/items", params={"limit": 9999})).json()

    assert body["query_echo"]["limit"] < 9999


async def test_sort_is_echoed(client: httpx.AsyncClient) -> None:
    body = (await client.get(
        "/api/items", params={"sort": "min_bid_amt", "order": "desc", "limit": 3})).json()

    assert body["query_echo"]["sort"] == "min_bid_amt"
    assert body["query_echo"]["order"] == "desc"


async def test_bad_sort_is_a_client_error(client: httpx.AsyncClient) -> None:
    """서버 오류로 처리하면 LLM 이 재시도한다 — 고쳐야 할 쪽은 요청이다."""
    response = await client.get("/api/items", params={"sort": "price_score"})

    assert response.status_code == 400
    assert "정렬" in response.json()["detail"]


async def test_bad_status_is_a_client_error(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/items", params={"status": "진행중"})

    assert response.status_code == 400


# ── /api/items/{cltr_mng_no} ───────────────────────────────────────────


async def test_detail_returns_one_item(client: httpx.AsyncClient) -> None:
    listed = (await client.get("/api/items", params={"limit": 1})).json()["items"][0]

    body = (await client.get(f"/api/items/{listed['cltr_mng_no']}")).json()

    assert body["item"]["cltr_mng_no"] == listed["cltr_mng_no"]


async def test_detail_disambiguates_by_condition(client: httpx.AsyncClient) -> None:
    """물건관리번호 하나에 공매조건번호가 최대 10개 붙는다 (F4.1)."""
    listed = (await client.get("/api/items", params={"limit": 1})).json()["items"][0]

    body = (await client.get(
        f"/api/items/{listed['cltr_mng_no']}",
        params={"pbct_cdtn_no": listed["pbct_cdtn_no"]})).json()

    assert body["item"]["pbct_cdtn_no"] == listed["pbct_cdtn_no"]


async def test_detail_reports_sibling_conditions(client: httpx.AsyncClient) -> None:
    """조건이 여러 개면 그 사실을 알려야 한다 — 하나만 보여주면 나머지를 못 찾는다."""
    listed = (await client.get("/api/items", params={"limit": 1})).json()["items"][0]

    body = (await client.get(f"/api/items/{listed['cltr_mng_no']}")).json()

    assert body["meta"]["count"] >= 1


async def test_missing_item_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/items/없는물건번호")

    assert response.status_code == 404


# ── /api/stats ─────────────────────────────────────────────────────────


async def test_stats_returns_buckets(client: httpx.AsyncClient) -> None:
    body = (await client.get(
        "/api/stats", params={"group_by": "region"})).json()

    assert body["buckets"]
    assert body["n"] == sum(b["count"] for b in body["buckets"])


async def test_stats_carry_the_mixing_caveat(client: httpx.AsyncClient) -> None:
    """유형 필터 없이 최저가율을 뽑으면 저감 체계가 다른 10종이 섞인다 (§8.3 필수)."""
    body = (await client.get(
        "/api/stats", params={"group_by": "min_bid_rate_bucket"})).json()

    assert "재산유형" in body["meta"]["caveat"]
    assert body["meta"]["prpt_div_breakdown"]


async def test_stats_drop_the_caveat_when_filtered(client: httpx.AsyncClient) -> None:
    body = (await client.get(
        "/api/stats",
        params={"group_by": "min_bid_rate_bucket", "prpt_div": "0007"})).json()

    assert body["meta"].get("caveat") is None


async def test_stats_reject_unknown_axis(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/stats", params={"group_by": "price_score"})

    assert response.status_code == 400


async def test_stats_never_expose_individual_items(client: httpx.AsyncClient) -> None:
    """집계는 집계값만 준다 (§8.3)."""
    body = (await client.get("/api/stats", params={"group_by": "region"})).json()

    assert "items" not in body
    for bucket in body["buckets"]:
        assert set(bucket) == {"key", "label", "count"}
