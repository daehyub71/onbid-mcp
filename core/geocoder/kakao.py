"""카카오 로컬 주소검색 클라이언트 (F3.3·F3.4·F3.5).

**이 앱은 다른 프로젝트와 공유된다.** 그래서 429(쿼터 소진)는 우리만의 문제가 아니다 —
계속 두드리면 남의 서비스까지 막는다. 감지 즉시 예외를 올려 **전체를 중단**하고, 호출자가
처리 위치를 기록해 다음 실행이 이어받는다 (F3.3).

일시적 오류(5xx·타임아웃)와는 다르게 다뤄야 한다. 5xx 는 재시도하면 통하지만 429 를
재시도하는 것은 문을 더 세게 두드리는 일이다. 키 문제(401·403)도 마찬가지로 재시도가 무의미하다.

**인증키는 헤더로 보낸다.** 온비드처럼 쿼리 파라미터에 실리면 요청 URL 로그에 그대로 남는다
(N4.1·N4.5 에서 겪은 문제).

호출 수는 재시도까지 포함해 센다 (F3.5) — 성공 건수만 세면 공유 앱의 실제 사용량을 놓친다.
"""

import asyncio
import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx

from core.onbid.client import RateLimiter

logger = logging.getLogger(__name__)

SEARCH_URL: Final = "https://dapi.kakao.com/v2/local/search/address.json"

DEFAULT_RATE_PER_SEC: Final = 10.0
"""공유 앱이라 여유를 두고 흘려보낸다."""

DEFAULT_TIMEOUT: Final = 10.0
DEFAULT_MAX_ATTEMPTS: Final = 3
DEFAULT_BACKOFF_BASE: Final = 0.5

QUOTA_STATUS: Final = 429
AUTH_STATUSES: Final = frozenset({401, 403})
RETRYABLE_STATUSES: Final = frozenset({500, 502, 503, 504})


class KakaoError(Exception):
    """카카오 로컬 API 관련 오류의 최상위."""


class KakaoApiError(KakaoError):
    """응답을 받았으나 실패했다.

    Attributes:
        status_code: HTTP 상태코드. 통신 실패면 None.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class KakaoQuotaExceededError(KakaoApiError):
    """일일 쿼터를 소진했다. **재시도하지 않고 배치를 중단해야 한다** (F3.3)."""


class KakaoAuthError(KakaoApiError):
    """키 문제. 재시도가 아니라 키 점검이 필요하다."""


@dataclass(frozen=True, slots=True)
class KakaoPoint:
    """주소검색 결과 한 건.

    Attributes:
        lat: 위도 (응답의 ``y``).
        lng: 경도 (응답의 ``x``).
        address_name: 카카오가 맞춘 주소 문자열.
        address_type: ``ROAD_ADDR`` | ``REGION_ADDR`` 등. 기록할 level 을 정할 때 쓴다.
    """

    lat: float
    lng: float
    address_name: str
    address_type: str


class KakaoClient:
    """카카오 로컬 주소검색 클라이언트.

    Args:
        rest_api_key: REST API 키. **지도 SDK 용 JavaScript 키가 아니다.**
        rate_per_sec: 초당 호출 상한. 0 이하면 제어하지 않는다.
        timeout: 요청 타임아웃(초).
        max_attempts: 일시 오류 재시도를 포함한 최대 시도 횟수.
        backoff_base: 지수 백오프 기준(초).
        client: 주입할 httpx 클라이언트. 테스트·재사용에 쓴다.
        sleep_fn: 대기 함수. 테스트에서 주입한다.
    """

    def __init__(
        self,
        rest_api_key: str,
        *,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        client: httpx.AsyncClient | None = None,
        sleep_fn: Any = asyncio.sleep,
    ) -> None:
        if not rest_api_key:
            raise ValueError("rest_api_key 가 비어 있다 (.env 의 KAKAO_REST_API_KEY 확인)")
        self._key = rest_api_key
        self._limiter = RateLimiter(rate_per_sec)
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep_fn
        self.call_count = 0
        """이번 실행에서 실제로 보낸 요청 수. **재시도를 포함한다** (F3.5)."""

    async def __aenter__(self) -> Self:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, address: str) -> KakaoPoint | None:
        """주소로 좌표를 찾는다.

        Args:
            address: 검색할 주소.

        Returns:
            좌표. **결과가 없으면 None** — 실패가 아니라 다음 폴백 단계로 넘어갈 신호다.

        Raises:
            KakaoQuotaExceededError: 429. 배치를 즉시 중단해야 한다.
            KakaoAuthError: 401·403. 키 점검이 필요하다.
            KakaoApiError: 재시도를 소진했거나 통신에 실패했다.
        """
        query = address.strip() if address else ""
        if not query:
            # 빈 주소로 부르면 결과 없이 쿼터만 쓴다.
            return None

        payload = await self._get({"query": query})
        documents = payload.get("documents") or []
        if not documents:
            return None

        return _to_point(documents[0])

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        """재시도를 포함해 한 번의 논리적 요청을 수행한다."""
        if self._client is None:
            raise RuntimeError("KakaoClient 를 async with 로 열어야 한다")

        last: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            await self._limiter.acquire()
            self.call_count += 1
            try:
                response = await self._client.get(
                    SEARCH_URL, params=params,
                    headers={"Authorization": f"KakaoAK {self._key}"},
                )
            except httpx.HTTPError as exc:
                last = KakaoApiError(f"통신 실패: {type(exc).__name__}")
                await self._backoff(attempt)
                continue

            status = response.status_code
            # 쿼터·키 문제는 재시도 대상이 아니다 — 즉시 올린다 (F3.3).
            if status == QUOTA_STATUS:
                raise KakaoQuotaExceededError(
                    "카카오 일일 쿼터 소진 — 배치를 중단한다", status_code=status)
            if status in AUTH_STATUSES:
                raise KakaoAuthError(
                    f"카카오 인증 실패 (HTTP {status}) — 키를 점검한다", status_code=status)

            if status in RETRYABLE_STATUSES:
                last = KakaoApiError(f"일시 오류 HTTP {status}", status_code=status)
                await self._backoff(attempt)
                continue

            if status != httpx.codes.OK:
                raise KakaoApiError(f"예상치 못한 응답 HTTP {status}", status_code=status)

            body: dict[str, Any] = response.json()
            return body

        assert last is not None  # 루프는 성공 아니면 last 를 채운다
        raise last

    async def _backoff(self, attempt: int) -> None:
        """마지막 시도가 아니면 지수 백오프로 기다린다 (F3.4)."""
        if attempt < self._max_attempts:
            await self._sleep(self._backoff_base * (2 ** (attempt - 1)))


def _to_point(document: dict[str, Any]) -> KakaoPoint | None:
    """응답 문서를 좌표로 바꾼다. 좌표가 없으면 None."""
    try:
        lat = float(document["y"])
        lng = float(document["x"])
    except (KeyError, TypeError, ValueError):
        logger.warning("카카오 응답에 좌표가 없다")
        return None

    return KakaoPoint(
        lat=lat, lng=lng,
        address_name=str(document.get("address_name") or ""),
        address_type=str(document.get("address_type") or ""),
    )
