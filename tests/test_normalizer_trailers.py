"""주소 꼬리표 제거 테스트 (F2.2).

**어떤 꼬리표가 실제로 지오코딩을 깨뜨리는지 측정한 뒤 설계했다.**
카카오 실측(기준 주소 `서울특별시 강남구 도곡동 467-6`)에서 실패한 것은
``외 N필지`` / ``외 N필`` 뿐이며, 건물명·층·호·괄호 부기·쉼표 상세는 모두 흡수된다.

그래서 제거는 **보수적**이다 — 깨뜨리는 것만 걷어내고 나머지는 남긴다.
과하게 자르면 동명이지번을 만들어 엉뚱한 좌표로 이어진다.
"""

import pytest

from core.normalizer.trailers import (
    BREAKING_TRAILERS,
    strip_address_trailers,
    strip_detail_suffix,
)

# ── 지오코딩을 깨뜨리는 꼬리표 (실측) ────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [
    ("서울특별시 강남구 도곡동 467-6 외 2필지", "서울특별시 강남구 도곡동 467-6"),
    ("서울특별시 강남구 도곡동 467-6 외 2필", "서울특별시 강남구 도곡동 467-6"),
    ("서울특별시 강남구 도곡동 467-6 외2필지", "서울특별시 강남구 도곡동 467-6"),
    ("서울특별시 강남구 도곡동 467-6 외 12 필지", "서울특별시 강남구 도곡동 467-6"),
])
def test_trailers_removes_multi_lot_marker(raw: str, expected: str) -> None:
    """실측: `외 N필지` 가 붙으면 카카오가 0건을 반환한다."""
    assert strip_address_trailers(raw) == expected


def test_trailers_removes_everything_after_the_marker() -> None:
    """마커 뒤 건물명·층·호까지 함께 사라진다 — 마커가 남으면 여전히 실패한다."""
    raw = "서울특별시 강남구 도곡동 467-6 외 2필지 대림아크로빌 제17층 제1714호"
    assert strip_address_trailers(raw) == "서울특별시 강남구 도곡동 467-6"


# ── 카카오가 흡수하는 것은 건드리지 않는다 ──────────────────────────────


@pytest.mark.parametrize("raw", [
    "서울특별시 강남구 도곡동 467-6 대림아크로빌 제17층 제1714호",
    "서울특별시 강남구 수서동 724 로즈데일오피스텔 제지하2층 제101호",
    "서울특별시 강남구 대치동 897-15 (건물 및 토지)",
    "서울특별시 강남구 언주로30길 13, 제17층 제1714호 (도곡동, 대림아크로빌)",
    "서울특별시 강남구 도곡동 467-6 101동 1503호",
    "서울특별시 강남구 도곡동 467-6 외 3개호",
])
def test_trailers_keeps_what_kakao_can_handle(raw: str) -> None:
    """실측에서 성공한 형태는 손대지 않는다. 과한 제거가 더 위험하다."""
    assert strip_address_trailers(raw) == raw


def test_trailers_leaves_plain_address_alone() -> None:
    plain = "서울특별시 강남구 도곡동 467-6"
    assert strip_address_trailers(plain) == plain


@pytest.mark.parametrize("raw", [None, "", "   ", "-"])
def test_trailers_returns_none_for_blank(raw: object) -> None:
    assert strip_address_trailers(raw) is None


def test_trailers_never_empties_the_address() -> None:
    """제거 결과가 비면 원본을 돌려준다 — 빈 문자열로 지오코딩할 수는 없다."""
    assert strip_address_trailers("외 2필지") == "외 2필지"


def test_breaking_trailers_is_documented() -> None:
    """무엇을 왜 지우는지 코드에서 확인할 수 있어야 한다."""
    assert BREAKING_TRAILERS
    assert all(reason for _, reason in BREAKING_TRAILERS)


# ── 상세주소 절단 (폴백용) ──────────────────────────────────────────────


def test_detail_suffix_cuts_at_comma() -> None:
    """도로명주소는 `도로명 건물번호, 상세 (법정동, 건물명)` 형식이다."""
    raw = "서울특별시 강남구 언주로30길 13, 제17층 제1714호 (도곡동, 대림아크로빌)"
    assert strip_detail_suffix(raw) == "서울특별시 강남구 언주로30길 13"


def test_detail_suffix_cuts_parenthesis_without_comma() -> None:
    assert strip_detail_suffix("서울특별시 강남구 대치동 897-15 (건물 및 토지)") == \
        "서울특별시 강남구 대치동 897-15"


def test_detail_suffix_cuts_floor_and_unit() -> None:
    raw = "서울특별시 강남구 도곡동 467-6 대림아크로빌 제17층 제1714호"
    assert strip_detail_suffix(raw) == "서울특별시 강남구 도곡동 467-6 대림아크로빌"


def test_detail_suffix_cuts_basement_floor() -> None:
    """실측: 지하층은 `제지하2층` 처럼 붙여 쓴다."""
    raw = "서울특별시 강남구 수서동 724 로즈데일오피스텔 제지하2층 제101호"
    assert strip_detail_suffix(raw) == "서울특별시 강남구 수서동 724 로즈데일오피스텔"


def test_detail_suffix_is_idempotent() -> None:
    once = strip_detail_suffix("서울특별시 강남구 언주로30길 13, 제17층 (도곡동)")
    assert strip_detail_suffix(once) == once


def test_detail_suffix_leaves_clean_address_alone() -> None:
    plain = "서울특별시 강남구 도곡동 467-6"
    assert strip_detail_suffix(plain) == plain


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_detail_suffix_returns_none_for_blank(raw: object) -> None:
    assert strip_detail_suffix(raw) is None


def test_detail_suffix_never_empties_the_address() -> None:
    assert strip_detail_suffix(", 제3층") == ", 제3층"


# ── 실데이터 ────────────────────────────────────────────────────────────


def test_trailers_on_real_item_names() -> None:
    """실응답 물건명에 적용해도 주소 앞부분이 살아 있어야 한다."""
    from tests.conftest import load_fixture
    rows = load_fixture("list_many")["body"]["items"]["item"]

    for row in rows:
        name = row["onbidCltrNm"]
        stripped = strip_address_trailers(name)
        assert stripped is not None
        assert stripped.startswith(row["lctnSdnm"])
        assert row["lctnEmdNm"] in stripped
