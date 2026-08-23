"""나머지 툴 3종 테스트 (`pytest -m db`, SPEC §8.2·§8.3·§8.4).

세 툴이 각각 다른 이유로 조용히 틀린다.

- **상세**: 물건관리번호 하나에 공매조건번호가 최대 10개 붙는다 (F4.1). 아무거나 하나를
  주면 사용자는 자기가 본 회차가 아닌 것을 본다.
- **통계**: 재산유형이 섞인 분포를 단일 모집단으로 읽으면 잘못된 요약이 나온다 (§8.3).
- **지오코딩**: 카카오 쿼터를 **LLM 호출이 소진**한다. 서버 측 상한이 없으면 대화 몇 번으로
  배치가 쓸 쿼터가 사라진다 (F6.10).
"""

import pytest

from core.geocoder.kakao import KakaoPoint, KakaoQuotaExceededError
from onbid_mcp.common import ToolError
from onbid_mcp.tools.detail import get_auction_detail
from onbid_mcp.tools.geocode import DailyBudget, get_address_geocode
from onbid_mcp.tools.stats import get_auction_stats
from tests.conftest import Conn

pytestmark = pytest.mark.db


class FakeKakao:
    """호출 수를 세는 대역."""

    def __init__(self, *, found: bool = True, error: Exception | None = None):
        self.calls = 0
        self._point: KakaoPoint | None = (
            KakaoPoint(lat=37.5, lng=127.0, address_name="서울 강남구 개포동 12-3",
                       address_type="REGION_ADDR")
            if found else None
        )
        self._error = error

    async def search(self, address: str) -> KakaoPoint | None:
        self.calls += 1
        if self._error:
            raise self._error
        return self._point


async def any_key(conn: Conn) -> tuple[str, str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "select cltr_mng_no, pbct_cdtn_no from onbid_cltr order by cltr_mng_no limit 1")
        found = await cur.fetchone()
    assert found is not None
    return str(found[0]), str(found[1])


# ── get_auction_detail (§8.2) ──────────────────────────────────────────


async def test_detail_returns_one_item(conn: Conn) -> None:
    mng, _cdtn = await any_key(conn)

    body = await get_auction_detail(conn, cltr_mng_no=mng)

    assert body["cltr"]["cltr_mng_no"] == mng
    assert body["meta"]["notice"]


async def test_detail_omits_raw_payload(conn: Conn) -> None:
    """원본 전체를 실으면 응답이 수십 KB 가 되고 LLM 컨텍스트를 잡아먹는다 (§8.2)."""
    mng, _cdtn = await any_key(conn)

    body = await get_auction_detail(conn, cltr_mng_no=mng)

    assert "raw_payload" not in body["cltr"]


async def test_detail_includes_the_original_link(conn: Conn) -> None:
    """입찰 전 원문 확인을 안내하려면 링크가 있어야 한다 (F1.15·F6.9)."""
    mng, _cdtn = await any_key(conn)

    body = await get_auction_detail(conn, cltr_mng_no=mng)

    assert "onbid_url" in body["cltr"]


async def test_detail_selects_by_condition(conn: Conn) -> None:
    mng, cdtn = await any_key(conn)

    body = await get_auction_detail(conn, cltr_mng_no=mng, pbct_cdtn_no=cdtn)

    assert body["cltr"]["pbct_cdtn_no"] == cdtn


async def test_detail_lists_sibling_conditions(conn: Conn) -> None:
    """조건이 여러 개인데 하나만 보여주면 나머지를 찾을 방법이 없다 (F4.1)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "select cltr_mng_no from onbid_cltr group by 1 having count(*) > 1 limit 1")
        found = await cur.fetchone()
    if not found:
        pytest.skip("조건번호가 여러 개인 물건이 없다")

    body = await get_auction_detail(conn, cltr_mng_no=str(found[0]))

    assert len(body["meta"]["sibling_conditions"]) > 1


async def test_missing_item_is_not_found(conn: Conn) -> None:
    """`no_result` 가 아니라 `not_found` 다 — LLM 이 검색으로 유도해야 한다 (§8.7)."""
    with pytest.raises(ToolError) as caught:
        await get_auction_detail(conn, cltr_mng_no="없는물건번호")

    assert caught.value.code == "not_found"


# ── get_auction_stats (§8.3) ───────────────────────────────────────────


async def test_stats_returns_buckets(conn: Conn) -> None:
    body = await get_auction_stats(conn, group_by="region")

    assert body["buckets"]
    assert body["n"] == sum(b["count"] for b in body["buckets"])


async def test_stats_force_the_mixing_caveat(conn: Conn) -> None:
    body = await get_auction_stats(conn, group_by="min_bid_rate_bucket")

    assert "재산유형" in body["meta"]["caveat"]
    assert body["meta"]["prpt_div_breakdown"]


async def test_stats_reject_unknown_axis(conn: Conn) -> None:
    with pytest.raises(ToolError) as caught:
        await get_auction_stats(conn, group_by="price_score")

    assert caught.value.code == "invalid_param"
    assert caught.value.candidates


async def test_stats_never_leak_individual_items(conn: Conn) -> None:
    body = await get_auction_stats(conn, group_by="region")

    assert "items" not in body
    for bucket in body["buckets"]:
        assert set(bucket) == {"key", "label", "count"}


async def test_win_rate_axis_carries_the_population_caveat(conn: Conn) -> None:
    """표본이 '무산되어 다시 나온' 건뿐이라는 사실이 숫자보다 중요하다 (§8.3·D18)."""
    body = await get_auction_stats(conn, group_by="win_rate")

    assert "재공매" in body["meta"]["caveat"]
    assert body["meta"]["population"]


async def test_win_rate_separates_the_two_metrics(conn: Conn) -> None:
    """섞으면 300% 짜리 경쟁 강도가 낙찰가율로 읽힌다."""
    body = await get_auction_stats(conn, group_by="win_rate")

    assert "win_to_appraisal" in body
    assert "win_to_min_bid" in body


async def test_stats_apply_filters(conn: Conn) -> None:
    everything = await get_auction_stats(conn, group_by="region")
    one = await get_auction_stats(conn, group_by="region", region="강남구")

    assert one["n"] < everything["n"]


async def test_stats_reject_unknown_region(conn: Conn) -> None:
    with pytest.raises(ToolError) as caught:
        await get_auction_stats(conn, group_by="region", region="강남시")

    assert caught.value.code == "invalid_param"


# ── get_address_geocode (§8.4·F6.10) ───────────────────────────────────


async def test_geocode_returns_a_point(conn: Conn) -> None:
    kakao = FakeKakao()

    body = await get_address_geocode(conn, address="서울특별시 강남구 개포동 12-3",
                                     kakao=kakao, budget=DailyBudget(limit=10))

    assert body["lat"] and body["lng"]
    assert body["src"] == "kakao"
    assert body["matched_addr"]


async def test_geocode_reports_the_legal_code(conn: Conn) -> None:
    """법정동코드가 있어야 다른 공적 데이터와 이어붙일 수 있다 (§8.4)."""
    body = await get_address_geocode(conn, address="서울특별시 강남구 개포동 12-3",
                                     kakao=FakeKakao(), budget=DailyBudget(limit=10))

    assert "bcode" in body


async def test_geocode_uses_the_cache_before_calling(conn: Conn) -> None:
    """같은 주소를 두 번 묻는 것은 남의 프로젝트 쿼터까지 태우는 일이다 (F3.2)."""
    kakao = FakeKakao()
    budget = DailyBudget(limit=10)
    address = "서울특별시 강남구 개포동 99-99"

    await get_address_geocode(conn, address=address, kakao=kakao, budget=budget)
    await get_address_geocode(conn, address=address, kakao=kakao, budget=budget)

    assert kakao.calls == 1


async def test_cached_hit_does_not_consume_budget(conn: Conn) -> None:
    """캐시 적중은 외부 호출이 아니다 — 상한을 깎으면 쓸 수 있는 조회가 줄어든다."""
    kakao = FakeKakao()
    budget = DailyBudget(limit=2)
    address = "서울특별시 강남구 개포동 98-98"

    await get_address_geocode(conn, address=address, kakao=kakao, budget=budget)
    await get_address_geocode(conn, address=address, kakao=kakao, budget=budget)

    assert budget.used == 1


# ── 일일 상한 (F6.10 핵심) ─────────────────────────────────────────────


async def test_budget_stops_further_calls(conn: Conn) -> None:
    """상한이 없으면 대화 몇 번으로 배치가 쓸 쿼터가 사라진다."""
    kakao = FakeKakao()
    budget = DailyBudget(limit=1)

    await get_address_geocode(conn, address="서울특별시 강남구 논현동 1-1",
                             kakao=kakao, budget=budget)

    with pytest.raises(ToolError) as caught:
        await get_address_geocode(conn, address="서울특별시 강남구 논현동 2-2",
                                  kakao=kakao, budget=budget)

    assert caught.value.code == "quota_exceeded"
    assert kakao.calls == 1


async def test_budget_resets_on_a_new_day(conn: Conn) -> None:
    """'일일' 상한이 영구 상한이 되면 하루 뒤에도 못 쓴다."""
    budget = DailyBudget(limit=1)
    budget.consume(on="2026-08-23")

    budget.consume(on="2026-08-24")

    assert budget.used == 1


async def test_upstream_quota_maps_to_quota_exceeded(conn: Conn) -> None:
    kakao = FakeKakao(error=KakaoQuotaExceededError("쿼터 소진"))

    with pytest.raises(ToolError) as caught:
        await get_address_geocode(conn, address="서울특별시 강남구 삼성동 5-5",
                                  kakao=kakao, budget=DailyBudget(limit=10))

    assert caught.value.code == "quota_exceeded"


async def test_unresolvable_address_is_no_result(conn: Conn) -> None:
    """빈 배열이 아니라 오류다 — '없다' 와 '실패했다' 를 구분해야 한다 (§8.7)."""
    kakao = FakeKakao(found=False)

    with pytest.raises(ToolError) as caught:
        await get_address_geocode(conn, address="존재하지 않는 주소 999",
                                  kakao=kakao, budget=DailyBudget(limit=10))

    assert caught.value.code == "no_result"


async def test_blank_address_is_invalid_param(conn: Conn) -> None:
    kakao = FakeKakao()

    with pytest.raises(ToolError) as caught:
        await get_address_geocode(conn, address="   ", kakao=kakao,
                                  budget=DailyBudget(limit=10))

    assert caught.value.code == "invalid_param"
    assert kakao.calls == 0


async def test_budget_is_reported_in_meta(conn: Conn) -> None:
    """얼마나 남았는지 보여야 LLM 이 남발하지 않는다."""
    body = await get_address_geocode(conn, address="서울특별시 강남구 대치동 7-7",
                                     kakao=FakeKakao(), budget=DailyBudget(limit=10))

    assert body["meta"]["daily_remaining"] >= 0
