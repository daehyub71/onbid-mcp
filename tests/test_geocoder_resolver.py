"""폴백 사다리 테스트 (F3.1·F3.6·F3.7).

주소 하나를 좌표로 바꾸기까지 여러 번 시도한다. **순서와 기록이 이 모듈의 전부다.**

- 캐시를 먼저 본다 (F3.2) — 적중하면 외부 호출이 **0회**여야 한다
- 어느 단계에서 맞았는지가 `level` 이고, 근사인지 정확인지가 `status` 다 (F3.6)
- 읍면동 조합은 **근사**(`approx`)다. 정확으로 기록하면 지도에서 엉뚱한 점을 정확한 척 보여준다
- 429 는 사다리를 타지 않고 즉시 위로 던진다 (F3.3) — 남의 프로젝트 쿼터까지 태우기 때문

**빈 후보로 호출하지 않는다.** 도로명주소는 물건목록에 없어 실측 결측률이 100% 다 —
매번 빈 질의를 보내면 그만큼 쿼터가 사라진다.
"""

from typing import Any

import pytest

from core.geocoder.kakao import KakaoPoint, KakaoQuotaExceededError
from core.geocoder.resolver import GeocodeTarget, resolve_one

SEOUL = "서울특별시 서대문구 창천동 72-22"


class FakeKakao:
    """주소 → 결과를 미리 정해 둔 대역. 호출 순서를 기록한다."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.queries: list[str] = []

    async def search(self, address: str) -> Any:
        self.queries.append(address)
        answer = self.answers.get(address)
        if isinstance(answer, Exception):
            raise answer
        return answer


def point(lat: float = 37.5, lng: float = 127.0, kind: str = "REGION_ADDR") -> KakaoPoint:
    return KakaoPoint(lat=lat, lng=lng, address_name="맞춘 주소", address_type=kind)


def target(**overrides: Any) -> GeocodeTarget:
    values: dict[str, Any] = {
        "key": ("T-1", "1"), "road_addr": None, "jibun_addr": SEOUL,
        "sd_nm": "서울특별시", "sgg_nm": "서대문구", "emd_nm": "창천동", **overrides,
    }
    return GeocodeTarget(**values)


# ── 캐시 (F3.2) ────────────────────────────────────────────────────────


async def test_cache_hit_skips_every_call() -> None:
    kakao = FakeKakao()
    cached = {SEOUL: (37.1, 127.1, "kakao", "jibun")}

    result = await resolve_one(target(), kakao=kakao, cached=cached)

    assert kakao.queries == []
    assert (result.lat, result.level, result.status) == (37.1, "jibun", "ok")


async def test_cached_failure_is_not_retried() -> None:
    """실패를 캐시해 두었으면 다시 묻지 않는다 — 쿼터가 실패 주소로 새는 것을 막는다."""
    kakao = FakeKakao({SEOUL: point()})
    cached = {SEOUL: (None, None, None, None)}

    result = await resolve_one(target(), kakao=kakao, cached=cached)

    assert kakao.queries == []
    assert result.status == "failed"


# ── 사다리 순서 (F3.1) ─────────────────────────────────────────────────


async def test_road_address_is_tried_first() -> None:
    road = "서울특별시 서대문구 신촌로 12"
    kakao = FakeKakao({road: point(kind="ROAD_ADDR")})

    result = await resolve_one(target(road_addr=road), kakao=kakao, cached={})

    assert kakao.queries == [road]
    assert (result.level, result.status) == ("road", "ok")


async def test_jibun_is_tried_when_road_is_absent() -> None:
    """도로명주소는 물건목록에 없다 — 실측 결측률 100%."""
    kakao = FakeKakao({SEOUL: point()})

    result = await resolve_one(target(), kakao=kakao, cached={})

    assert kakao.queries == [SEOUL]
    assert result.level == "jibun"


async def test_blank_candidates_are_not_queried() -> None:
    """빈 질의를 보내면 결과 없이 쿼터만 쓴다."""
    kakao = FakeKakao({SEOUL: point()})

    await resolve_one(target(road_addr="   "), kakao=kakao, cached={})

    assert kakao.queries == [SEOUL]


async def test_trailer_is_stripped_and_retried() -> None:
    """`외 N필지` 가 붙으면 카카오가 0건을 준다 (실측). 떼고 다시 묻는다."""
    with_trailer = f"{SEOUL} 외 2필지"
    kakao = FakeKakao({with_trailer: None, SEOUL: point()})

    result = await resolve_one(target(jibun_addr=with_trailer), kakao=kakao, cached={})

    assert kakao.queries == [with_trailer, SEOUL]
    assert result.level == "trimmed"


async def test_district_center_is_the_last_resort() -> None:
    """읍면동 결측률이 0% 라 이 단계는 항상 성립한다 (F3.7)."""
    kakao = FakeKakao({SEOUL: None, "서울특별시 서대문구 창천동": point()})

    result = await resolve_one(target(), kakao=kakao, cached={})

    assert kakao.queries[-1] == "서울특별시 서대문구 창천동"
    assert (result.level, result.status) == ("dong_center", "approx")


async def test_district_center_is_approximate_not_exact() -> None:
    """정확으로 기록하면 지도에서 엉뚱한 점을 정확한 척 보여준다."""
    kakao = FakeKakao({SEOUL: None, "서울특별시 서대문구 창천동": point()})

    assert (await resolve_one(target(), kakao=kakao, cached={})).status == "approx"


async def test_duplicate_candidates_are_queried_once() -> None:
    """지번주소와 꼬리표 제거 결과가 같으면 같은 질의를 두 번 보내게 된다."""
    kakao = FakeKakao({SEOUL: None, "서울특별시 서대문구 창천동": point()})

    await resolve_one(target(), kakao=kakao, cached={})

    assert kakao.queries.count(SEOUL) == 1


# ── 전부 실패 (F3.7) ───────────────────────────────────────────────────


async def test_all_steps_failing_yields_failed() -> None:
    """읍면동까지 실패하면 버그로 본다 — 그래도 배치를 멈추지는 않는다."""
    kakao = FakeKakao()

    result = await resolve_one(target(), kakao=kakao, cached={})

    assert result.status == "failed"
    assert result.lat is None and result.level is None


async def test_target_without_any_address_makes_no_call() -> None:
    kakao = FakeKakao()

    result = await resolve_one(
        target(jibun_addr=None, emd_nm=None, sgg_nm=None, sd_nm=None),
        kakao=kakao, cached={})

    assert kakao.queries == []
    assert result.status == "failed"


# ── 쿼터 (F3.3) ────────────────────────────────────────────────────────


async def test_quota_error_propagates_immediately() -> None:
    """사다리를 계속 타면 남의 프로젝트 쿼터까지 태운다."""
    kakao = FakeKakao({SEOUL: KakaoQuotaExceededError("쿼터 소진")})

    with pytest.raises(KakaoQuotaExceededError):
        await resolve_one(target(), kakao=kakao, cached={})

    assert kakao.queries == [SEOUL]


# ── 기록 (F3.6) ────────────────────────────────────────────────────────


async def test_result_records_the_source() -> None:
    kakao = FakeKakao({SEOUL: point()})

    result = await resolve_one(target(), kakao=kakao, cached={})

    assert result.src == "kakao"
    assert result.key == ("T-1", "1")


async def test_result_carries_the_query_that_worked() -> None:
    """어떤 질의로 맞췄는지 남겨야 캐시에 넣고 나중에 검수할 수 있다."""
    kakao = FakeKakao({SEOUL: point()})

    result = await resolve_one(target(), kakao=kakao, cached={})

    assert result.addr == SEOUL


async def test_failed_result_carries_the_last_query() -> None:
    """실패도 캐시하려면 어떤 주소가 실패했는지 알아야 한다."""
    kakao = FakeKakao()

    result = await resolve_one(target(), kakao=kakao, cached={})

    assert result.addr == SEOUL


async def test_failure_does_not_poison_the_district_key() -> None:
    """실패를 읍면동 키로 캐시하면 **같은 동의 모든 물건**이 최후 폴백을 건너뛴다."""
    kakao = FakeKakao()

    result = await resolve_one(target(), kakao=kakao, cached={})

    assert result.addr != "서울특별시 서대문구 창천동"
