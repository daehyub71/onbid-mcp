"""온비드 원문 링크 조립 테스트 (SPEC §8.2 `onbid_url`, D5).

URL 규칙은 실호출로 확정했다 (`docs/API_FINDINGS.md`). 필요한 식별자 4개는 모두
물건목록 응답에 **채움률 100%** 로 들어 있으므로 별도 조회 없이 조립할 수 있다.
"""

import urllib.parse

import pytest

from core.onbid.links import DETAIL_URL, REQUIRED_ID_FIELDS, detail_url

ROW = {
    "cltrMngNo": "2022-08218-007",
    "pbctCdtnNo": 6162684,
    "onbidCltrno": 2030560,
    "onbidPbancNo": 902219,
    "pbctNo": 10093226,
    "onbidCltrNm": "서울특별시 마포구 연남동 245-42",
}


def query_of(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def test_links_uses_onbid_detail_endpoint() -> None:
    url = detail_url(ROW)
    assert url is not None
    assert url.startswith(DETAIL_URL)
    assert urllib.parse.urlparse(url).hostname == "www.onbid.co.kr"


def test_links_carries_exactly_the_four_identifiers() -> None:
    """실측: 넷 중 하나라도 빠지면 온비드가 HTTP 500 을 낸다."""
    params = query_of(detail_url(ROW) or "")
    assert set(params) == set(REQUIRED_ID_FIELDS)


def test_links_values_come_from_the_row() -> None:
    params = query_of(detail_url(ROW) or "")
    assert params["onbidCltrno"] == "2030560"
    assert params["onbidPbancNo"] == "902219"
    assert params["pbctNo"] == "10093226"
    assert params["pbctCdtnNo"] == "6162684"


def test_links_accepts_numeric_and_string_ids() -> None:
    """응답이 숫자로 주는 필드와 문자열로 주는 필드가 섞여 있다."""
    as_text = {**ROW, "onbidCltrno": "2030560", "pbctNo": "10093226"}
    assert detail_url(as_text) == detail_url(ROW)


@pytest.mark.parametrize("missing", sorted(REQUIRED_ID_FIELDS))
def test_links_returns_none_when_an_identifier_is_missing(missing: str) -> None:
    """조립할 수 없으면 깨진 링크를 만들지 않고 None 을 준다.

    HTTP 500 이 나는 URL 을 사용자에게 주는 것보다 링크가 없는 편이 낫다.
    """
    row = {k: v for k, v in ROW.items() if k != missing}
    assert detail_url(row) is None


@pytest.mark.parametrize("blank", ["", "  ", None, "-"])
def test_links_treats_blank_identifier_as_missing(blank: object) -> None:
    assert detail_url({**ROW, "pbctNo": blank}) is None


def test_links_ignores_unrelated_fields() -> None:
    noisy = {**ROW, "apslEvlAmt": 489000000, "lctnEmdNm": "연남동"}
    assert detail_url(noisy) == detail_url(ROW)


def test_links_returns_none_for_non_mapping() -> None:
    assert detail_url(None) is None
    assert detail_url("문자열") is None


def test_links_url_is_stable() -> None:
    """같은 입력이면 같은 URL — 파라미터 순서가 흔들리지 않는다."""
    assert detail_url(ROW) == detail_url(dict(reversed(list(ROW.items()))))
