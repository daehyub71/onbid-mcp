"""물건목록 페이지 순회 테스트 (F1.9·F1.10).

respx 로 온비드를 흉내 내되, **페이지·pvctTrgtYn 별로 다른 응답**을 돌려주는 가짜 서버를 만든다.
순회 종료 조건과 Y/N 2회 순회가 핵심이다.
"""

import urllib.parse
from collections.abc import Iterator

import httpx
import pytest
import respx

from core.codes.constants import PRPT_DIV_ALL
from core.onbid.client import OnbidClient, RateLimiter
from core.onbid.collector import CollectResult, ListingFilter, collect_listings
from core.onbid.endpoints import ENDPOINTS

LIST_URL = ENDPOINTS["realestate_list"].url


class FakeOnbid:
    """pvctTrgtYn 그룹별로 정해진 건수를 페이지 단위로 돌려주는 가짜 온비드.

    Args:
        totals: 그룹(Y/N)별 전체 건수.
        empty_groups: 0건(NODATA_ERROR)으로 응답할 그룹.
    """

    def __init__(self, totals: dict[str, int], empty_groups: tuple[str, ...] = ()) -> None:
        self.totals = totals
        self.empty_groups = empty_groups
        self.requests: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        self.requests.append(params)

        group = params["pvctTrgtYn"]
        if group in self.empty_groups:
            return httpx.Response(
                200, json={"result": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}}
            )

        total = self.totals.get(group, 0)
        page = int(params["pageNo"])
        size = int(params["numOfRows"])
        start = (page - 1) * size
        rows = [
            {"cltrMngNo": f"{group}-{i:04d}", "pbctCdtnNo": str(1000 + i)}
            for i in range(start, min(start + size, total))
        ]
        return httpx.Response(200, json={
            "header": {"resultCode": "00", "resultMsg": "NORMAL_CODE"},
            "body": {"items": {"item": rows}, "totalCount": total,
                     "pageNo": page, "numOfRows": size},
        })

    def params_for(self, group: str) -> list[dict[str, str]]:
        return [p for p in self.requests if p["pvctTrgtYn"] == group]


class NoSleep:
    """리미터·백오프가 실제로 기다리지 않게 한다."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def client() -> Iterator[OnbidClient]:
    clock = NoSleep()
    api = OnbidClient(
        service_key="test-key",
        rate_limiter=RateLimiter(0, time_fn=clock.time, sleep_fn=clock.sleep),
        sleep_fn=clock.sleep,
    )
    yield api


# ── Y/N 2회 순회 (F1.9) ──────────────────────────────────────────────────


@respx.mock
async def test_collector_traverses_both_pvct_groups(client: OnbidClient) -> None:
    """pvctTrgtYn 은 단일값 필수라 Y·N 양쪽을 돌아야 전량이 나온다."""
    fake = FakeOnbid({"N": 3, "Y": 2})
    respx.get(LIST_URL).mock(side_effect=fake)

    result = await collect_listings(client, page_size=10)

    assert {p["pvctTrgtYn"] for p in fake.requests} == {"N", "Y"}
    assert result.total_by_group == {"N": 3, "Y": 2}
    assert len(result.items) == 5


@respx.mock
async def test_collector_records_group_on_each_item(client: OnbidClient) -> None:
    """어느 그룹에서 온 물건인지 알 수 있어야 한다 — 성격이 다른 모집단이다."""
    respx.get(LIST_URL).mock(side_effect=FakeOnbid({"N": 2, "Y": 1}))

    result = await collect_listings(client, page_size=10)

    assert {item.group for item in result.items} == {"N", "Y"}


# ── totalCount 기반 종료 ─────────────────────────────────────────────────


@respx.mock
async def test_collector_stops_when_total_count_reached(client: OnbidClient) -> None:
    """250건 · 페이지 100 → 그룹당 3페이지. 4페이지째를 부르지 않는다."""
    fake = FakeOnbid({"N": 250, "Y": 0})
    respx.get(LIST_URL).mock(side_effect=fake)

    result = await collect_listings(client, page_size=100)

    assert [p["pageNo"] for p in fake.params_for("N")] == ["1", "2", "3"]
    assert len(result.items) == 250


@respx.mock
async def test_collector_single_page_when_total_fits(client: OnbidClient) -> None:
    """실측: numOfRows=5000 이면 서울 전량이 그룹당 1~2페이지로 끝난다."""
    fake = FakeOnbid({"N": 4868, "Y": 1157})
    respx.get(LIST_URL).mock(side_effect=fake)

    await collect_listings(client, page_size=5000)

    assert len(fake.params_for("N")) == 1
    assert len(fake.params_for("Y")) == 1


@respx.mock
async def test_collector_handles_no_data_group(client: OnbidClient) -> None:
    """0건 그룹은 예외가 아니라 빈 결과다. 다른 그룹 수집은 계속된다."""
    fake = FakeOnbid({"N": 2}, empty_groups=("Y",))
    respx.get(LIST_URL).mock(side_effect=fake)

    result = await collect_listings(client, page_size=10)

    assert result.total_by_group == {"N": 2, "Y": 0}
    assert len(result.items) == 2
    assert len(fake.params_for("Y")) == 1  # 0건 확인 후 더 부르지 않는다


@respx.mock
async def test_collector_stops_on_short_page(client: OnbidClient) -> None:
    """totalCount 가 실제보다 커도 빈 페이지가 오면 멈춘다 (무한 루프 방지)."""
    def liar(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        page = int(params["pageNo"])
        group = params["pvctTrgtYn"]
        # 한 물건은 한 그룹에만 속한다 — 그룹별로 다른 번호를 준다
        rows = [{"cltrMngNo": f"a-{group}", "pbctCdtnNo": "1"}] if page == 1 else []
        return httpx.Response(200, json={
            "header": {"resultCode": "00"},
            "body": {"items": {"item": rows}, "totalCount": 99999,
                     "pageNo": page, "numOfRows": 10},
        })

    route = respx.get(LIST_URL).mock(side_effect=liar)
    result = await collect_listings(client, page_size=10)

    assert route.call_count == 4  # 그룹당 2회(데이터 1 + 빈 페이지 1)
    assert len(result.items) == 2


@respx.mock
async def test_collector_respects_max_pages_guard(client: OnbidClient) -> None:
    """방어선. 넘으면 조용히 자르지 않고 결과에 남긴다."""
    fake = FakeOnbid({"N": 10_000, "Y": 0})
    respx.get(LIST_URL).mock(side_effect=fake)

    result = await collect_listings(client, page_size=10, max_pages=3)

    assert len(fake.params_for("N")) == 3
    assert result.truncated is True


# ── 중복 제거 ────────────────────────────────────────────────────────────


@respx.mock
async def test_collector_deduplicates_by_composite_key(client: OnbidClient) -> None:
    """순회 중 데이터가 바뀌면 같은 물건이 두 페이지에 걸칠 수 있다."""
    def duplicating(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        page = int(params["pageNo"])
        row = {"cltrMngNo": "same", "pbctCdtnNo": "1"}
        rows = [row] if page <= 2 else []
        return httpx.Response(200, json={
            "header": {"resultCode": "00"},
            "body": {"items": {"item": rows}, "totalCount": 2,
                     "pageNo": page, "numOfRows": 1},
        })

    respx.get(LIST_URL).mock(side_effect=duplicating)
    result = await collect_listings(client, page_size=1)

    assert len(result.items) == 1
    assert result.duplicates_dropped == 3  # N에서 1건 + Y 그룹 2건


# ── 필터 (F1.10, SPEC §2.1) ──────────────────────────────────────────────


@respx.mock
async def test_collector_applies_seoul_and_sale_filters(client: OnbidClient) -> None:
    """지역은 코드가 아니라 문자열이다 — 온비드는 법정동코드를 쓰지 않는다."""
    fake = FakeOnbid({"N": 1, "Y": 0})
    respx.get(LIST_URL).mock(side_effect=fake)

    await collect_listings(client, page_size=10)

    sent = fake.requests[0]
    assert sent["lctnSdnm"] == "서울특별시"
    assert sent["dspsMthodCd"] == "0001"
    assert sent["prptDivCd"] == ",".join(PRPT_DIV_ALL)


@respx.mock
async def test_collector_incremental_mode_sends_modified_window(client: OnbidClient) -> None:
    """증분 모드는 mdfcnYmd 구간을 붙인다 (F1.8)."""
    fake = FakeOnbid({"N": 1, "Y": 0})
    respx.get(LIST_URL).mock(side_effect=fake)

    await collect_listings(
        client,
        listing_filter=ListingFilter(modified_from="20260820", modified_to="20260822"),
        page_size=10,
    )

    sent = fake.requests[0]
    assert sent["mdfcnYmdStart"] == "20260820"
    assert sent["mdfcnYmdEnd"] == "20260822"


@respx.mock
async def test_collector_full_mode_omits_modified_window(client: OnbidClient) -> None:
    """전량 모드에서는 기간 파라미터를 보내지 않는다 — tombstone 판정의 기준이 된다."""
    fake = FakeOnbid({"N": 1, "Y": 0})
    respx.get(LIST_URL).mock(side_effect=fake)

    await collect_listings(client, page_size=10)

    assert "mdfcnYmdStart" not in fake.requests[0]


@respx.mock
async def test_collector_filter_can_narrow_fail_count(client: OnbidClient) -> None:
    """입찰정보 대상(유찰 ≥1회)을 고를 때 쓴다 (F1.11)."""
    fake = FakeOnbid({"N": 1, "Y": 0})
    respx.get(LIST_URL).mock(side_effect=fake)

    await collect_listings(client, listing_filter=ListingFilter(fail_count_min=1), page_size=10)

    assert fake.requests[0]["usbdNftStart"] == "1"


def test_listing_filter_defaults_match_spec() -> None:
    """SPEC §2.1 수집 범위가 기본값이어야 한다."""
    params = ListingFilter().to_params()
    assert params["lctnSdnm"] == "서울특별시"
    assert params["dspsMthodCd"] == "0001"
    assert params["prptDivCd"] == ",".join(PRPT_DIV_ALL)
    assert "mdfcnYmdStart" not in params


def test_listing_filter_is_immutable() -> None:
    with pytest.raises((AttributeError, TypeError)):
        ListingFilter().region_sd = "부산광역시"  # type: ignore[misc]


# ── 진행 보고 (F1.5) ─────────────────────────────────────────────────────


@respx.mock
async def test_collector_reports_progress_per_page(client: OnbidClient) -> None:
    respx.get(LIST_URL).mock(side_effect=FakeOnbid({"N": 25, "Y": 0}))
    seen: list[tuple[str, int, int]] = []

    await collect_listings(
        client, page_size=10,
        on_page=lambda group, page, count: seen.append((group, page, count)),
    )

    assert seen[:3] == [("N", 1, 10), ("N", 2, 10), ("N", 3, 5)]


@respx.mock
async def test_collector_result_summarises_run(client: OnbidClient) -> None:
    """F1.5 요약 로깅에 쓸 수치가 결과에 담겨야 한다."""
    respx.get(LIST_URL).mock(side_effect=FakeOnbid({"N": 25, "Y": 5}))

    result = await collect_listings(client, page_size=10)

    assert isinstance(result, CollectResult)
    assert result.pages_fetched == 4  # N 3페이지 + Y 1페이지
    assert result.collected == 30
    assert result.truncated is False


# ── 원본 보존 (F1.3) ─────────────────────────────────────────────────────


@respx.mock
async def test_collector_keeps_rows_untouched(client: OnbidClient) -> None:
    """형변환은 정규화 계층의 몫이다. 수집기는 원본을 그대로 넘긴다."""
    def raw(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        rows = [{"cltrMngNo": "x", "pbctCdtnNo": "1", "usbdNft": 7,
                 "lowstBidPrcIndctCont": "비공개", "cltrBidEndDt": "299901021600"}]
        return httpx.Response(200, json={
            "header": {"resultCode": "00"},
            "body": {"items": {"item": rows if params["pvctTrgtYn"] == "N" else []},
                     "totalCount": 1 if params["pvctTrgtYn"] == "N" else 0,
                     "pageNo": 1, "numOfRows": 10},
        })

    respx.get(LIST_URL).mock(side_effect=raw)
    result = await collect_listings(client, page_size=10)

    row = result.items[0].raw
    assert row["lowstBidPrcIndctCont"] == "비공개"
    assert row["cltrBidEndDt"] == "299901021600"
    assert row["usbdNft"] == 7


# ── 원본 보존 (F1.3) ─────────────────────────────────────────────────────


@respx.mock
async def test_collector_does_not_mutate_raw_row(client: OnbidClient) -> None:
    """수집 그룹을 응답 행에 써넣지 않는다.

    온비드는 `pvctTrgtYn` 을 응답에 이미 100% 채워 보낸다. 우리 값으로 덮어쓰면
    `raw_payload` 가 순수 원본이 아니게 되고, API 값과 어긋날 때 그 사실이 숨겨진다.
    """
    def echo(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        group = params["pvctTrgtYn"]
        # 응답의 pvctTrgtYn 을 일부러 요청과 다르게 준다 — 덮어쓰면 이 불일치가 사라진다
        rows = [{"cltrMngNo": f"x-{group}", "pbctCdtnNo": "1", "pvctTrgtYn": "?"}]
        return httpx.Response(200, json={
            "header": {"resultCode": "00"},
            "body": {"items": {"item": rows}, "totalCount": 1, "pageNo": 1, "numOfRows": 10},
        })

    respx.get(LIST_URL).mock(side_effect=echo)
    result = await collect_listings(client, page_size=10)

    assert all(item.raw["pvctTrgtYn"] == "?" for item in result.items)
    assert {item.group for item in result.items} == {"N", "Y"}


@respx.mock
async def test_collector_item_key_uses_composite_key(client: OnbidClient) -> None:
    respx.get(LIST_URL).mock(side_effect=FakeOnbid({"N": 1, "Y": 0}))
    result = await collect_listings(client, page_size=10)
    assert result.items[0].key == ("N-0000", "1000")


# ── 페이지 실패 허용 (F1.4) ──────────────────────────────────────────────


@respx.mock
async def test_collector_records_failed_page_and_continues(client: OnbidClient) -> None:
    """재시도 소진 시 그 페이지만 실패로 남기고 다음 페이지를 계속한다."""
    def flaky(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        page, group = int(params["pageNo"]), params["pvctTrgtYn"]
        if group == "N" and page == 2:
            return httpx.Response(200, json={"header": {"resultCode": "02",
                                                        "resultMsg": "DB_ERROR"}})
        rows = [{"cltrMngNo": f"{group}-{page}", "pbctCdtnNo": "1"}]
        return httpx.Response(200, json={
            "header": {"resultCode": "00"},
            "body": {"items": {"item": rows}, "totalCount": 3, "pageNo": page, "numOfRows": 1},
        })

    respx.get(LIST_URL).mock(side_effect=flaky)
    result = await collect_listings(client, page_size=1)

    assert [(f.group, f.page) for f in result.failed_pages] == [("N", 2)]
    assert any(item.raw["cltrMngNo"] == "N-3" for item in result.items)


@respx.mock
async def test_collector_stops_group_after_consecutive_failures(client: OnbidClient) -> None:
    """연속 실패가 이어지면 그 그룹을 포기한다 — 쿼터를 태우며 헛돌지 않는다."""
    def always_fail(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        if int(params["pageNo"]) == 1:
            return httpx.Response(200, json={
                "header": {"resultCode": "00"},
                "body": {"items": {"item": [{"cltrMngNo": "a", "pbctCdtnNo": "1"}]},
                         "totalCount": 100, "pageNo": 1, "numOfRows": 1},
            })
        return httpx.Response(500)

    respx.get(LIST_URL).mock(side_effect=always_fail)
    result = await collect_listings(client, page_size=1, max_page_failures=2)

    assert len(result.failed_pages) == 4  # 그룹당 2회 연속 실패
    assert result.aborted_reason is None


@respx.mock
async def test_collector_aborts_on_quota_exceeded(client: OnbidClient) -> None:
    """쿼터 소진은 페이지 실패와 다르다. 즉시 멈추고 재개 지점을 남긴다 (N2.2)."""
    def quota(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        if int(params["pageNo"]) == 1:
            return httpx.Response(200, json={
                "header": {"resultCode": "00"},
                "body": {"items": {"item": [{"cltrMngNo": "a", "pbctCdtnNo": "1"}]},
                         "totalCount": 100, "pageNo": 1, "numOfRows": 1},
            })
        return httpx.Response(200, json={"header": {"resultCode": "22",
                                                    "resultMsg": "LIMITED_NUMBER"}})

    respx.get(LIST_URL).mock(side_effect=quota)
    result = await collect_listings(client, page_size=1)

    assert result.aborted_reason is not None
    assert "22" in result.aborted_reason
    assert result.stopped_at == ("N", 2)
    assert len(result.items) == 1  # 이미 받은 건 버리지 않는다


@respx.mock
async def test_collector_aborts_on_auth_error(client: OnbidClient) -> None:
    """키 문제도 계속할 이유가 없다."""
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(200, json={"header": {"resultCode": "30",
                                                          "resultMsg": "NOT_REGISTERED"}}))
    result = await collect_listings(client, page_size=1)

    assert result.aborted_reason is not None
    assert result.stopped_at == ("N", 1)


# ── 요약 (F1.5) ──────────────────────────────────────────────────────────


@respx.mock
async def test_collector_measures_elapsed_time(client: OnbidClient) -> None:
    respx.get(LIST_URL).mock(side_effect=FakeOnbid({"N": 1, "Y": 0}))
    ticks = iter([100.0, 103.5])

    result = await collect_listings(client, page_size=10, time_fn=lambda: next(ticks))

    assert result.elapsed_sec == pytest.approx(3.5)


@respx.mock
async def test_collector_summary_includes_all_required_numbers(client: OnbidClient) -> None:
    """F1.5: 요청 페이지 수·수집 건수·실패 페이지 수·소요 시간."""
    respx.get(LIST_URL).mock(side_effect=FakeOnbid({"N": 25, "Y": 5}))
    ticks = iter([0.0, 2.0])

    result = await collect_listings(client, page_size=10, time_fn=lambda: next(ticks))
    summary = result.summary()

    assert "30건" in summary
    assert "페이지 4" in summary
    assert "실패 0" in summary
    assert "2.0" in summary


@respx.mock
async def test_collector_logs_summary(client: OnbidClient,
                                      caplog: pytest.LogCaptureFixture) -> None:
    respx.get(LIST_URL).mock(side_effect=FakeOnbid({"N": 2, "Y": 1}))

    with caplog.at_level("INFO", logger="core.onbid.collector"):
        await collect_listings(client, page_size=10)

    assert any("수집" in record.message for record in caplog.records)


@respx.mock
async def test_collector_logs_warning_when_pages_failed(
    client: OnbidClient, caplog: pytest.LogCaptureFixture
) -> None:
    """실패를 조용히 삼키지 않는다 — 요약이 성공처럼 읽히면 안 된다."""
    def fail_second(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(str(request.url.query, "utf-8")))
        if int(params["pageNo"]) == 1:
            return httpx.Response(200, json={
                "header": {"resultCode": "00"},
                "body": {"items": {"item": [{"cltrMngNo": "a", "pbctCdtnNo": "1"}]},
                         "totalCount": 5, "pageNo": 1, "numOfRows": 1},
            })
        return httpx.Response(500)

    respx.get(LIST_URL).mock(side_effect=fail_second)
    with caplog.at_level("WARNING", logger="core.onbid.collector"):
        result = await collect_listings(client, page_size=1, max_page_failures=1)

    assert result.failed_pages
    assert any(record.levelname == "WARNING" for record in caplog.records)
