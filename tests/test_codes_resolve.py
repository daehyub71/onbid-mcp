"""툴 파라미터 해석 테스트 (F6.6·F6.7).

MCP 툴은 `region`·`usage`·`prpt_div` 를 **코드값과 한글 명칭 양쪽으로** 받는다.
단 `region` 은 온비드에 코드가 없어 **명칭 전용**이다.

매칭이 하나로 좁혀지지 않으면 `invalid_param` 오류에 **후보**를 실어 LLM 이 재시도하게 한다.
실측상 서울 199개 읍면동 중 `신사동` 이 강남구·은평구 양쪽에 있어 실제로 모호해진다.
"""

import pytest

from core.codes.address import AddressEntry
from core.codes.index import UsageIndex
from core.codes.resolve import (
    PropertyType,
    RegionIndex,
    RegionMatch,
    resolve_property_type,
    resolve_usage,
)
from core.codes.usage import UsageCode

ENTRIES = [
    AddressEntry("서울특별시", "강남구", "개포동"),
    AddressEntry("서울특별시", "강남구", "논현동"),
    AddressEntry("서울특별시", "강남구", "신사동"),
    AddressEntry("서울특별시", "은평구", "신사동"),
    AddressEntry("서울특별시", "은평구", "응암동"),
]

USAGE_NODES = [
    UsageCode("10000", "부동산", None, None, 1),
    UsageCode("10200", "주거용건물", "10000", "부동산", 2),
    UsageCode("10201", "아파트", "10200", "주거용건물", 3),
]


@pytest.fixture
def regions() -> RegionIndex:
    return RegionIndex(ENTRIES)


# ── 지역 (명칭 전용) ────────────────────────────────────────────────────


def test_region_resolves_a_district(regions: RegionIndex) -> None:
    result = regions.resolve("강남구")
    assert result.is_resolved
    assert result.matched == (RegionMatch("강남구", None),)


def test_region_resolves_a_neighbourhood(regions: RegionIndex) -> None:
    """읍면동을 주면 자치구까지 함께 특정된다."""
    result = regions.resolve("개포동")
    assert result.is_resolved
    assert result.matched == (RegionMatch("강남구", "개포동"),)


def test_region_resolves_a_full_phrase(regions: RegionIndex) -> None:
    result = regions.resolve("강남구 신사동")
    assert result.is_resolved
    assert result.matched == (RegionMatch("강남구", "신사동"),)


def test_region_reports_ambiguity(regions: RegionIndex) -> None:
    """실측: 서울에 `신사동` 이 강남구·은평구 두 곳에 있다."""
    result = regions.resolve("신사동")
    assert not result.is_resolved
    assert result.is_ambiguous
    assert set(result.matched) == {
        RegionMatch("강남구", "신사동"),
        RegionMatch("은평구", "신사동"),
    }


def test_region_unknown_returns_candidates(regions: RegionIndex) -> None:
    """매칭 실패 시 LLM 이 재시도할 수 있게 후보를 준다 (F6.7)."""
    result = regions.resolve("강남")
    assert result.is_unknown
    assert RegionMatch("강남구", None) in result.candidates


def test_region_candidates_are_empty_for_nonsense(regions: RegionIndex) -> None:
    assert regions.resolve("없는동네").candidates == ()


def test_region_ignores_spacing(regions: RegionIndex) -> None:
    assert regions.resolve(" 강남구  개포동 ").is_resolved


def test_region_rejects_blank(regions: RegionIndex) -> None:
    result = regions.resolve(None)
    assert result.is_unknown
    assert result.matched == ()


def test_region_lists_districts(regions: RegionIndex) -> None:
    assert regions.districts == ("강남구", "은평구")


def test_region_match_is_immutable() -> None:
    with pytest.raises((AttributeError, TypeError)):
        RegionMatch("강남구", None).sgg_nm = "서초구"  # type: ignore[misc]


# ── 재산유형 (코드·명칭 양쪽) ───────────────────────────────────────────


def test_property_type_resolves_a_name() -> None:
    result = resolve_property_type("압류재산")
    assert result.is_resolved
    assert result.matched == (PropertyType("0007", "압류재산"),)


def test_property_type_resolves_a_code() -> None:
    assert resolve_property_type("0007").matched == (PropertyType("0007", "압류재산"),)


def test_property_type_accepts_unpadded_code() -> None:
    assert resolve_property_type("7").matched == (PropertyType("0007", "압류재산"),)


def test_property_type_resolves_a_comma_list() -> None:
    """온비드가 쉼표 복수 지정을 받으므로 툴도 그대로 받는다."""
    result = resolve_property_type("압류재산,국유재산")
    assert {p.code for p in result.matched} == {"0007", "0010"}


def test_property_type_partial_list_is_unknown() -> None:
    """일부만 맞으면 해석하지 않는다 — 조용히 일부를 버리면 결과가 틀린다."""
    result = resolve_property_type("압류재산,없는유형")
    assert result.is_unknown


def test_property_type_unknown_returns_candidates() -> None:
    result = resolve_property_type("압류")
    assert result.is_unknown
    assert PropertyType("0007", "압류재산") in result.candidates


def test_property_type_rejects_blank() -> None:
    assert resolve_property_type("").is_unknown


def test_property_type_codes_match_spec() -> None:
    """SPEC §6.5 재산유형 10종."""
    assert len(resolve_property_type(None).candidates) == 10


# ── 용도 (인덱스 위임) ──────────────────────────────────────────────────


def test_usage_resolves_a_name() -> None:
    index = UsageIndex(USAGE_NODES)
    result = resolve_usage("아파트", index)
    assert result.is_resolved
    assert result.matched[0].ctgr_id == "10201"


def test_usage_expands_mid_category() -> None:
    """중분류를 주면 하위 소분류까지 — 확장하지 않으면 조회가 0건이 된다 (F6.12)."""
    index = UsageIndex(USAGE_NODES)
    result = resolve_usage("주거용건물", index, expand=True)
    assert {n.ctgr_id for n in result.matched} == {"10200", "10201"}


def test_usage_unknown_returns_candidates() -> None:
    index = UsageIndex(USAGE_NODES)
    result = resolve_usage("주거", index)
    assert result.is_unknown
    assert "주거용건물" in [n.ctgr_nm for n in result.candidates]


# ── 오류 메시지용 요약 ──────────────────────────────────────────────────


def test_resolution_describes_candidates(regions: RegionIndex) -> None:
    """`invalid_param` 응답에 그대로 실을 수 있어야 한다."""
    result = regions.resolve("강남")
    assert "강남구" in result.describe_candidates()


def test_resolution_describe_is_empty_without_candidates(regions: RegionIndex) -> None:
    assert regions.resolve("없는동네").describe_candidates() == ""


def test_resolution_ambiguous_describes_matches(regions: RegionIndex) -> None:
    """모호할 때는 매칭된 것들을 보여줘야 사용자가 고를 수 있다."""
    described = regions.resolve("신사동").describe_candidates()
    assert "강남구" in described
    assert "은평구" in described
