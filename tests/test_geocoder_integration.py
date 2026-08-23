"""사다리 × 실제 클라이언트 통합 테스트 (F3.1·F3.3, HTTP mock).

`test_geocoder_resolver` 는 사다리 논리를 **가짜 검색기**로 시험한다. 여기서는 **실제
`KakaoClient` 를 HTTP mock 위에 올려** 둘이 맞물리는지 본다 — 응답을 오류로 바꾸는 곳은
클라이언트이고 그 오류로 사다리를 멈추는 곳은 resolver 라, 둘을 따로 시험하면 경계가 빈다.

여기서 확인하는 것:

- **폴백 전환**이 HTTP 응답(빈 결과)으로 실제로 일어나는가
- **429 가 사다리를 뚫고 올라와** 배치를 중단시키는가 (F3.3)
- 중단 시 **어디까지 처리했는지** 남아 다음 실행이 이어받는가
"""

from typing import Any

import httpx
import pytest
import respx

from core.geocoder.kakao import KakaoClient, KakaoQuotaExceededError
from core.geocoder.resolver import GeocodeTarget, resolve_many, resolve_one

SEARCH_URL = "https://dapi.kakao.com/v2/local/search/address.json"

JIBUN = "서울특별시 서대문구 창천동 72-22"
DISTRICT = "서울특별시 서대문구 창천동"


def hit(lat: float = 37.55, lng: float = 126.93) -> httpx.Response:
    return httpx.Response(200, json={
        "documents": [{"address_name": "맞춘 주소", "address_type": "REGION_ADDR",
                       "x": str(lng), "y": str(lat)}],
        "meta": {"total_count": 1},
    })


def miss() -> httpx.Response:
    return httpx.Response(200, json={"documents": [], "meta": {"total_count": 0}})


def target(mng: str = "T-1", **overrides: Any) -> GeocodeTarget:
    values: dict[str, Any] = {
        "key": (mng, "1"), "road_addr": None, "jibun_addr": JIBUN,
        "sd_nm": "서울특별시", "sgg_nm": "서대문구", "emd_nm": "창천동", **overrides,
    }
    return GeocodeTarget(**values)


@pytest.fixture
async def client() -> KakaoClient:
    async def no_sleep(_seconds: float) -> None:
        return None

    return KakaoClient(rest_api_key="test-key", rate_per_sec=0, sleep_fn=no_sleep)


def query_of(call: Any) -> str:
    return str(call.request.url.params.get("query"))


# ── 폴백 전환 ──────────────────────────────────────────────────────────


@respx.mock
async def test_empty_response_advances_the_ladder(client: KakaoClient) -> None:
    """지번이 0건이면 읍면동 조합으로 내려간다 — 근사 좌표라도 얻는 편이 낫다."""
    route = respx.get(SEARCH_URL).mock(side_effect=[miss(), hit()])

    async with client:
        result = await resolve_one(target(), kakao=client, cached={})

    assert [query_of(c) for c in route.calls] == [JIBUN, DISTRICT]
    assert (result.level, result.status) == ("dong_center", "approx")


@respx.mock
async def test_first_hit_stops_the_ladder(client: KakaoClient) -> None:
    """지번에서 맞으면 그 아래 단계는 호출하지 않는다 — 쿼터를 아낀다."""
    route = respx.get(SEARCH_URL).mock(return_value=hit())

    async with client:
        result = await resolve_one(target(), kakao=client, cached={})

    assert route.call_count == 1
    assert result.level == "jibun"


@respx.mock
async def test_transient_error_does_not_advance_the_ladder(client: KakaoClient) -> None:
    """5xx 는 '이 주소가 없다' 가 아니다. 같은 주소를 재시도해야 한다."""
    route = respx.get(SEARCH_URL).mock(side_effect=[httpx.Response(503), hit()])

    async with client:
        result = await resolve_one(target(), kakao=client, cached={})

    assert [query_of(c) for c in route.calls] == [JIBUN, JIBUN]
    assert result.level == "jibun"


# ── 429 중단·재개 (F3.3) ───────────────────────────────────────────────


@respx.mock
async def test_quota_breaks_through_the_ladder(client: KakaoClient) -> None:
    """사다리가 429 를 삼키고 다음 단계를 시도하면 공유 앱을 더 세게 두드린다."""
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))

    async with client:
        with pytest.raises(KakaoQuotaExceededError):
            await resolve_one(target(), kakao=client, cached={})

    assert route.call_count == 1


@respx.mock
async def test_quota_stops_the_remaining_targets(client: KakaoClient) -> None:
    """두 번째 대상에서 끊기면 그 뒤는 손대지 않는다 — 다음 실행이 이어받는다.

    주소를 서로 다르게 준다. 같은 지번이면 캐시에 걸려 호출 자체가 없다.
    """
    route = respx.get(SEARCH_URL).mock(side_effect=[hit(), httpx.Response(429)])
    targets = [
        target("T-1", jibun_addr=JIBUN),
        target("T-2", jibun_addr="서울특별시 서대문구 연희동 1-1"),
        target("T-3", jibun_addr="서울특별시 서대문구 홍은동 2-2"),
    ]

    async with client:
        with pytest.raises(KakaoQuotaExceededError):
            await resolve_many(targets, kakao=client, cached={})

    # 세 번째 대상은 시도조차 하지 않는다.
    assert route.call_count == 2


@respx.mock
async def test_work_before_the_quota_is_not_wasted(client: KakaoClient) -> None:
    """패스 계층이 중간 결과를 살릴 수 있도록, 끊기기 전 호출은 캐시에 남을 값을 만든다."""
    respx.get(SEARCH_URL).mock(side_effect=[hit(), httpx.Response(429)])

    async with client:
        first = await resolve_one(target("T-1"), kakao=client, cached={})
        with pytest.raises(KakaoQuotaExceededError):
            await resolve_one(
                target("T-2", jibun_addr="서울특별시 서대문구 연희동 1-1"),
                kakao=client, cached={})

    assert first.is_located


# ── 캐시 (F3.2) ────────────────────────────────────────────────────────


@respx.mock
async def test_cached_address_makes_no_http_call(client: KakaoClient) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=hit())
    cached = {JIBUN: (37.1, 127.1, "kakao", "jibun")}

    async with client:
        result = await resolve_one(target(), kakao=client, cached=cached)

    assert route.call_count == 0
    assert result.from_cache


@respx.mock
async def test_repeated_address_is_asked_once_per_pass(client: KakaoClient) -> None:
    """한 지번에 여러 물건이 걸려 있다 — 실측 6,902건의 고유 주소가 801개다."""
    route = respx.get(SEARCH_URL).mock(return_value=hit())

    async with client:
        await resolve_many([target("T-1"), target("T-2"), target("T-3")],
                           kakao=client, cached={})

    assert route.call_count == 1
