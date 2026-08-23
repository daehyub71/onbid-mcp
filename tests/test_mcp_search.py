"""`search_auction_items` 테스트 (`pytest -m db`, SPEC §8.1·F6.5~F6.12).

**툴 계층이 하는 일은 조회가 아니라 통역이다.** 사람이 "강남구 아파트" 라고 말하면 코드로
바꾸고, 없는 이름이면 후보를 돌려주고, 0건이면 빈 배열이 아니라 `no_result` 로 알린다.

여기서 못박는 것 넷:

1. **중분류는 소분류까지 확장**한다 — 확장하지 않으면 `주거용건물` 검색이 0건이다 (F6.12).
2. **명칭 매칭 실패는 후보를 준다** — 빈 결과보다 "무엇을 고를 수 있는지" 가 유용하다 (F6.7).
3. **0건은 `no_result`** 다. 빈 배열은 '없다' 와 '실패했다' 를 구분하지 못한다 (§8.7).
4. **`pvct_trgt` 는 성격이 다른 두 모집단**을 가른다 — 기본은 전체다 (§8.1).
"""

from typing import Any

import pytest

from onbid_mcp.common import ToolError
from onbid_mcp.tools.search import search_auction_items
from tests.conftest import Conn

pytestmark = pytest.mark.db


async def search(conn: Conn, **kwargs: Any) -> dict[str, Any]:
    return await search_auction_items(conn, **kwargs)


# ── 기본 조회 ──────────────────────────────────────────────────────────


async def test_returns_items_with_meta(conn: Conn) -> None:
    body = await search(conn, region="강남구", limit=3)

    assert len(body["items"]) == 3
    assert body["meta"]["is_realtime"] is False
    assert body["query_echo"]


async def test_total_count_is_reported(conn: Conn) -> None:
    """몇 건 중 몇 건을 보여주는지 알아야 LLM 이 '전부' 라고 말하지 않는다 (§8.1)."""
    body = await search(conn, region="강남구", limit=2)

    assert body["total_count"] > len(body["items"])
    assert body["meta"]["truncated"] is True


async def test_cursor_continues(conn: Conn) -> None:
    first = await search(conn, region="강남구", limit=2)

    second = await search(conn, region="강남구", limit=2, cursor=first["next_cursor"])

    assert {i["cltr_mng_no"] for i in first["items"]} != {
        i["cltr_mng_no"] for i in second["items"]}


async def test_limit_is_capped(conn: Conn) -> None:
    """최대 50 이다 (§8.1). 넘겨 받으면 잘라내고 적용값을 되돌려준다."""
    body = await search(conn, region="강남구", limit=500)

    assert len(body["items"]) <= 50
    assert body["query_echo"]["limit"] == 50


# ── 명칭 해석 (F6.6·F6.7) ──────────────────────────────────────────────


async def test_region_accepts_a_korean_name(conn: Conn) -> None:
    body = await search(conn, region="강남구", limit=1)

    assert body["items"][0]["sgg_nm"] == "강남구"


async def test_unknown_region_offers_candidates(conn: Conn) -> None:
    """빈 결과보다 '무엇을 고를 수 있는지' 가 유용하다 (F6.7)."""
    with pytest.raises(ToolError) as caught:
        await search(conn, region="강남시")

    assert caught.value.code == "invalid_param"
    assert caught.value.candidates


async def test_property_type_accepts_name_or_code(conn: Conn) -> None:
    by_name = await search(conn, prpt_div="압류재산", limit=1)
    by_code = await search(conn, prpt_div="0007", limit=1)

    assert by_name["query_echo"]["prpt_div_cds"] == by_code["query_echo"]["prpt_div_cds"]


async def test_unknown_property_type_offers_candidates(conn: Conn) -> None:
    with pytest.raises(ToolError) as caught:
        await search(conn, prpt_div="압류자산")

    assert caught.value.code == "invalid_param"
    assert caught.value.candidates


# ── 용도 확장 (F6.12) ──────────────────────────────────────────────────


async def test_usage_expands_to_subcategories(conn: Conn) -> None:
    """물건에는 소분류만 들어 있다 — 확장하지 않으면 중분류 검색이 0건이다."""
    body = await search(conn, usage="주거용건물", limit=5)

    assert body["items"]
    assert len(body["query_echo"]["usage_ids"]) > 1


async def test_usage_accepts_a_code(conn: Conn) -> None:
    body = await search(conn, usage="10000", limit=3)
    assert body["items"]


async def test_unknown_usage_offers_candidates(conn: Conn) -> None:
    with pytest.raises(ToolError) as caught:
        await search(conn, usage="아파트먼트하우스")

    assert caught.value.code == "invalid_param"
    assert caught.value.candidates


# ── 모집단 구분 (§8.1) ─────────────────────────────────────────────────


async def test_private_contract_filter_splits_populations(conn: Conn) -> None:
    """수의계약 물건은 전량이 유찰 경험자다 — 성격이 다른 모집단이다."""
    bidding = await search(conn, pvct_trgt="입찰", limit=5)
    private = await search(conn, pvct_trgt="수의계약", limit=5)

    assert all(i["pvct_trgt_yn"] is False for i in bidding["items"])
    assert all(i["pvct_trgt_yn"] is True for i in private["items"])


async def test_default_covers_both_populations(conn: Conn) -> None:
    both = await search(conn, limit=5)
    assert "pvct_trgt" not in both["query_echo"]


async def test_unknown_enum_is_invalid_param(conn: Conn) -> None:
    with pytest.raises(ToolError) as caught:
        await search(conn, pvct_trgt="아무거나")

    assert caught.value.code == "invalid_param"


# ── 0건 (§8.7 핵심) ────────────────────────────────────────────────────


async def test_no_result_is_an_error_not_an_empty_list(conn: Conn) -> None:
    """빈 배열은 '없다' 와 '실패했다' 를 구분하지 못한다."""
    with pytest.raises(ToolError) as caught:
        await search(conn, region="강남구", min_fail_cnt=999)

    assert caught.value.code == "no_result"


# ── 정렬 (F6.8·§2.4) ───────────────────────────────────────────────────


async def test_sort_whitelist(conn: Conn) -> None:
    body = await search(conn, region="강남구", sort="min_bid_amt_desc", limit=5)

    amounts = [i["min_bid_amt"] for i in body["items"] if i["min_bid_amt"] is not None]
    assert amounts == sorted(amounts, reverse=True)


async def test_unknown_sort_is_invalid_param(conn: Conn) -> None:
    with pytest.raises(ToolError) as caught:
        await search(conn, sort="price_score_desc")

    assert caught.value.code == "invalid_param"


async def test_default_sort_is_deadline(conn: Conn) -> None:
    body = await search(conn, region="강남구", limit=3)
    assert body["query_echo"]["sort"] == "bid_end_asc"


# ── 상태 (§8.1) ────────────────────────────────────────────────────────


async def test_status_defaults_to_in_progress(conn: Conn) -> None:
    """끝난 물건이 기본으로 섞이면 '지금 살 수 있는 것' 을 묻는 질문에 잘못 답한다."""
    body = await search(conn, region="강남구", limit=5)

    assert all(i["status"] == "진행" for i in body["items"])
    assert body["query_echo"]["statuses"] == ["진행"]


async def test_status_all_includes_everything(conn: Conn) -> None:
    body = await search(conn, region="강남구", status="전체", limit=5)
    assert "statuses" not in body["query_echo"]
