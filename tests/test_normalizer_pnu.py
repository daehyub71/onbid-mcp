"""PNU 처리 테스트 (SPEC §7.1, F2.1).

PNU 19자리 = 법정동코드(10) + 산여부(1) + 본번(4) + 부번(4).

**문자열로 다뤄야 한다.** 숫자로 저장하면 선행 0이 사라져 법정동코드가 깨진다
(예: 서울 종로구 청운동 `1111010100` → `1111010100` 은 살아남지만,
0으로 시작하는 코드는 9자리가 된다).
"""

import pytest

from core.normalizer.pnu import LEGAL_DONG_CODE_LENGTH, PNU_LENGTH, parse_pnu

SAMPLE = "1141011600000720022"  # 서울 서대문구 창천동 72-22


def test_pnu_splits_all_four_parts() -> None:
    parsed = parse_pnu(SAMPLE)
    assert parsed is not None
    assert parsed.legal_dong_code == "1141011600"
    assert parsed.is_mountain is False
    assert parsed.main_no == 72
    assert parsed.sub_no == 22


def test_pnu_keeps_raw_string() -> None:
    """원본을 그대로 보존한다 — 조립 규칙이 바뀌어도 재계산할 수 있다."""
    assert parse_pnu(SAMPLE).raw == SAMPLE  # type: ignore[union-attr]


def test_pnu_legal_dong_code_is_the_first_ten_digits() -> None:
    parsed = parse_pnu(SAMPLE)
    assert parsed is not None
    assert len(parsed.legal_dong_code) == LEGAL_DONG_CODE_LENGTH
    assert SAMPLE.startswith(parsed.legal_dong_code)


def test_pnu_preserves_leading_zeros_in_code() -> None:
    """법정동코드는 문자열이어야 한다. 정수로 만들면 선행 0이 사라진다."""
    parsed = parse_pnu("0141011600000720022")
    assert parsed is not None
    assert parsed.legal_dong_code == "0141011600"


def test_pnu_detects_mountain_lot() -> None:
    parsed = parse_pnu("1162010200101810011")
    assert parsed is not None
    assert parsed.is_mountain is True
    assert parsed.main_no == 181
    assert parsed.sub_no == 11


def test_pnu_jibun_formats_with_district() -> None:
    assert parse_pnu(SAMPLE).jibun("창천동") == "창천동 72-22"  # type: ignore[union-attr]


def test_pnu_jibun_omits_zero_sub() -> None:
    parsed = parse_pnu("1141011600000720000")
    assert parsed is not None
    assert parsed.jibun("창천동") == "창천동 72"


def test_pnu_jibun_marks_mountain_without_space() -> None:
    """온비드 물건명 표기와 맞춘다 — `산181-11`."""
    parsed = parse_pnu("1162010200101810011")
    assert parsed is not None
    assert parsed.jibun("신림동") == "신림동 산181-11"


def test_pnu_jibun_needs_a_district_name() -> None:
    """PNU 는 법정동 '코드'만 담는다. 이름은 응답 필드에서 와야 한다."""
    assert parse_pnu(SAMPLE).jibun(None) is None  # type: ignore[union-attr]


@pytest.mark.parametrize("raw", [
    None, "", "  ", "-",
    "114101160000072002",      # 18자리
    "11410116000007200222",    # 20자리
    "114101160000072002X",     # 숫자 아님
    123,
])
def test_pnu_rejects_malformed(raw: object) -> None:
    """19자리 숫자가 아니면 만들지 않는다 — 틀린 좌표보다 없는 편이 낫다."""
    assert parse_pnu(raw) is None


def test_pnu_rejects_zero_main_lot() -> None:
    """본번이 0이면 지번이 성립하지 않는다."""
    assert parse_pnu("1141011600000000000") is None


def test_pnu_length_constant_matches() -> None:
    assert PNU_LENGTH == 19
    assert len(SAMPLE) == PNU_LENGTH


def test_pnu_is_immutable() -> None:
    parsed = parse_pnu(SAMPLE)
    assert parsed is not None
    with pytest.raises((AttributeError, TypeError)):
        parsed.main_no = 1  # type: ignore[misc]


# ── 실데이터 ────────────────────────────────────────────────────────────


def test_pnu_on_real_rows() -> None:
    """PNU 가 있는 행은 전부 파싱되고, 법정동코드가 10자리여야 한다."""
    from tests.conftest import load_fixture
    rows = load_fixture("list_many")["body"]["items"]["item"]

    seen = 0
    for row in rows:
        parsed = parse_pnu(row.get("ltnoPnu"))
        if parsed is None:
            continue
        seen += 1
        assert len(parsed.legal_dong_code) == LEGAL_DONG_CODE_LENGTH
        assert parsed.jibun(row["lctnEmdNm"]) is not None
    assert seen > 0, "PNU 를 가진 표본이 없다"
