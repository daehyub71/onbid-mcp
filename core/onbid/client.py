"""온비드 OpenAPI HTTP 클라이언트 (SPEC F1.4·N1.3·§6.4·§6.4.1).

이 모듈이 흡수하는 온비드의 특이사항:

1. **서비스키 표현이 두 가지다.** 포털이 Encoding/Decoding 으로 나눠 보여주지만 같은 키다.
   어느 쪽을 받아도 ``unquote`` 로 정규화한 뒤 httpx 의 ``params=`` 로 넘긴다.
   URL 을 손으로 조립하면 Encoding 키가 이중 인코딩(``%2B`` → ``%252B``)되어 인증에 실패한다.
2. **HTTP 200 에 오류가 실려 온다.** 상태코드만 보고 성공으로 판정하면 안 된다.
3. **오류 봉투가 세 가지다.** ``header`` / ``OpenAPI_ServiceResponse.cmmMsgHeader`` / ``result``.
4. **초당 최대 10 트랜잭션**이라 유량 제어가 필요하다.
"""

import asyncio
import logging
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from types import TracebackType
from typing import Any, Final, Self

import httpx

from core.onbid.endpoints import ENDPOINTS, Endpoint

# 온비드는 인증키를 **쿼리 파라미터**로 요구한다. httpx 는 INFO 레벨에서 요청 URL을 통째로
# 기록하므로, 애플리케이션이 로깅을 켜는 순간 키가 로그에 남는다 (N4.3·AC11):
#
#     INFO:httpx:HTTP Request: GET https://...&serviceKey=... "HTTP/1.1 200 OK"
#
# 예외도 경고도 없이 조용히 샌다 — 실제로 첫 실적재(2026-08-23)에서 터미널에 찍혔고 보안
# 점검에서 발견했다. 키가 URL 에 들어간다는 사실을 아는 이 모듈에서 막는다.
# 디버깅이 필요하면 호출자가 되돌릴 수 있다: ``logging.getLogger("httpx").setLevel(INFO)``
logging.getLogger("httpx").setLevel(logging.WARNING)

DEFAULT_RATE_PER_SEC: Final = 10.0
"""온비드 활용가이드가 명시한 초당 최대 트랜잭션."""

DEFAULT_TIMEOUT: Final = 30.0
DEFAULT_MAX_ATTEMPTS: Final = 3
DEFAULT_BACKOFF_BASE: Final = 0.5

NO_DATA_CODE: Final = "03"
"""NODATA_ERROR. 조건에 맞는 데이터가 없을 뿐 실패가 아니다."""

QUOTA_CODE: Final = "22"
"""LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR."""

AUTH_CODES: Final = frozenset({"20", "21", "30", "31", "32", "33"})
"""접근거부·키 미등록·기한만료·IP 미등록 등. 재시도로 해결되지 않는다."""

RETRYABLE_CODES: Final = frozenset({"01", "02", "04", "05", "99"})
"""일시적 서버·DB·타임아웃 오류. ``12``(경로 오류)와 ``10``·``11``(파라미터 오류)은 제외한다 —
재시도해도 결과가 같고 쿼터만 소모한다."""


class OnbidError(Exception):
    """온비드 호출 관련 오류의 최상위."""


class OnbidApiError(OnbidError):
    """온비드가 오류 결과코드를 반환했거나 HTTP 통신에 실패했다.

    Attributes:
        result_code: 온비드 결과코드. HTTP 계층 실패면 ``None``.
        result_msg: 결과 메시지 또는 예외 설명.
        status_code: HTTP 상태코드.
    """

    def __init__(
        self,
        result_code: str | None,
        result_msg: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.result_code = result_code
        self.result_msg = result_msg
        self.status_code = status_code
        super().__init__(f"[{result_code or 'HTTP'}] {result_msg}")


class OnbidQuotaExceededError(OnbidApiError):
    """일일 호출 한도를 소진했다. 재시도하지 않고 배치를 중단해야 한다."""


class OnbidAuthError(OnbidApiError):
    """서비스키 문제. 재시도가 아니라 키 점검이 필요하다."""


def extract_result(payload: Any) -> tuple[str | None, str]:
    """응답에서 (결과코드, 메시지)를 뽑는다. 봉투 세 종류를 모두 처리한다 (SPEC §6.4.1).

    Args:
        payload: 파싱된 JSON 응답.

    Returns:
        결과코드와 메시지. 알 수 없는 형식이면 ``(None, "")``.
    """
    if not isinstance(payload, dict):
        return None, ""

    gateway = payload.get("OpenAPI_ServiceResponse")
    if isinstance(gateway, dict):
        head = gateway.get("cmmMsgHeader", {})
        if isinstance(head, dict):
            code = head.get("returnReasonCode")
            msg = head.get("errMsg") or head.get("returnAuthMsg") or ""
            return (str(code) if code is not None else None), str(msg)

    for envelope in ("header", "result"):
        block = payload.get(envelope)
        if isinstance(block, dict) and "resultCode" in block:
            return str(block["resultCode"]), str(block.get("resultMsg", ""))

    return None, ""


@dataclass(frozen=True, slots=True)
class OnbidResponse:
    """성공 응답 한 건.

    Attributes:
        result_code: 온비드 결과코드 (``00`` 또는 ``03``).
        result_msg: 결과 메시지.
        payload: 파싱된 전체 응답. 항목 추출은 파서의 몫이다.
    """

    result_code: str
    result_msg: str
    payload: dict[str, Any]

    @property
    def is_no_data(self) -> bool:
        """조건에 맞는 데이터가 없는 응답인지 여부."""
        return self.result_code == NO_DATA_CODE


class RateLimiter:
    """호출 간 최소 간격을 보장하는 유량 제어기.

    버스트를 허용하지 않고 균등하게 흘려보낸다. 온비드처럼 초당 상한이 명시된 API에서는
    토큰 버킷보다 예측 가능해 배치 소요 시간을 계산하기 쉽다.

    Args:
        rate_per_sec: 초당 허용 호출 수. ``0`` 이하면 제어하지 않는다.
        time_fn: 단조 시계. 테스트에서 주입한다.
        sleep_fn: 대기 함수. 테스트에서 주입한다.
    """

    def __init__(
        self,
        rate_per_sec: float,
        *,
        time_fn: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rate_per_sec = rate_per_sec
        self._interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._time = time_fn
        self._sleep = sleep_fn
        self._next_allowed: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """다음 호출이 허용될 때까지 대기한다."""
        if self._interval <= 0:
            return
        async with self._lock:
            now = self._time()
            if self._next_allowed is not None and now < self._next_allowed:
                await self._sleep(self._next_allowed - now)
                now = self._time()
            self._next_allowed = now + self._interval


class OnbidClient:
    """온비드 OpenAPI 호출기.

    커넥션을 재사용하므로 배치·MCP 모두 인스턴스를 하나 만들어 오래 쓴다 (N1.3).

    Args:
        service_key: 포털에서 발급받은 일반 인증키. Encoding/Decoding 어느 쪽이든 된다.
        rate_limiter: 유량 제어기. 생략하면 10 TPS.
        max_attempts: 재시도 포함 최대 시도 횟수.
        backoff_base: 지수 백오프의 첫 대기 시간(초).
        timeout: 요청 타임아웃(초).
        sleep_fn: 백오프 대기 함수. 테스트에서 주입한다.
        http: 외부에서 만든 httpx 클라이언트. 생략하면 내부에서 만든다.
    """

    def __init__(
        self,
        service_key: str,
        *,
        rate_limiter: RateLimiter | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not service_key or not service_key.strip():
            raise ValueError("service_key 가 비어 있다 (.env 의 ONBID_SERVICE_KEY 확인)")
        # Encoding/Decoding 어느 쪽이 와도 원본으로 되돌린다. 서비스키에 %가 없어 멱등하다.
        self.service_key = urllib.parse.unquote(service_key.strip())
        self.rate_limiter = rate_limiter or RateLimiter(DEFAULT_RATE_PER_SEC)
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self._sleep = sleep_fn
        self.http = http or httpx.AsyncClient(
            timeout=timeout, headers={"Accept": "application/json"}
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """HTTP 커넥션을 정리한다."""
        await self.http.aclose()

    async def call(self, endpoint_name: str, **params: Any) -> OnbidResponse:
        """엔드포인트를 호출한다.

        Args:
            endpoint_name: `ENDPOINTS` 의 키.
            **params: 오퍼레이션 파라미터. ``serviceKey`` 와 ``resultType`` 은 자동으로 채운다.

        Returns:
            성공 응답. 결과코드 ``03``(데이터 없음)도 성공으로 본다.

        Raises:
            KeyError: 알 수 없는 엔드포인트.
            ValueError: 필수 파라미터가 빠졌다.
            OnbidQuotaExceededError: 일일 한도 소진.
            OnbidAuthError: 서비스키 문제.
            OnbidApiError: 그 밖의 오류 또는 재시도 소진.
        """
        endpoint = ENDPOINTS[endpoint_name]
        request_params = self._build_params(endpoint, params)
        return await self._request_with_retry(endpoint, request_params)

    def _build_params(self, endpoint: Endpoint, params: Mapping[str, Any]) -> dict[str, Any]:
        """기본값을 채우고 필수 파라미터를 검증한다.

        네트워크를 타기 전에 막아 쿼터를 아낀다.
        """
        merged: dict[str, Any] = {"resultType": "json", **params}
        merged["serviceKey"] = self.service_key
        missing = endpoint.missing_params(merged)
        if missing:
            raise ValueError(
                f"{endpoint.operation}: 필수 파라미터 누락 {sorted(missing)}"
            )
        return {k: v for k, v in merged.items() if v is not None}

    async def _request_with_retry(
        self, endpoint: Endpoint, params: dict[str, Any]
    ) -> OnbidResponse:
        last: OnbidApiError | None = None
        for attempt in range(1, self.max_attempts + 1):
            await self.rate_limiter.acquire()
            try:
                return self._to_response(await self.http.get(endpoint.url, params=params))
            except (OnbidQuotaExceededError, OnbidAuthError):
                raise
            except OnbidApiError as exc:
                if exc.result_code is not None and exc.result_code not in RETRYABLE_CODES:
                    raise
                last = exc
            except httpx.HTTPError as exc:
                last = OnbidApiError(None, f"{type(exc).__name__}: {exc}")

            if attempt < self.max_attempts:
                await self._sleep(self.backoff_base * 2 ** (attempt - 1))

        assert last is not None  # noqa: S101 — 루프가 최소 1회 돌므로 항상 채워진다
        raise last

    def _to_response(self, response: httpx.Response) -> OnbidResponse:
        """HTTP 응답을 결과코드 기준으로 해석한다.

        HTTP 상태코드가 아니라 **본문의 결과코드**가 판정 기준이다 (SPEC §6.4.1).
        """
        try:
            payload = response.json()
        except ValueError:
            raise OnbidApiError(
                None,
                f"JSON 이 아닌 응답: {response.text[:200]}",
                status_code=response.status_code,
            ) from None

        code, msg = extract_result(payload)

        if code == QUOTA_CODE:
            raise OnbidQuotaExceededError(code, msg, status_code=response.status_code)
        if code in AUTH_CODES:
            raise OnbidAuthError(code, msg, status_code=response.status_code)
        if code in ("00", NO_DATA_CODE):
            return OnbidResponse(result_code=code, result_msg=msg, payload=payload)
        if code is not None:
            raise OnbidApiError(code, msg, status_code=response.status_code)

        # 결과코드를 못 읽었다면 HTTP 상태로 판정한다.
        if response.is_success:
            raise OnbidApiError(
                None, f"결과코드를 해석할 수 없는 응답: {str(payload)[:200]}",
                status_code=response.status_code,
            )
        raise OnbidApiError(
            None, f"HTTP {response.status_code}", status_code=response.status_code
        )
