"""`get_address_geocode` — 주소 → 좌표 (SPEC §8.4·F6.10).

**카카오 쿼터를 LLM 호출이 소진한다.** 배치는 예산을 지키지만 대화는 그렇지 않다 — 상한이
없으면 몇 번의 질문으로 배치가 쓸 쿼터가 사라지고, 그 앱은 다른 프로젝트와 공유된다.
그래서 **서버 측 일일 상한**을 둔다 (F6.10).

**캐시 적중은 상한을 깎지 않는다.** 외부 호출이 아니기 때문이다 — 깎으면 쓸 수 있는 조회가
이유 없이 줄어든다.

찾지 못한 주소는 빈 값이 아니라 `no_result` 다 (§8.7).
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import psycopg

from core.geocoder.cache import CachedPoint, lookup_cache, normalize_addr, store_cache
from core.geocoder.kakao import KakaoPoint
from core.normalizer.pnu import parse_pnu
from onbid_mcp.common import ToolError, ok_response
from onbid_mcp.errors import to_tool_error

logger = logging.getLogger(__name__)

DEFAULT_DAILY_LIMIT: Final = 50
"""대화용 상한. 배치 예산(수백~천)과 별개로 작게 잡는다 — 검수 편의가 목적이다 (§8.4)."""

SRC_KAKAO: Final = "kakao"


class AddressSearcher(Protocol):
    """주소 검색기. `KakaoClient` 가 이 모양이다."""

    async def search(self, address: str) -> KakaoPoint | None: ...


@dataclass(slots=True)
class DailyBudget:
    """일일 호출 상한 (F6.10).

    **날짜가 바뀌면 초기화**한다 — '일일' 상한이 영구 상한이 되면 하루 뒤에도 못 쓴다.

    Attributes:
        limit: 하루 상한.
        used: 오늘 쓴 횟수.
        day: 기준 날짜 (KST 기준 문자열).
    """

    limit: int = DEFAULT_DAILY_LIMIT
    used: int = 0
    day: str = field(default="")

    @property
    def remaining(self) -> int:
        """남은 횟수."""
        return max(0, self.limit - self.used)

    def consume(self, *, on: str | None = None) -> None:
        """한 번 썼다고 기록한다.

        Args:
            on: 날짜 문자열. 생략하면 오늘.

        Raises:
            ToolError: 상한을 넘겼을 때 `quota_exceeded`.
        """
        today = on or datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.used = today, 0
        if self.used >= self.limit:
            raise ToolError(
                "quota_exceeded",
                f"주소 변환 일일 한도({self.limit}회)를 소진했습니다. 내일 다시 시도하세요.")
        self.used += 1


#: 서버 전역 상한. 툴 호출마다 새로 만들면 상한이 무의미해진다.
_BUDGET = DailyBudget()


def _bcode(point: KakaoPoint | None, cached: CachedPoint | None) -> str | None:
    """법정동코드. 다른 공적 데이터와 이어붙이는 열쇠다 (§8.4).

    카카오 응답에는 없으므로 주소에서 PNU 를 만들 수 없는 경우 None 이다.
    """
    address = point.address_name if point else (cached.addr if cached else None)
    parsed = parse_pnu(address) if address else None
    return parsed.legal_dong_code if parsed else None


async def get_address_geocode(
    conn: psycopg.AsyncConnection[Any],
    *,
    address: str,
    kakao: AddressSearcher,
    budget: DailyBudget | None = None,
) -> dict[str, Any]:
    """주소를 좌표로 바꾼다.

    Args:
        conn: 열린 연결 (캐시 조회·저장).
        address: 변환할 주소.
        kakao: 주소 검색기.
        budget: 일일 상한. 생략하면 서버 전역 상한.

    Returns:
        `lat` · `lng` · `bcode` · `level` · `src` · `matched_addr` + `meta`.

    Raises:
        ToolError: 빈 주소는 `invalid_param`, 못 찾으면 `no_result`,
            상한·외부 쿼터 소진은 `quota_exceeded`.
    """
    quota = budget if budget is not None else _BUDGET
    key = normalize_addr(address)
    if not key:
        raise ToolError("invalid_param", "address 는 비어 있을 수 없습니다.")

    echo = {"address": key}

    # 캐시 적중은 외부 호출이 아니므로 상한을 깎지 않는다 (F3.2).
    cached = (await lookup_cache(conn, [key])).get(key)
    if cached is not None and not cached.is_failure:
        return ok_response(
            {"lat": cached.lat, "lng": cached.lng, "bcode": _bcode(None, cached),
             "level": cached.level, "src": cached.src, "matched_addr": cached.addr,
             "from_cache": True},
            query_echo=echo, count=1,
            meta_extra={"daily_remaining": quota.remaining})

    quota.consume()
    try:
        point = await kakao.search(key)
    except Exception as exc:
        raise to_tool_error(exc) from exc

    if point is None:
        # 실패도 캐시한다 — 같은 주소를 매번 다시 묻지 않기 위해서다 (F3.2).
        await store_cache(conn, [CachedPoint(addr=key, lat=None, lng=None,
                                             src=None, level=None)])
        raise ToolError("no_result", f"주소를 찾을 수 없습니다: {key}")

    level = "road" if point.address_type == "ROAD_ADDR" else "jibun"
    await store_cache(conn, [CachedPoint(addr=key, lat=point.lat, lng=point.lng,
                                         src=SRC_KAKAO, level=level)])

    return ok_response(
        {"lat": point.lat, "lng": point.lng, "bcode": _bcode(point, None),
         "level": level, "src": SRC_KAKAO, "matched_addr": point.address_name,
         "from_cache": False},
        query_echo=echo, count=1,
        meta_extra={"daily_remaining": quota.remaining})
