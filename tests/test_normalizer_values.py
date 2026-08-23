"""금액·일시 정규화 테스트 (F4.7, SPEC §7.1).

실측 근거:

- 금액 비수치는 전량 6,910건 중 **`"비공개"` 1건**뿐이다. 드물지만 정수 캐스팅을 터뜨린다.
- 일시는 세 형식이 섞인다 — `yyyyMMddHHmm`(입찰일시) · `yyyyMMddHHmmss`(수정일시) ·
  `yyyy/MM/dd`(배분요구종기). **타임존 표기가 없어 KST로 간주해야 한다.**
- 입찰일시에 `2999...` sentinel 이 18건(0.26%) 있다. 일정 미정을 뜻한다.
"""

from datetime import UTC, datetime

import pytest

from core.normalizer.amounts import Amount, parse_amount
from core.normalizer.datetimes import KST, ParsedDateTime, parse_datetime, to_iso

# ── 금액 (F4.7) ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "value"), [
    ("953089200", 953089200),
    (953089200, 953089200),
    ("1,244,143,800", 1244143800),
    (" 42 ", 42),
    ("0", 0),
])
def test_amount_parses_numbers(raw: object, value: int) -> None:
    parsed = parse_amount(raw)
    assert parsed.value == value
    assert parsed.text is None
    assert parsed.is_disclosed


def test_amount_keeps_original_when_not_a_number() -> None:
    """실측: 최저입찰가가 `"비공개"` 로 오는 물건이 있다 (6,910건 중 1건)."""
    parsed = parse_amount("비공개")
    assert parsed.value is None
    assert parsed.text == "비공개"
    assert not parsed.is_disclosed


def test_amount_missing_value_has_no_text() -> None:
    """값이 아예 없는 것과 '비공개'로 가려진 것은 다르다.

    전자는 데이터가 없는 것이고 후자는 온비드가 의도적으로 감춘 것이다.
    보존할 원문이 없으면 `text` 도 비운다.
    """
    parsed = parse_amount(None)
    assert parsed.value is None
    assert parsed.text is None
    assert not parsed.is_disclosed


@pytest.mark.parametrize("raw", ["", "   ", "-"])
def test_amount_treats_blank_as_missing(raw: str) -> None:
    assert parse_amount(raw) == Amount(value=None, text=None)


def test_amount_rejects_negative() -> None:
    """금액이 음수일 수 없다. 조용히 받아들이면 최저가율이 음수가 된다."""
    parsed = parse_amount("-500")
    assert parsed.value is None
    assert parsed.text == "-500"


def test_amount_rejects_decimal() -> None:
    """원 단위 정수 필드다. 반올림해서 값을 바꾸지 않는다."""
    parsed = parse_amount("1234.5")
    assert parsed.value is None
    assert parsed.text == "1234.5"


def test_amount_is_immutable() -> None:
    with pytest.raises((AttributeError, TypeError)):
        parse_amount("100").value = 1  # type: ignore[misc]


# ── 일시 ────────────────────────────────────────────────────────────────


def test_datetime_parses_bid_timestamp() -> None:
    """입찰일시는 `yyyyMMddHHmm` 12자리다."""
    parsed = parse_datetime("202508181600")
    assert parsed.value == datetime(2025, 8, 18, 16, 0, tzinfo=KST)
    assert not parsed.is_tbd


def test_datetime_parses_modified_timestamp() -> None:
    """수정일시는 `yyyyMMddHHmmss` 14자리다."""
    assert parse_datetime("20240227101419").value == datetime(2024, 2, 27, 10, 14, 19, tzinfo=KST)


def test_datetime_parses_plain_date() -> None:
    assert parse_datetime("20260928").value == datetime(2026, 9, 28, 0, 0, tzinfo=KST)


def test_datetime_parses_slash_date() -> None:
    """실측: 배분요구종기만 `yyyy/MM/dd` 로 온다."""
    assert parse_datetime("2026/09/28").value == datetime(2026, 9, 28, 0, 0, tzinfo=KST)


def test_datetime_is_timezone_aware_in_kst() -> None:
    """응답에 타임존 표기가 없다. KST 로 간주하지 않으면 9시간이 어긋난다."""
    value = parse_datetime("202508181600").value
    assert value is not None
    assert value.utcoffset() is not None
    assert value.astimezone(UTC).hour == 7


def test_datetime_marks_sentinel_as_tbd() -> None:
    """실측: `2999...` 는 일정 미정이다 (18건). 그대로 두면 마감일 정렬이 오염된다."""
    parsed = parse_datetime("299901021600")
    assert parsed.value is None
    assert parsed.is_tbd


def test_datetime_sentinel_threshold_is_documented() -> None:
    """2999만 잡는 게 아니라 먼 미래를 모두 미정으로 본다."""
    assert parse_datetime("999901021600").is_tbd
    assert not parse_datetime("205001021600").is_tbd


@pytest.mark.parametrize("raw", [None, "", "  ", "-", "미정", "2026", "20261301", "2026/13/01"])
def test_datetime_returns_empty_for_unparseable(raw: object) -> None:
    parsed = parse_datetime(raw)
    assert parsed.value is None
    assert not parsed.is_tbd


def test_datetime_rejects_impossible_date() -> None:
    """2월 30일 같은 값은 파싱 실패로 처리한다."""
    assert parse_datetime("20260230").value is None


def test_datetime_result_is_immutable() -> None:
    with pytest.raises((AttributeError, TypeError)):
        parse_datetime("202508181600").is_tbd = True  # type: ignore[misc]


# ── ISO 직렬화 ──────────────────────────────────────────────────────────


def test_to_iso_emits_kst_offset() -> None:
    """MCP 응답은 ISO8601 + `+09:00` 으로 내보낸다 (SPEC §7.1)."""
    assert to_iso(parse_datetime("202508181600").value) == "2025-08-18T16:00:00+09:00"


def test_to_iso_of_none_is_none() -> None:
    assert to_iso(None) is None


def test_to_iso_converts_other_zones_to_kst() -> None:
    assert to_iso(datetime(2025, 8, 18, 7, 0, tzinfo=UTC)) == "2025-08-18T16:00:00+09:00"


# ── 실데이터 ────────────────────────────────────────────────────────────


def test_values_on_real_rows() -> None:
    from tests.conftest import load_fixture
    rows = load_fixture("list_many")["body"]["items"]["item"]

    for row in rows:
        low = parse_amount(row["lowstBidPrcIndctCont"])
        assert low.value is not None or low.text is not None

        end = parse_datetime(row["cltrBidEndDt"])
        assert end.value is not None or end.is_tbd

        modified = parse_datetime(row["mdfcnDt"])
        assert modified.value is not None


def test_datetime_handles_round_history() -> None:
    """회차 이력의 개찰일시도 같은 12자리 형식이다."""
    from tests.conftest import load_fixture
    item = load_fixture("bid_detail_usbd1")["body"]["items"]["item"][0]

    for entry in item["prcnBidClgList"]:
        assert parse_datetime(entry["cltrOpbdDt"]).value is not None


def test_parsed_datetime_equality() -> None:
    assert parse_datetime("202508181600") == ParsedDateTime(
        value=datetime(2025, 8, 18, 16, 0, tzinfo=KST), is_tbd=False
    )
