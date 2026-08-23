"""온비드 응답 파서 테스트.

기대값은 `tests/fixtures/onbid/` 의 **실응답**에 근거한다.
파서는 전송 계층의 모양만 다룬다 — 주소 선택·상태 파생·2999 sentinel 같은 도메인 규칙은
`core/normalizer` 의 몫이다.
"""

from typing import Any

import pytest

from core.onbid.parser import (
    PageInfo,
    as_bool_yn,
    as_float,
    as_int,
    as_str,
    items_of,
    page_info,
    sub_list,
)
from tests.conftest import load_fixture

# ── items.item 모양 흡수 ─────────────────────────────────────────────────


def test_parser_items_of_single_item_is_still_a_list() -> None:
    """실측: JSON 모드에서는 numOfRows=1 이어도 item 이 배열로 온다."""
    payload = load_fixture("list_single")
    items = items_of(payload)
    assert len(items) == 1
    assert items[0]["cltrMngNo"]


def test_parser_items_of_multiple_items() -> None:
    payload = load_fixture("list_many")
    assert len(items_of(payload)) == 3


def test_parser_items_of_empty_response_has_no_body_at_all() -> None:
    """실측: 0건이면 body 가 통째로 없고 result 봉투만 온다. KeyError 를 내면 안 된다."""
    payload = load_fixture("list_empty")
    assert "body" not in payload
    assert items_of(payload) == []


def test_parser_items_of_wraps_dict_into_list() -> None:
    """XML→JSON 변환에서 단건이 dict 로 오는 경우를 대비한다 (활용가이드 §4 경고)."""
    payload = {"body": {"items": {"item": {"cltrMngNo": "x"}}}}
    assert items_of(payload) == [{"cltrMngNo": "x"}]


@pytest.mark.parametrize("payload", [
    {},
    {"body": {}},
    {"body": {"items": {}}},
    {"body": {"items": None}},
    {"body": {"items": {"item": None}}},
    {"body": {"items": {"item": []}}},
    None,
    "문자열 응답",
])
def test_parser_items_of_returns_empty_for_missing_shapes(payload: Any) -> None:
    assert items_of(payload) == []


def test_parser_items_of_drops_non_dict_entries() -> None:
    payload = {"body": {"items": {"item": [{"a": 1}, "쓰레기", None]}}}
    assert items_of(payload) == [{"a": 1}]


# ── 페이지 정보 ──────────────────────────────────────────────────────────


def test_parser_page_info_from_real_response() -> None:
    info = page_info(load_fixture("list_many"))
    assert info.page_no == 1
    assert info.num_of_rows == 3
    assert info.total_count > 3


def test_parser_page_info_of_empty_response_is_zero() -> None:
    """0건 응답에는 body 가 없다. total_count 0 으로 해석해 순회를 끝낼 수 있어야 한다."""
    info = page_info(load_fixture("list_empty"))
    assert info == PageInfo(total_count=0, page_no=None, num_of_rows=None)


def test_parser_page_info_coerces_string_numbers() -> None:
    payload = {"body": {"totalCount": "1234", "pageNo": "2", "numOfRows": "100"}}
    assert page_info(payload) == PageInfo(total_count=1234, page_no=2, num_of_rows=100)


def test_parser_page_info_has_more_pages() -> None:
    assert PageInfo(total_count=250, page_no=1, num_of_rows=100).has_more is True
    assert PageInfo(total_count=250, page_no=3, num_of_rows=100).has_more is False
    assert PageInfo(total_count=0, page_no=None, num_of_rows=None).has_more is False


# ── 중첩 배열 ────────────────────────────────────────────────────────────


def test_parser_sub_list_reads_bid_history() -> None:
    """회차별 유찰 이력. 유찰 1회 물건도 이전 회차 이력이 여러 행 딸려 온다."""
    item = items_of(load_fixture("bid_detail_usbd1"))[0]
    rounds = sub_list(item, "prcnBidClgList")
    assert len(rounds) > 1
    assert {"pbctNsq", "lowstBidPrcIndctCont"} <= set(rounds[0])


def test_parser_sub_list_wraps_dict() -> None:
    assert sub_list({"xs": {"a": 1}}, "xs") == [{"a": 1}]


@pytest.mark.parametrize("item", [{}, {"xs": None}, {"xs": []}, {"xs": "문자열"}])
def test_parser_sub_list_returns_empty_for_missing(item: dict[str, Any]) -> None:
    assert sub_list(item, "xs") == []


def test_parser_empty_nested_list_stays_empty() -> None:
    """실측: 평가항목이 없는 물건은 prpslEvlItemClgList 가 빈 배열로 온다."""
    item = items_of(load_fixture("bid_detail_usbd1"))[0]
    assert sub_list(item, "prpslEvlItemClgList") == []


# ── 형변환 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [
    ("953089200", 953089200),
    (953089200, 953089200),
    ("1,244,143,800", 1244143800),
    (" 42 ", 42),
    ("0", 0),
    (0, 0),
    ("-5", -5),
])
def test_parser_as_int_parses_numbers(raw: Any, expected: int) -> None:
    assert as_int(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "-", "비공개", "N/A", "12.5", [], {}])
def test_parser_as_int_returns_none_for_non_numbers(raw: Any) -> None:
    """실측: 최저입찰가가 '비공개' 문자열로 오는 경우가 있다. 예외 대신 None."""
    assert as_int(raw) is None


def test_parser_as_int_rejects_float_input_silently() -> None:
    """소수는 정수 필드가 아니다. 반올림해서 조용히 값을 바꾸지 않는다."""
    assert as_int(12.5) is None
    assert as_int(12.0) == 12


@pytest.mark.parametrize(("raw", "expected"), [
    ("84.46", 84.46),
    (84.46, 84.46),
    ("28", 28.0),
    ("1,234.5", 1234.5),
])
def test_parser_as_float_parses_numbers(raw: Any, expected: float) -> None:
    assert as_float(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "-", "없음", [], {}])
def test_parser_as_float_returns_none_for_non_numbers(raw: Any) -> None:
    assert as_float(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [
    ("서울특별시", "서울특별시"),
    ("  창천동  ", "창천동"),
    (12345, "12345"),
])
def test_parser_as_str_keeps_meaningful_values(raw: Any, expected: str) -> None:
    assert as_str(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "-"])
def test_parser_as_str_treats_blank_sentinels_as_none(raw: Any) -> None:
    """실측: 빈 값을 ''·' '·'-' 세 가지로 표현한다. 전부 결측으로 통일한다."""
    assert as_str(raw) is None


def test_parser_as_str_can_keep_hyphen_when_asked() -> None:
    """'-' 가 의미를 갖는 필드를 만나면 sentinel 처리를 끌 수 있어야 한다."""
    assert as_str("-", blank_sentinels=()) == "-"


@pytest.mark.parametrize(("raw", "expected"), [
    ("Y", True), ("y", True), ("N", False), ("n", False), (" Y ", True),
])
def test_parser_as_bool_yn_maps_yn(raw: Any, expected: bool) -> None:
    assert as_bool_yn(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "-", "  ", "T", "1", 1, True])
def test_parser_as_bool_yn_returns_none_for_anything_else(raw: Any) -> None:
    """Y/N 외의 값을 참으로 넘겨짚지 않는다."""
    assert as_bool_yn(raw) is None


# ── 실데이터 통합 ────────────────────────────────────────────────────────


def test_parser_coerces_real_listing_row() -> None:
    """실응답 한 행을 형변환해도 값이 깨지지 않는다."""
    row = items_of(load_fixture("list_many"))[0]

    assert as_str(row["cltrMngNo"])
    appraisal = as_int(row["apslEvlAmt"])
    assert appraisal is None or appraisal > 0
    assert as_int(row["usbdNft"]) is not None
    assert as_bool_yn(row["alcYn"]) in (True, False)
    assert as_str(row["lctnEmdNm"]) is not None  # 실측 결측률 0%


def test_parser_unfilled_rate_fields_become_none() -> None:
    """실측: 온비드가 주는 비율 필드는 채움률 0% 라 전부 결측이어야 한다 (SPEC F4.9)."""
    row = items_of(load_fixture("list_many"))[0]
    assert as_float(row.get("apslPrcCtrsLowstBidRto")) is None
    assert as_float(row.get("feeRate")) is None
