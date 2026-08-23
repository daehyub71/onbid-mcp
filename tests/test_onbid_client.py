"""온비드 HTTP 클라이언트 테스트 (SPEC F1.4·§6.4·§6.4.1).

네트워크를 타지 않는다. httpx 호출은 respx로 가로채고, 시간은 주입한 가짜 함수로 제어한다.
"""

import urllib.parse
from typing import Any

import httpx
import pytest
import respx

from core.onbid.client import (
    OnbidApiError,
    OnbidAuthError,
    OnbidClient,
    OnbidQuotaExceededError,
    RateLimiter,
    extract_result,
)
from core.onbid.endpoints import ENDPOINTS

# 포털이 보여주는 Encoding 표현과 그 원본
ENCODED_KEY = "abc%2Bdef%2Fghi%3D%3D"
DECODED_KEY = "abc+def/ghi=="

LIST_URL = ENDPOINTS["realestate_list"].url
LIST_ARGS: dict[str, Any] = {"prptDivCd": "0007", "pvctTrgtYn": "N", "pageNo": 1, "numOfRows": 10}


def ok_body(count: int = 1) -> dict[str, Any]:
    return {
        "header": {"resultCode": "00", "resultMsg": "NORMAL_CODE"},
        "body": {"items": {"item": [{"cltrMngNo": "x"}] * count},
                 "totalCount": count, "pageNo": 1, "numOfRows": 10},
    }


def service_body(code: str, msg: str = "ERR") -> dict[str, Any]:
    return {"header": {"resultCode": code, "resultMsg": msg}}


def gateway_body(code: str = "12", msg: str = "NO_OPENAPI_SERVICE_ERROR") -> dict[str, Any]:
    return {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {
        "errMsg": msg, "returnAuthMsg": "해당 오픈API 서비스가 없거나 폐기됨",
        "returnReasonCode": code}}}


class FakeClock:
    """단조 시계와 sleep 을 대신한다. sleep 은 실제로 기다리지 않고 시계만 앞당긴다."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(clock: FakeClock, **kwargs: Any) -> OnbidClient:
    return OnbidClient(
        service_key=kwargs.pop("service_key", DECODED_KEY),
        rate_limiter=kwargs.pop("rate_limiter", RateLimiter(0, time_fn=clock.time,
                                                            sleep_fn=clock.sleep)),
        sleep_fn=clock.sleep,
        **kwargs,
    )


# ── 서비스키 정규화 (SPEC §6.4) ──────────────────────────────────────────


@respx.mock
async def test_client_unquotes_encoded_service_key() -> None:
    """포털의 Encoding 키를 받아도 원본으로 되돌려 보낸다. URL 수동 조립을 하지 않으므로
    httpx가 다시 인코딩한다 — 이중 인코딩(%252B) 함정 회피."""
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=ok_body()))
    clock = FakeClock()
    async with make_client(clock, service_key=ENCODED_KEY) as client:
        await client.call("realestate_list", **LIST_ARGS)

    sent = dict(urllib.parse.parse_qsl(str(route.calls[0].request.url.query, "utf-8")))
    assert sent["serviceKey"] == DECODED_KEY


@respx.mock
async def test_client_unquote_is_idempotent_for_decoded_key() -> None:
    """이미 원본인 키를 다시 unquote 해도 값이 변하지 않는다 (서비스키에 %가 없다)."""
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=ok_body()))
    clock = FakeClock()
    async with make_client(clock, service_key=DECODED_KEY) as client:
        await client.call("realestate_list", **LIST_ARGS)

    sent = dict(urllib.parse.parse_qsl(str(route.calls[0].request.url.query, "utf-8")))
    assert sent["serviceKey"] == DECODED_KEY


def test_client_rejects_empty_service_key() -> None:
    with pytest.raises(ValueError, match="service_key"):
        OnbidClient(service_key="")


# ── 기본 파라미터 ────────────────────────────────────────────────────────


@respx.mock
async def test_client_injects_result_type_json() -> None:
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=ok_body()))
    clock = FakeClock()
    async with make_client(clock) as client:
        await client.call("realestate_list", **LIST_ARGS)

    sent = dict(urllib.parse.parse_qsl(str(route.calls[0].request.url.query, "utf-8")))
    assert sent["resultType"] == "json"


@respx.mock
async def test_client_caller_can_override_result_type() -> None:
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=ok_body()))
    clock = FakeClock()
    async with make_client(clock) as client:
        await client.call("realestate_list", resultType="xml", **LIST_ARGS)

    sent = dict(urllib.parse.parse_qsl(str(route.calls[0].request.url.query, "utf-8")))
    assert sent["resultType"] == "xml"


async def test_client_rejects_missing_required_param_before_request() -> None:
    """필수 파라미터가 빠지면 네트워크를 타기 전에 막는다 (쿼터 보호)."""
    clock = FakeClock()
    async with make_client(clock) as client:
        with pytest.raises(ValueError, match="pvctTrgtYn"):
            await client.call("realestate_list", prptDivCd="0007", pageNo=1, numOfRows=10)


async def test_client_rejects_unknown_endpoint() -> None:
    clock = FakeClock()
    async with make_client(clock) as client:
        with pytest.raises(KeyError):
            await client.call("no_such_endpoint")


# ── 응답 봉투 3종 (SPEC §6.4.1) ──────────────────────────────────────────


def test_extract_result_reads_service_envelope() -> None:
    assert extract_result(service_body("00", "NORMAL_CODE")) == ("00", "NORMAL_CODE")


def test_extract_result_reads_gateway_envelope() -> None:
    """게이트웨이 오류는 returnReasonCode 를 resultCode 와 동일 체계로 취급한다."""
    code, msg = extract_result(gateway_body("12"))
    assert code == "12"
    assert "NO_OPENAPI_SERVICE_ERROR" in msg


def test_extract_result_reads_result_envelope() -> None:
    """입찰정보 서비스의 NODATA_ERROR 는 세 번째 형식으로 온다."""
    body = {"result": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}}
    assert extract_result(body) == ("03", "NODATA_ERROR")


def test_extract_result_returns_none_for_unknown_shape() -> None:
    assert extract_result({"unexpected": 1}) == (None, "")


@respx.mock
async def test_client_returns_response_on_success() -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=ok_body(3)))
    clock = FakeClock()
    async with make_client(clock) as client:
        resp = await client.call("realestate_list", **LIST_ARGS)

    assert resp.result_code == "00"
    assert resp.payload["body"]["totalCount"] == 3
    assert not resp.is_no_data


@respx.mock
async def test_client_no_data_is_not_an_error() -> None:
    """03 은 '조건에 맞는 데이터가 없음'이지 실패가 아니다. 예외를 던지지 않는다."""
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(200, json={"result": {"resultCode": "03",
                                                          "resultMsg": "NODATA_ERROR"}}))
    clock = FakeClock()
    async with make_client(clock) as client:
        resp = await client.call("realestate_list", **LIST_ARGS)

    assert resp.is_no_data


@respx.mock
async def test_client_raises_on_http_200_with_error_code() -> None:
    """공공데이터포털은 HTTP 200 에 오류를 담아 보낸다. 상태코드만 보면 안 된다."""
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(200, json=service_body("10",
                                                           "INVALID_REQUEST_PARAMETER_ERROR")))
    clock = FakeClock()
    async with make_client(clock) as client:
        with pytest.raises(OnbidApiError) as exc:
            await client.call("realestate_list", **LIST_ARGS)

    assert exc.value.result_code == "10"


@respx.mock
async def test_client_raises_on_gateway_error_with_http_400() -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(400, json=gateway_body("12")))
    clock = FakeClock()
    async with make_client(clock) as client:
        with pytest.raises(OnbidApiError) as exc:
            await client.call("realestate_list", **LIST_ARGS)

    assert exc.value.result_code == "12"


# ── 재시도 정책 (SPEC F1.4) ──────────────────────────────────────────────


@respx.mock
async def test_client_retries_transient_server_error_then_succeeds() -> None:
    respx.get(LIST_URL).mock(side_effect=[
        httpx.Response(500),
        httpx.Response(200, json=ok_body()),
    ])
    clock = FakeClock()
    async with make_client(clock) as client:
        resp = await client.call("realestate_list", **LIST_ARGS)

    assert resp.result_code == "00"
    assert len(clock.slept) == 1


@respx.mock
async def test_client_retries_transient_result_code() -> None:
    """02 DB_ERROR 같은 일시적 서버 오류는 재시도한다."""
    respx.get(LIST_URL).mock(side_effect=[
        httpx.Response(200, json=service_body("02", "DB_ERROR")),
        httpx.Response(200, json=ok_body()),
    ])
    clock = FakeClock()
    async with make_client(clock) as client:
        resp = await client.call("realestate_list", **LIST_ARGS)

    assert resp.result_code == "00"


@respx.mock
async def test_client_gives_up_after_max_attempts() -> None:
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(503))
    clock = FakeClock()
    async with make_client(clock, max_attempts=3) as client:
        with pytest.raises(OnbidApiError):
            await client.call("realestate_list", **LIST_ARGS)

    assert route.call_count == 3
    assert len(clock.slept) == 2  # 시도 사이에만 대기


@respx.mock
async def test_client_backoff_is_exponential() -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(500))
    clock = FakeClock()
    async with make_client(clock, max_attempts=4, backoff_base=0.5) as client:
        with pytest.raises(OnbidApiError):
            await client.call("realestate_list", **LIST_ARGS)

    assert clock.slept == [0.5, 1.0, 2.0]


@respx.mock
async def test_client_retries_on_timeout() -> None:
    respx.get(LIST_URL).mock(side_effect=[
        httpx.ConnectTimeout("timeout"),
        httpx.Response(200, json=ok_body()),
    ])
    clock = FakeClock()
    async with make_client(clock) as client:
        resp = await client.call("realestate_list", **LIST_ARGS)

    assert resp.result_code == "00"


@respx.mock
async def test_client_does_not_retry_quota_exceeded() -> None:
    """22 는 재시도해도 소용없고 오히려 상황을 악화시킨다."""
    route = respx.get(LIST_URL).mock(
        return_value=httpx.Response(200, json=service_body("22", "LIMITED_NUMBER")))
    clock = FakeClock()
    async with make_client(clock) as client:
        with pytest.raises(OnbidQuotaExceededError):
            await client.call("realestate_list", **LIST_ARGS)

    assert route.call_count == 1
    assert clock.slept == []


@pytest.mark.parametrize("code", ["20", "21", "30", "31", "32", "33"])
@respx.mock
async def test_client_does_not_retry_auth_errors(code: str) -> None:
    route = respx.get(LIST_URL).mock(
        return_value=httpx.Response(200, json=service_body(code, "KEY_PROBLEM")))
    clock = FakeClock()
    async with make_client(clock) as client:
        with pytest.raises(OnbidAuthError):
            await client.call("realestate_list", **LIST_ARGS)

    assert route.call_count == 1


@respx.mock
async def test_client_does_not_retry_bad_path() -> None:
    """12 는 경로가 틀린 것이라 재시도로 해결되지 않는다."""
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(400, json=gateway_body("12")))
    clock = FakeClock()
    async with make_client(clock) as client:
        with pytest.raises(OnbidApiError):
            await client.call("realestate_list", **LIST_ARGS)

    assert route.call_count == 1


@respx.mock
async def test_client_does_not_retry_invalid_param() -> None:
    route = respx.get(LIST_URL).mock(
        return_value=httpx.Response(200, json=service_body("11", "NO_MANDATORY")))
    clock = FakeClock()
    async with make_client(clock) as client:
        with pytest.raises(OnbidApiError):
            await client.call("realestate_list", **LIST_ARGS)

    assert route.call_count == 1


# ── 유량 제어 (10 TPS) ───────────────────────────────────────────────────


async def test_rate_limiter_first_acquire_does_not_wait() -> None:
    clock = FakeClock()
    limiter = RateLimiter(10, time_fn=clock.time, sleep_fn=clock.sleep)
    await limiter.acquire()
    assert clock.slept == []


async def test_rate_limiter_enforces_minimum_interval() -> None:
    """10 TPS = 호출 간 최소 0.1초."""
    clock = FakeClock()
    limiter = RateLimiter(10, time_fn=clock.time, sleep_fn=clock.sleep)
    await limiter.acquire()
    await limiter.acquire()
    assert clock.slept == [pytest.approx(0.1)]


async def test_rate_limiter_does_not_wait_when_enough_time_passed() -> None:
    clock = FakeClock()
    limiter = RateLimiter(10, time_fn=clock.time, sleep_fn=clock.sleep)
    await limiter.acquire()
    clock.now += 5.0
    await limiter.acquire()
    assert clock.slept == []


async def test_rate_limiter_zero_rate_disables_throttling() -> None:
    clock = FakeClock()
    limiter = RateLimiter(0, time_fn=clock.time, sleep_fn=clock.sleep)
    for _ in range(5):
        await limiter.acquire()
    assert clock.slept == []


@respx.mock
async def test_client_applies_rate_limiter_per_request() -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=ok_body()))
    clock = FakeClock()
    limiter = RateLimiter(10, time_fn=clock.time, sleep_fn=clock.sleep)
    async with make_client(clock, rate_limiter=limiter) as client:
        await client.call("realestate_list", **LIST_ARGS)
        await client.call("realestate_list", **LIST_ARGS)

    assert clock.slept == [pytest.approx(0.1)]


@respx.mock
async def test_client_default_rate_is_ten_per_second() -> None:
    """온비드 가이드가 명시한 초당 최대 트랜잭션."""
    client = OnbidClient(service_key=DECODED_KEY)
    assert client.rate_limiter.rate_per_sec == 10
    await client.aclose()


# ── 커넥션 재사용 (SPEC N1.3) ────────────────────────────────────────────


@respx.mock
async def test_client_reuses_single_http_connection() -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=ok_body()))
    clock = FakeClock()
    async with make_client(clock) as client:
        transport_before = client.http
        await client.call("realestate_list", **LIST_ARGS)
        await client.call("realestate_list", **LIST_ARGS)
        assert client.http is transport_before


async def test_client_closes_http_on_exit() -> None:
    clock = FakeClock()
    client = make_client(clock)
    async with client:
        pass
    assert client.http.is_closed
