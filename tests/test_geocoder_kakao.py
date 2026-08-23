"""카카오 로컬 클라이언트 테스트 (F3.3·F3.4·F3.5).

**카카오 앱을 다른 프로젝트와 공유한다.** 그래서 429(쿼터 소진)는 우리만의 문제가 아니다 —
계속 두드리면 남의 서비스까지 막는다. 감지 즉시 **전체를 중단**하고 위치를 남겨 다음 실행이
이어받는다.

일시적 오류(5xx·타임아웃)와는 다르게 다뤄야 한다. 5xx 는 재시도하면 통하지만, 429 를
재시도하는 것은 문을 더 세게 두드리는 일이다.
"""

import httpx
import pytest
import respx

from core.geocoder.kakao import (
    KakaoAuthError,
    KakaoClient,
    KakaoQuotaExceededError,
)

SEARCH_URL = "https://dapi.kakao.com/v2/local/search/address.json"


def found(lat: float = 37.4783, lng: float = 127.0454) -> httpx.Response:
    return httpx.Response(200, json={
        "documents": [{
            "address_name": "서울 강남구 개포동 12-3",
            "address_type": "REGION_ADDR",
            "x": str(lng), "y": str(lat),
        }],
        "meta": {"total_count": 1},
    })


def empty() -> httpx.Response:
    return httpx.Response(200, json={"documents": [], "meta": {"total_count": 0}})


@pytest.fixture
async def client() -> KakaoClient:
    """대기를 주입해 백오프가 실제로 잠들지 않게 한다 — 테스트가 느려지면 안 돌린다."""

    async def no_sleep(_seconds: float) -> None:
        return None

    return KakaoClient(rest_api_key="test-key", rate_per_sec=0, sleep_fn=no_sleep)


# ── 정상 조회 ──────────────────────────────────────────────────────────


@respx.mock
async def test_search_returns_a_point(client: KakaoClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=found())

    async with client:
        point = await client.search("서울특별시 강남구 개포동 12-3")

    assert point is not None
    assert point.lat == pytest.approx(37.4783)
    assert point.lng == pytest.approx(127.0454)


@respx.mock
async def test_search_reports_the_matched_type(client: KakaoClient) -> None:
    """도로명으로 맞았는지 지번으로 맞았는지에 따라 기록할 level 이 갈린다 (F3.6)."""
    respx.get(SEARCH_URL).mock(return_value=found())

    async with client:
        point = await client.search("서울특별시 강남구 개포동 12-3")

    assert point is not None
    assert point.address_type == "REGION_ADDR"


@respx.mock
async def test_search_returns_none_when_not_found(client: KakaoClient) -> None:
    """결과 없음은 실패가 아니다 — 다음 폴백 단계로 넘어갈 신호다."""
    respx.get(SEARCH_URL).mock(return_value=empty())

    async with client:
        assert await client.search("꼬리표 붙은 주소 외 2필지") is None


@respx.mock
async def test_search_skips_blank_input(client: KakaoClient) -> None:
    """빈 주소로 호출하면 쿼터만 쓴다."""
    route = respx.get(SEARCH_URL).mock(return_value=found())

    async with client:
        assert await client.search("   ") is None

    assert route.call_count == 0


@respx.mock
async def test_key_travels_in_the_header(client: KakaoClient) -> None:
    """URL 이 아니라 헤더로 보낸다 — 요청 URL 은 로그에 남는다 (N4.1)."""
    route = respx.get(SEARCH_URL).mock(return_value=found())

    async with client:
        await client.search("서울특별시 강남구 개포동 12-3")

    request = route.calls[0].request
    assert request.headers["Authorization"] == "KakaoAK test-key"
    assert "test-key" not in str(request.url)


# ── 429 즉시 중단 (F3.3) ───────────────────────────────────────────────


@respx.mock
async def test_quota_exceeded_aborts_immediately(client: KakaoClient) -> None:
    """재시도하면 남의 프로젝트 쿼터까지 태운다."""
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))

    async with client:
        with pytest.raises(KakaoQuotaExceededError):
            await client.search("서울특별시 강남구 개포동 12-3")

    assert route.call_count == 1


@respx.mock
async def test_auth_error_aborts_immediately(client: KakaoClient) -> None:
    """키 문제는 재시도로 풀리지 않는다."""
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(401))

    async with client:
        with pytest.raises(KakaoAuthError):
            await client.search("서울특별시 강남구 개포동 12-3")

    assert route.call_count == 1


# ── 일시 오류 재시도 (F3.4) ────────────────────────────────────────────


@respx.mock
async def test_server_error_is_retried(client: KakaoClient) -> None:
    route = respx.get(SEARCH_URL).mock(side_effect=[
        httpx.Response(503), httpx.Response(503), found(),
    ])

    async with client:
        point = await client.search("서울특별시 강남구 개포동 12-3")

    assert point is not None
    assert route.call_count == 3


@respx.mock
async def test_retries_are_bounded(client: KakaoClient) -> None:
    """무한 재시도는 배치를 멈춰 세운다."""
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))

    async with client:
        with pytest.raises(Exception, match="503|재시도"):
            await client.search("서울특별시 강남구 개포동 12-3")

    assert route.call_count == 3


@respx.mock
async def test_timeout_is_retried(client: KakaoClient) -> None:
    route = respx.get(SEARCH_URL).mock(side_effect=[
        httpx.ConnectTimeout("timeout"), found(),
    ])

    async with client:
        assert await client.search("서울특별시 강남구 개포동 12-3") is not None

    assert route.call_count == 2


# ── 호출량 (F3.5) ──────────────────────────────────────────────────────


@respx.mock
async def test_calls_are_counted(client: KakaoClient) -> None:
    """카카오 앱을 공유하므로 얼마나 썼는지 알아야 한다."""
    respx.get(SEARCH_URL).mock(return_value=found())

    async with client:
        await client.search("주소1")
        await client.search("주소2")

    assert client.call_count == 2


@respx.mock
async def test_retries_count_toward_usage(client: KakaoClient) -> None:
    """재시도도 남의 쿼터를 쓴다 — 성공 건수만 세면 실제 사용량을 놓친다."""
    respx.get(SEARCH_URL).mock(side_effect=[httpx.Response(503), found()])

    async with client:
        await client.search("주소")

    assert client.call_count == 2


@respx.mock
async def test_blank_input_does_not_count(client: KakaoClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=found())

    async with client:
        await client.search("")

    assert client.call_count == 0


# ── 백오프 간격 (F3.4) ─────────────────────────────────────────────────


@respx.mock
async def test_backoff_grows_exponentially() -> None:
    """간격이 늘지 않으면 장애 중인 서버를 같은 속도로 계속 두드린다."""
    waited: list[float] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)

    client = KakaoClient(rest_api_key="test-key", rate_per_sec=0,
                         backoff_base=0.5, sleep_fn=record)
    respx.get(SEARCH_URL).mock(side_effect=[
        httpx.Response(503), httpx.Response(503), found(),
    ])

    async with client:
        await client.search("주소")

    assert waited == [0.5, 1.0]


@respx.mock
async def test_no_backoff_after_the_last_attempt() -> None:
    """마지막 시도 뒤의 대기는 아무 소용이 없다 — 배치만 느려진다."""
    waited: list[float] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)

    client = KakaoClient(rest_api_key="test-key", rate_per_sec=0,
                         max_attempts=3, sleep_fn=record)
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))

    async with client:
        with pytest.raises(Exception, match="503"):
            await client.search("주소")

    assert len(waited) == 2  # 3회 시도 사이의 간격은 2번뿐이다
