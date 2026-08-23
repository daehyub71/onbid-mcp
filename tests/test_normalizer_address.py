"""지오코딩에 넣을 주소 선택 테스트 (F2.1·F2.6).

온비드는 좌표를 주지 않으므로 주소를 골라 지오코딩해야 한다. 목록 응답에서 쓸 수 있는
소스는 셋뿐이며 — PNU · 물건명 · 읍면동 조합 — 정확도 순으로 시도한다.

`cltrRadr`·`zadrNm` 은 **물건상세 전용**이라 여기서 쓸 수 없다 (SPEC §4 F2.1 정정).
"""

import pytest

from core.normalizer.address import (
    AddressSource,
    SelectedAddress,
    jibun_from_pnu,
    select_address,
)


def picked(row: object) -> SelectedAddress:
    """주소가 선택됐음을 단언하고 돌려준다 — 테스트 본문에서 None 검사를 반복하지 않는다."""
    result = select_address(row)
    assert result is not None, "주소를 만들지 못했다"
    return result


ROW = {
    "ltnoPnu": "1144012400002450042",
    "lctnSdnm": "서울특별시",
    "lctnSggnm": "마포구",
    "lctnEmdNm": "연남동",
    "onbidCltrNm": "서울특별시 마포구 연남동 245-42 삼정도나빌 제2층 제204호",
}


# ── PNU → 지번 ──────────────────────────────────────────────────────────


def test_jibun_from_pnu_builds_bon_and_bu() -> None:
    """PNU 19자리 = 법정동코드(10) + 산여부(1) + 본번(4) + 부번(4)."""
    assert jibun_from_pnu("1144012400002450042", "연남동") == "연남동 245-42"


def test_jibun_from_pnu_omits_zero_bu() -> None:
    """부번이 0이면 `-0` 을 붙이지 않는다."""
    assert jibun_from_pnu("1144012400002450000", "연남동") == "연남동 245"


def test_jibun_from_pnu_marks_mountain_lots() -> None:
    """산여부 자리가 1이면 산번지다. 온비드 물건명은 공백 없이 `산181-11` 로 쓴다."""
    assert jibun_from_pnu("1162010200101810011", "신림동") == "신림동 산181-11"


def test_jibun_from_pnu_strips_leading_zeros() -> None:
    assert jibun_from_pnu("1144012400000010002", "연남동") == "연남동 1-2"


@pytest.mark.parametrize("pnu", [None, "", "114401240000245004", "1144012400002450042X", 123])
def test_jibun_from_pnu_rejects_malformed(pnu: object) -> None:
    """19자리 숫자가 아니면 만들지 않는다 — 틀린 주소로 지오코딩하면 엉뚱한 좌표가 나온다."""
    assert jibun_from_pnu(pnu, "연남동") is None


def test_jibun_from_pnu_requires_district_name() -> None:
    """PNU 는 법정동 '코드'만 담는다. 이름은 응답의 읍면동 필드에서 와야 한다."""
    assert jibun_from_pnu("1144012400002450042", None) is None


def test_jibun_from_pnu_rejects_all_zero_lot() -> None:
    """본번이 0이면 지번이 성립하지 않는다."""
    assert jibun_from_pnu("1144012400000000000", "연남동") is None


# ── 주소 선택 ───────────────────────────────────────────────────────────


def test_select_address_prefers_pnu() -> None:
    picked_ = picked(ROW)
    assert picked_.source is AddressSource.PNU
    assert picked_.query == "서울특별시 마포구 연남동 245-42"
    assert picked_.is_exact


def test_select_address_includes_sido_and_sigungu() -> None:
    """지오코딩에는 상위 행정구역이 있어야 동명 중복(서울 중구/부산 중구)을 가른다."""
    picked_ = picked(ROW)
    assert picked_.query.startswith("서울특별시 마포구")


def test_select_address_falls_back_to_item_name() -> None:
    """PNU 결측 23%가 여기로 온다."""
    picked_ = picked({k: v for k, v in ROW.items() if k != "ltnoPnu"})
    assert picked_.source is AddressSource.ITEM_NAME
    assert picked_.query == "서울특별시 마포구 연남동 245-42"


def test_select_address_item_name_drops_building_and_floor() -> None:
    """건물명·층·호가 붙으면 카카오가 0건을 반환한다 (실측)."""
    row = {"lctnSdnm": "서울특별시", "lctnSggnm": "강남구", "lctnEmdNm": "역삼동",
           "onbidCltrNm": "서울특별시 강남구 역삼동 123-4 외 2필지 래미안 제3층 제301호"}
    assert picked(row).query == "서울특별시 강남구 역삼동 123-4"


def test_select_address_falls_back_to_district() -> None:
    """마지막 수단. 동 중심 좌표라 근사값이다."""
    row = {"lctnSdnm": "서울특별시", "lctnSggnm": "마포구", "lctnEmdNm": "연남동"}
    picked_ = picked(row)
    assert picked_.source is AddressSource.DISTRICT
    assert picked_.query == "서울특별시 마포구 연남동"
    assert not picked_.is_exact


def test_select_address_returns_none_without_district() -> None:
    """읍면동조차 없으면 만들 수 있는 주소가 없다 (실측 결측률 0%라 드물다)."""
    assert select_address({"onbidCltrNm": "이름만 있음"}) is None


def test_select_address_ignores_item_name_without_a_lot_number() -> None:
    """지번이 없는 물건명은 주소가 아니다 — 그대로 넣으면 오탐 좌표가 나온다."""
    row = {"lctnSdnm": "서울특별시", "lctnSggnm": "마포구", "lctnEmdNm": "연남동",
           "onbidCltrNm": "서울특별시 마포구 연남동 상가 일괄"}
    assert picked(row).source is AddressSource.DISTRICT


def test_select_address_keeps_the_raw_row_untouched() -> None:
    row = dict(ROW)
    select_address(row)
    assert row == ROW


def test_select_address_result_is_immutable() -> None:
    picked_ = picked(ROW)
    with pytest.raises((AttributeError, TypeError)):
        picked_.query = "바꿈"  # type: ignore[misc]


@pytest.mark.parametrize("blank", [None, "", "  ", "-"])
def test_select_address_treats_blank_pnu_as_missing(blank: object) -> None:
    picked_ = picked({**ROW, "ltnoPnu": blank})
    assert picked_.source is AddressSource.ITEM_NAME


# ── 실데이터 ────────────────────────────────────────────────────────────


def test_select_address_on_real_rows() -> None:
    from tests.conftest import load_fixture
    rows = load_fixture("list_many")["body"]["items"]["item"]

    for row in rows:
        picked_ = picked(row)
        assert picked_.query.startswith("서울특별시")
        # PNU 로 만든 지번은 물건명 안에 그대로 들어 있어야 한다 (F2.6)
        if picked_.source is AddressSource.PNU:
            lot = picked_.query.split(f"{row['lctnEmdNm']} ", 1)[1]
            assert lot.replace("산 ", "산") in row["onbidCltrNm"].replace("산 ", "산")
