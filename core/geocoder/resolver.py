"""폴백 사다리 — 주소 하나를 좌표로 (F3.1·F3.6·F3.7).

한 번에 맞지 않는 주소가 많아 여러 단계를 순서대로 시도한다. **순서와 기록이 이 모듈의 전부다.**

| 단계 | 시도 | status | level |
|---|---|---|---|
| 0 | 캐시 | (캐시값) | (캐시값) |
| 1 | 도로명주소 | ok | road |
| 2 | 지번주소 | ok | jibun |
| 3 | 꼬리표 제거 후 재시도 | ok | trimmed |
| 5 | 시도+시군구+읍면동 | **approx** | dong_center |
| 6 | 전부 실패 | failed | — |

4단계(VWorld)는 카카오만으로 90% 에 못 미칠 때 착수한다 — 아직 없다.

**읍면동 조합은 근사다.** `ok` 로 기록하면 지도에서 동 중심점을 정확한 위치인 척 보여준다.
실측상 읍면동 결측률이 0% 라 이 단계는 항상 성립하며, 따라서 `failed` 는 원리적으로 나오지
않아야 한다 (F3.7). 나오면 버그로 보고 원인을 찾되, 배치를 멈추지는 않는다.

**쿼터 소진은 사다리를 타지 않고 즉시 위로 던진다** (F3.3). 다음 단계를 시도하는 것은 공유
중인 카카오 앱을 더 세게 두드리는 일이다.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from core.geocoder.kakao import KakaoPoint
from core.normalizer.trailers import strip_address_trailers

logger = logging.getLogger(__name__)

SRC_KAKAO: Final = "kakao"

STATUS_OK: Final = "ok"
STATUS_APPROX: Final = "approx"
STATUS_FAILED: Final = "failed"

LEVEL_ROAD: Final = "road"
LEVEL_JIBUN: Final = "jibun"
LEVEL_TRIMMED: Final = "trimmed"
LEVEL_DONG: Final = "dong_center"

#: 캐시 항목 형식 — ``(lat, lng, src, level)``.
CachedTuple = tuple[float | None, float | None, str | None, str | None]


class AddressSearcher(Protocol):
    """주소 검색기. `KakaoClient` 가 이 모양이다."""

    async def search(self, address: str) -> KakaoPoint | None: ...


@dataclass(frozen=True, slots=True)
class GeocodeTarget:
    """좌표를 붙일 대상.

    Attributes:
        key: 물건 복합키.
        road_addr: 도로명주소. **물건목록에는 없다** — 실측 결측률 100%.
        jibun_addr: 지번주소. PNU 또는 물건명에서 조립된다.
        sd_nm: 시도명.
        sgg_nm: 시군구명.
        emd_nm: 읍면동명. 실측 결측률 0%.
    """

    key: tuple[str, str]
    road_addr: str | None
    jibun_addr: str | None
    sd_nm: str | None
    sgg_nm: str | None
    emd_nm: str | None

    def district_query(self) -> str | None:
        """시도+시군구+읍면동 조합. 읍면동이 없으면 None."""
        if not self.emd_nm:
            return None
        parts = [p for p in (self.sd_nm, self.sgg_nm, self.emd_nm) if p]
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    """한 대상의 지오코딩 결과.

    Attributes:
        key: 물건 복합키.
        addr: 좌표를 얻은 질의. 실패했다면 마지막으로 시도한 질의 — **실패도 캐시하려면
            어떤 주소가 실패했는지 알아야 한다.**
        lat: 위도. 실패면 None.
        lng: 경도. 실패면 None.
        status: ``ok`` | ``approx`` | ``failed``.
        level: ``road`` | ``jibun`` | ``trimmed`` | ``dong_center``. 실패면 None.
        src: ``kakao`` | ``vworld``. 실패면 None.
        from_cache: 외부 호출 없이 캐시로 해결했는지 여부.
    """

    key: tuple[str, str]
    addr: str | None
    lat: float | None
    lng: float | None
    status: str
    level: str | None
    src: str | None
    from_cache: bool = False

    @property
    def is_located(self) -> bool:
        """좌표를 얻었는지 여부 (근사 포함)."""
        return self.lat is not None and self.lng is not None


def _candidates(target: GeocodeTarget) -> list[tuple[str, str, str]]:
    """시도 순서대로 ``(질의, status, level)`` 을 만든다.

    빈 질의와 **중복 질의는 빼놓는다** — 지번주소와 꼬리표 제거 결과가 같은 경우가 흔한데,
    그대로 두면 같은 주소를 두 번 묻는다.
    """
    steps: list[tuple[str | None, str, str]] = [
        (target.road_addr, STATUS_OK, LEVEL_ROAD),
        (target.jibun_addr, STATUS_OK, LEVEL_JIBUN),
        (strip_address_trailers(target.jibun_addr), STATUS_OK, LEVEL_TRIMMED),
        (target.district_query(), STATUS_APPROX, LEVEL_DONG),
    ]

    seen: set[str] = set()
    ordered: list[tuple[str, str, str]] = []
    for query, status, level in steps:
        text = (query or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append((text, status, level))
    return ordered


def _from_cache(key: tuple[str, str], addr: str, entry: CachedTuple) -> GeocodeResult:
    """캐시값을 결과로 바꾼다. 캐시된 실패는 실패 그대로 돌려준다."""
    lat, lng, src, level = entry
    if lat is None or lng is None:
        return GeocodeResult(key=key, addr=addr, lat=None, lng=None,
                             status=STATUS_FAILED, level=None, src=None, from_cache=True)
    status = STATUS_APPROX if level == LEVEL_DONG else STATUS_OK
    return GeocodeResult(key=key, addr=addr, lat=lat, lng=lng, status=status,
                         level=level, src=src, from_cache=True)


async def resolve_one(
    target: GeocodeTarget,
    *,
    kakao: AddressSearcher,
    cached: Mapping[str, CachedTuple],
) -> GeocodeResult:
    """한 대상의 좌표를 사다리를 따라 찾는다.

    Args:
        target: 좌표를 붙일 대상.
        kakao: 주소 검색기.
        cached: 이미 아는 주소 → ``(lat, lng, src, level)``. 정규화된 키여야 한다.

    Returns:
        결과. **실패해도 예외를 던지지 않는다** — 한 건 때문에 배치를 멈추지 않는다.

    Raises:
        KakaoQuotaExceededError: 쿼터 소진. 사다리를 타지 않고 즉시 올린다 (F3.3).
        KakaoAuthError: 키 문제.
    """
    candidates = _candidates(target)
    if not candidates:
        logger.warning("주소가 없어 지오코딩할 수 없다: %s", target.key)
        return GeocodeResult(key=target.key, addr=None, lat=None, lng=None,
                             status=STATUS_FAILED, level=None, src=None)

    # 캐시를 먼저 본다 — 어느 단계의 질의든 이미 알고 있으면 호출하지 않는다 (F3.2).
    for query, _status, _level in candidates:
        entry = cached.get(query)
        if entry is not None:
            return _from_cache(target.key, query, entry)

    for query, status, level in candidates:
        # 쿼터·키 오류는 여기서 잡지 않는다 — 위로 올려 배치를 중단시킨다.
        point = await kakao.search(query)
        if point is None:
            continue
        return GeocodeResult(key=target.key, addr=query, lat=point.lat, lng=point.lng,
                             status=status, level=level, src=SRC_KAKAO)

    # 읍면동 결측률이 0% 라 여기까지 오면 이상하다 (F3.7).
    #
    # 실패로 기록하는 주소는 **가장 구체적인 첫 후보**다. 마지막 후보(읍면동 조합)를 실패로
    # 캐시하면 **같은 동의 모든 물건이 최후 폴백을 건너뛰게 되어** 동 하나가 통째로 오염된다.
    primary = candidates[0][0]
    logger.warning("모든 폴백 단계 실패: %s (%s)", target.key, primary)
    return GeocodeResult(key=target.key, addr=primary, lat=None, lng=None,
                         status=STATUS_FAILED, level=None, src=None)


async def resolve_many(
    targets: Sequence[GeocodeTarget],
    *,
    kakao: AddressSearcher,
    cached: Mapping[str, CachedTuple],
) -> list[GeocodeResult]:
    """여러 대상을 순서대로 처리한다.

    이번 실행에서 얻은 좌표도 **바로 캐시에 얹어** 같은 주소를 두 번 묻지 않는다 —
    실측상 6,902건의 고유 주소가 801개뿐이라 이 절약이 크다.

    Args:
        targets: 대상들.
        kakao: 주소 검색기.
        cached: 시작 시점의 캐시.

    Returns:
        입력 순서대로의 결과.

    Raises:
        KakaoQuotaExceededError: 쿼터 소진. 호출자가 재개 지점을 기록한다 (F3.3).
    """
    running: dict[str, CachedTuple] = dict(cached)
    results: list[GeocodeResult] = []

    for target in targets:
        result = await resolve_one(target, kakao=kakao, cached=running)
        results.append(result)
        if result.addr and not result.from_cache:
            running[result.addr] = (result.lat, result.lng, result.src, result.level)

    return results
