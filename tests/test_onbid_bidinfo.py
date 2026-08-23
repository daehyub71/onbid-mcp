"""입찰정보 수집 테스트 (F1.7·F1.11).

물건당 1회 호출 · 일 1,000건 한도이므로 **대상 선별과 예산 관리가 본질**이다.
유찰 0회 물건은 이력이 비어 있어 호출 자체가 낭비다 (실측: 서울 물건의 68%가 유찰 0회).
"""

import urllib.parse
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from core.onbid.bidinfo import (
    BidTarget,
    collect_bid_details,
    select_bid_targets,
)
from core.onbid.client import OnbidClient, RateLimiter
from core.onbid.collector import CollectedItem
from core.onbid.endpoints import ENDPOINTS

BID_URL = ENDPOINTS["bid_detail"].url


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


def item(mng: str, *, fail: int, bid_end: str = "202609010000",
         group: str = "N") -> CollectedItem:
    return CollectedItem(
        raw={"cltrMngNo": mng, "pbctCdtnNo": "1", "usbdNft": fail, "cltrBidEndDt": bid_end},
        group=group,
    )


def bid_body(mng: str, rounds: int = 3) -> dict[str, Any]:
    history = [
        {"pbctNsq": str(i + 1), "pbctsn": "1", "cltrOpbdDt": f"20240{i + 1}011000",
         "pbctStatNm": "유찰", "lowstBidPrcIndctCont": str(1000 - i * 100), "scfbAmt": None}
        for i in range(rounds)
    ]
    return {
        "header": {"resultCode": "00"},
        "body": {"items": {"item": [{
            "cltrMngNo": mng, "pbctCdtnNo": "1", "usbdNft": rounds,
            "prcnNsqBidRsltNm": "유찰", "prcnNsqLowstBidPrc": "700",
            "prcnBidClgList": history, "prpslEvlItemClgList": [],
        }]}, "totalCount": 1, "pageNo": 1, "numOfRows": 10},
    }


def bid_server(failing: frozenset[str] = frozenset(),
               quota_at: str | None = None) -> Any:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        mng = params["cltrMngNo"]
        calls.append(mng)
        if mng == quota_at:
            return httpx.Response(200, json={"header": {"resultCode": "22",
                                                        "resultMsg": "LIMITED_NUMBER"}})
        if mng in failing:
            return httpx.Response(500)
        return httpx.Response(200, json=bid_body(mng))

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


# ── 대상 선별 (F1.11) ────────────────────────────────────────────────────


def test_select_targets_excludes_zero_fail_items() -> None:
    """유찰 0회는 `prcnBidClgList` 가 비어 있어 호출이 낭비다 (실측 68%가 여기 해당)."""
    items = [item("a", fail=0), item("b", fail=1), item("c", fail=5)]

    targets = select_bid_targets(items)

    assert [t.cltr_mng_no for t in targets] == ["c", "b"]


def test_select_targets_orders_by_fail_count_desc() -> None:
    """유찰이 많을수록 이력의 가치가 크다 (F1.11 우선순위 ②)."""
    items = [item("a", fail=2), item("b", fail=9), item("c", fail=5)]

    assert [t.cltr_mng_no for t in select_bid_targets(items)] == ["b", "c", "a"]


def test_select_targets_breaks_ties_by_closing_soonest() -> None:
    """유찰 횟수가 같으면 마감 임박 순 (F1.11 우선순위 ③)."""
    items = [item("late", fail=3, bid_end="202612010000"),
             item("soon", fail=3, bid_end="202609010000")]

    assert [t.cltr_mng_no for t in select_bid_targets(items)] == ["soon", "late"]


def test_select_targets_excludes_private_contract_items() -> None:
    """수의계약가능 물건은 입찰이 아니라 입찰정보가 없다 — 실측 18/18건이 03 이었다."""
    items = [item("bid", fail=5), item("private", fail=17, group="Y")]

    assert [t.cltr_mng_no for t in select_bid_targets(items)] == ["bid"]


def test_select_targets_can_include_private_contract_when_asked() -> None:
    """검증 목적으로 필요하면 끌 수 있어야 한다."""
    items = [item("private", fail=17, group="Y")]

    targets = select_bid_targets(items, exclude_private_contract=False)

    assert [t.cltr_mng_no for t in targets] == ["private"]


def test_select_targets_honours_custom_threshold() -> None:
    items = [item("a", fail=1), item("b", fail=3)]
    assert [t.cltr_mng_no for t in select_bid_targets(items, min_fail_count=3)] == ["b"]


def test_select_targets_skips_rows_without_key() -> None:
    broken = CollectedItem(raw={"usbdNft": 5}, group="N")
    assert select_bid_targets([broken, item("a", fail=1)]) == [BidTarget("a", "1")]


def test_select_targets_tolerates_missing_fail_count() -> None:
    """`usbdNft` 가 없거나 숫자가 아니면 0으로 본다 — 넘겨짚어 호출하지 않는다."""
    weird = CollectedItem(raw={"cltrMngNo": "x", "pbctCdtnNo": "1", "usbdNft": None}, group="N")
    assert select_bid_targets([weird]) == []


# ── 수집 ─────────────────────────────────────────────────────────────────


@respx.mock
async def test_collect_bid_details_fetches_each_target(client: OnbidClient) -> None:
    server = bid_server()
    respx.get(BID_URL).mock(side_effect=server)
    targets = [BidTarget("a", "1"), BidTarget("b", "1")]

    result = await collect_bid_details(client, targets)

    assert server.calls == ["a", "b"]
    assert result.collected == 2
    assert result.rounds_collected == 6


@respx.mock
async def test_collect_bid_details_keeps_raw(client: OnbidClient) -> None:
    """F1.3 과 같은 원칙 — 응답을 손대지 않는다."""
    respx.get(BID_URL).mock(side_effect=bid_server())

    result = await collect_bid_details(client, [BidTarget("a", "1")])

    detail = result.details[0]
    assert detail.key == ("a", "1")
    assert detail.raw["prcnNsqBidRsltNm"] == "유찰"
    assert len(detail.rounds) == 3
    assert detail.rounds[0]["lowstBidPrcIndctCont"] == "1000"


@respx.mock
async def test_collect_bid_details_reports_progress(client: OnbidClient) -> None:
    respx.get(BID_URL).mock(side_effect=bid_server())
    seen: list[tuple[str, int]] = []

    await collect_bid_details(
        client, [BidTarget("a", "1"), BidTarget("b", "1")],
        on_item=lambda target, rounds: seen.append((target.cltr_mng_no, rounds)),
    )

    assert seen == [("a", 3), ("b", 3)]


# ── 예산 관리 (F1.11) ────────────────────────────────────────────────────


@respx.mock
async def test_collect_bid_details_stops_at_budget(client: OnbidClient) -> None:
    """일 1,000건 한도. 넘는 대상은 다음 회차로 넘긴다 — 3일 롤링의 근거."""
    server = bid_server()
    respx.get(BID_URL).mock(side_effect=server)
    targets = [BidTarget(f"t{i}", "1") for i in range(5)]

    result = await collect_bid_details(client, targets, budget=2)

    assert server.calls == ["t0", "t1"]
    assert [t.cltr_mng_no for t in result.not_attempted] == ["t2", "t3", "t4"]
    assert not result.is_complete


@respx.mock
async def test_collect_bid_details_default_budget_matches_endpoint(client: OnbidClient) -> None:
    """기본 예산은 포털이 명시한 일일 트래픽에서 가져온다."""
    respx.get(BID_URL).mock(side_effect=bid_server())

    result = await collect_bid_details(client, [BidTarget("a", "1")])

    assert result.budget == ENDPOINTS["bid_detail"].daily_traffic == 1000


@respx.mock
async def test_collect_bid_details_complete_when_all_done(client: OnbidClient) -> None:
    respx.get(BID_URL).mock(side_effect=bid_server())

    result = await collect_bid_details(client, [BidTarget("a", "1")])

    assert result.is_complete
    assert result.not_attempted == []


# ── 실패 처리 (F1.4 계열) ────────────────────────────────────────────────


@respx.mock
async def test_collect_bid_details_records_failure_and_continues(client: OnbidClient) -> None:
    server = bid_server(failing=frozenset({"b"}))
    respx.get(BID_URL).mock(side_effect=server)
    targets = [BidTarget("a", "1"), BidTarget("b", "1"), BidTarget("c", "1")]

    result = await collect_bid_details(client, targets)

    assert result.collected == 2
    assert [f.target.cltr_mng_no for f in result.failed] == ["b"]
    assert not result.is_complete


@respx.mock
async def test_collect_bid_details_aborts_on_quota(client: OnbidClient) -> None:
    """쿼터 소진은 개별 실패가 아니다. 남은 대상을 통째로 넘긴다 (N2.2)."""
    server = bid_server(quota_at="b")
    respx.get(BID_URL).mock(side_effect=server)
    targets = [BidTarget("a", "1"), BidTarget("b", "1"), BidTarget("c", "1")]

    result = await collect_bid_details(client, targets)

    assert result.collected == 1
    assert result.aborted_reason is not None
    assert [t.cltr_mng_no for t in result.not_attempted] == ["b", "c"]


@respx.mock
async def test_collect_bid_details_no_data_is_not_a_failure(client: OnbidClient) -> None:
    """이력이 없는 물건이 섞여도 실패가 아니다. 빈 이력으로 남긴다."""
    respx.get(BID_URL).mock(return_value=httpx.Response(
        200, json={"result": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}}))

    result = await collect_bid_details(client, [BidTarget("a", "1")])

    assert result.failed == []
    assert result.collected == 0
    assert result.no_data == 1


# ── 요약 (F1.5 계열) ─────────────────────────────────────────────────────


@respx.mock
async def test_collect_bid_details_summary(client: OnbidClient) -> None:
    respx.get(BID_URL).mock(side_effect=bid_server(failing=frozenset({"b"})))
    ticks = iter([0.0, 4.0])

    result = await collect_bid_details(
        client, [BidTarget("a", "1"), BidTarget("b", "1")], time_fn=lambda: next(ticks)
    )
    summary = result.summary()

    assert "1건" in summary
    assert "회차 3" in summary
    assert "실패 1" in summary
    assert "4.0" in summary


@respx.mock
async def test_collect_bid_details_logs_warning_on_failure(
    client: OnbidClient, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(BID_URL).mock(side_effect=bid_server(failing=frozenset({"a"})))

    with caplog.at_level("WARNING", logger="core.onbid.bidinfo"):
        await collect_bid_details(client, [BidTarget("a", "1")])

    assert any(record.levelname == "WARNING" for record in caplog.records)
