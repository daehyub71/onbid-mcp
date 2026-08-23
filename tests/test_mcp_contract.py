"""툴 계약 테스트 (F6.3·F6.4·F6.5·F6.9·§8.6·§8.7).

**서버에 등록된 실제 계약을 본다.** 함수가 옳게 동작해도 등록이 빠지거나 설명이 없으면
LLM 은 툴을 못 쓰거나 잘못 쓴다.

셋을 못박는다.

1. **모든 툴 응답에 `meta` 와 `query_echo`** 가 있다 (F6.3·F6.4).
2. **모든 툴 설명에 세 취지**가 들어간다 — 판단 없음 / 배치 수집분 / 입찰 전 원문 확인 (F6.9).
3. **오류는 5종뿐**이고 빈 배열로 대체되지 않는다 (§8.7).
"""

import json
from typing import Any

import pytest

from onbid_mcp.common import ERROR_CODES
from onbid_mcp.server import TOOL_NOTICE, build_server
from tests.conftest import Conn

pytestmark = pytest.mark.db

async def call(server: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """등록된 툴을 부르고 본문을 돌려준다."""
    result = await server.call_tool(name, args)
    return dict(json.loads(result.content[0].text))


EXPECTED_TOOLS = {
    "search_auction_items",
    "get_auction_detail",
    "get_auction_stats",
    "get_address_geocode",
}


# ── 등록 (F6.2·F7) ─────────────────────────────────────────────────────


async def test_all_four_tools_are_registered() -> None:
    tools = await build_server().list_tools()

    assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_all_four_resources_are_registered() -> None:
    resources = await build_server().list_resources()

    assert {str(r.uri) for r in resources} == {
        "onbid://codes/regions",
        "onbid://codes/usages",
        "onbid://codes/property-types",
        "onbid://dataset/status",
    }


# ── 설명 문구 (F6.9) ───────────────────────────────────────────────────


async def test_every_tool_states_the_three_points() -> None:
    """LLM 이 단정적으로 답하지 않게 하는 문구다 — 하나라도 빠지면 그 툴에서 새어 나간다."""
    for tool in await build_server().list_tools():
        assert tool.description
        assert "판단" in tool.description, tool.name
        assert "배치" in tool.description or "실시간" in tool.description, tool.name
        assert "원문" in tool.description, tool.name


def test_shared_notice_carries_all_three() -> None:
    assert "판단" in TOOL_NOTICE
    assert "실시간" in TOOL_NOTICE or "배치" in TOOL_NOTICE
    assert "원문" in TOOL_NOTICE


async def test_no_ranking_tool_exists() -> None:
    """랭킹·추천·점수 툴을 만들지 않는다 (§8.8·§2.4)."""
    names = {t.name for t in await build_server().list_tools()}

    assert not any(word in name for name in names
                   for word in ("rank", "recommend", "score", "best"))


# ── 입력 스키마 ────────────────────────────────────────────────────────


async def test_tools_declare_input_schemas() -> None:
    """스키마가 없으면 LLM 이 인자를 추측한다."""
    for tool in await build_server().list_tools():
        assert tool.input_schema
        assert tool.input_schema.get("type") == "object"


async def test_detail_requires_the_management_number() -> None:
    tools = {t.name: t for t in await build_server().list_tools()}

    required = tools["get_auction_detail"].input_schema.get("required", [])
    assert "cltr_mng_no" in required


async def test_stats_requires_the_axis() -> None:
    tools = {t.name: t for t in await build_server().list_tools()}

    assert "group_by" in tools["get_auction_stats"].input_schema.get("required", [])


# ── 응답 계약 (F6.3·F6.4) ──────────────────────────────────────────────


async def test_search_response_carries_meta_and_echo(conn: Conn) -> None:
    server = build_server()

    body = await call(server, "search_auction_items", {"region": "강남구", "limit": 2})

    assert set(body["meta"]) >= {"source", "is_realtime", "count", "truncated", "notice"}
    assert "query_echo" in body


async def test_stats_response_carries_meta_and_echo(conn: Conn) -> None:
    server = build_server()

    body = await call(server, "get_auction_stats", {"group_by": "region"})

    assert body["meta"]["is_realtime"] is False
    assert "query_echo" in body


# ── 오류 (F6.5·§8.7) ───────────────────────────────────────────────────


async def test_no_result_is_an_error_not_an_empty_list(conn: Conn) -> None:
    """빈 배열은 '없다' 와 '실패했다' 를 구분하지 못한다."""
    server = build_server()

    body = await call(server, "search_auction_items",
                      {"region": "강남구", "min_fail_cnt": 999})

    assert body["error"]["code"] == "no_result"
    assert "items" not in body


async def test_invalid_param_carries_candidates(conn: Conn) -> None:
    server = build_server()

    body = await call(server, "search_auction_items", {"region": "강남시"})

    assert body["error"]["code"] == "invalid_param"
    assert body["error"]["candidates"]


async def test_not_found_is_distinct_from_no_result(conn: Conn) -> None:
    server = build_server()

    body = await call(server, "get_auction_detail", {"cltr_mng_no": "없는번호"})

    assert body["error"]["code"] == "not_found"


async def test_error_responses_keep_meta_and_echo(conn: Conn) -> None:
    """오류일 때도 조건을 돌려줘야 LLM 이 무엇을 완화할지 안다."""
    server = build_server()

    body = await call(server, "search_auction_items",
                      {"region": "강남구", "min_fail_cnt": 999})

    assert body["meta"]["notice"]
    assert body["query_echo"]


@pytest.mark.parametrize("code", sorted(ERROR_CODES))
def test_error_codes_are_documented(code: str) -> None:
    """코드마다 LLM 이 취할 행동이 적혀 있어야 한다."""
    assert ERROR_CODES[code]


# ── Resource 읽기 ──────────────────────────────────────────────────────


async def test_resources_return_json(conn: Conn) -> None:
    server = build_server()

    for uri in ("onbid://codes/property-types", "onbid://dataset/status"):
        contents = list(await server.read_resource(uri))
        text = getattr(contents[0], "content", None) or getattr(contents[0], "text", "")
        assert json.loads(str(text))
