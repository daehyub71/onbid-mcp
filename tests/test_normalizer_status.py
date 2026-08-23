"""입찰 상태 파생 테스트 (SPEC §7.1).

온비드는 상태를 **두 가지 방식**으로 준다.

- 물건목록: ``pbctStatCd`` 코드 + ``pbctStatNm`` 이름
- 회차 이력(``prcnBidClgList``): **이름만** — 코드 필드가 없다

그래서 코드 경로와 이름 경로를 모두 지원해야 한다.
"""

import pytest

from core.normalizer.status import (
    AuctionStatus,
    status_from_code,
    status_from_name,
    status_of,
)

# ── 코드 → 상태 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("code", "status"), [
    ("0001", AuctionStatus.OPEN),    # 입찰준비중
    ("0002", AuctionStatus.OPEN),    # 입찰진행중
    ("0009", AuctionStatus.OPEN),    # 수의계약가능
    ("0003", AuctionStatus.CLOSED),  # 입찰마감
    ("0006", AuctionStatus.CLOSED),  # 개찰중
    ("0010", AuctionStatus.WON),     # 낙찰
    ("0011", AuctionStatus.FAILED),  # 유찰
    ("0012", AuctionStatus.CANCELLED),
])
def test_status_from_code_maps_all_eight(code: str, status: AuctionStatus) -> None:
    assert status_from_code(code) is status


def test_status_from_code_groups_pre_bid_with_open() -> None:
    """입찰준비중·수의계약가능은 아직 취득할 수 있으므로 `진행` 으로 묶는다.

    수의계약과 입찰의 구분은 상태가 아니라 `pvct_trgt_yn` 컬럼이 담당한다.
    """
    assert status_from_code("0001") is status_from_code("0009")


@pytest.mark.parametrize("code", [None, "", "-", "9999", "0004", "abcd"])
def test_status_from_code_returns_none_for_unknown(code: object) -> None:
    """모르는 코드를 임의의 상태로 넘겨짚지 않는다."""
    assert status_from_code(code) is None


def test_status_from_code_accepts_unpadded() -> None:
    """숫자로 온 코드도 받아들인다 — 응답 필드 타입이 섞인다 (`1` → `0001`)."""
    assert status_from_code(1) is AuctionStatus.OPEN
    assert status_from_code("11") is AuctionStatus.FAILED


# ── 이름 → 상태 (회차 이력) ─────────────────────────────────────────────


@pytest.mark.parametrize(("name", "status"), [
    ("입찰준비중", AuctionStatus.OPEN),
    ("입찰진행중", AuctionStatus.OPEN),
    ("수의계약가능", AuctionStatus.OPEN),
    ("입찰마감", AuctionStatus.CLOSED),
    ("개찰중", AuctionStatus.CLOSED),
    ("낙찰", AuctionStatus.WON),
    ("유찰", AuctionStatus.FAILED),
    ("취소", AuctionStatus.CANCELLED),
])
def test_status_from_name_maps_history_results(name: str, status: AuctionStatus) -> None:
    """회차 이력은 이름만 주므로 이 경로가 필수다."""
    assert status_from_name(name) is status


def test_status_from_name_tolerates_whitespace() -> None:
    assert status_from_name("  유찰  ") is AuctionStatus.FAILED


@pytest.mark.parametrize("name", [None, "", "-", "알수없음"])
def test_status_from_name_returns_none_for_unknown(name: object) -> None:
    assert status_from_name(name) is None


# ── 행 단위 파생 ────────────────────────────────────────────────────────


def test_status_of_prefers_code() -> None:
    """코드가 이름보다 안정적이다 — 이름은 표기가 바뀔 수 있다."""
    row = {"pbctStatCd": "0011", "pbctStatNm": "입찰준비중"}
    assert status_of(row) is AuctionStatus.FAILED


def test_status_of_falls_back_to_name() -> None:
    """회차 이력에는 코드가 없다."""
    assert status_of({"pbctStatNm": "낙찰"}) is AuctionStatus.WON


def test_status_of_falls_back_when_code_is_unknown() -> None:
    """모르는 코드가 오면 이름이라도 써 본다."""
    assert status_of({"pbctStatCd": "0099", "pbctStatNm": "유찰"}) is AuctionStatus.FAILED


def test_status_of_returns_none_when_both_missing() -> None:
    assert status_of({}) is None
    assert status_of(None) is None


# ── 값 규약 ─────────────────────────────────────────────────────────────


def test_status_values_match_spec_wording() -> None:
    """DB `status` 컬럼에 그대로 들어가는 문자열이다 (SPEC §7.1)."""
    assert {s.value for s in AuctionStatus} == {
        "진행", "마감", "낙찰", "유찰", "취소", "종료추정",
    }


def test_presumed_ended_is_never_derived_from_a_response() -> None:
    """`종료추정` 은 응답에 없는 상태다 — 목록에서 사라졌을 때 적재 계층이 부여한다 (F4.2)."""
    assert status_from_code("0013") is None
    assert status_from_name("종료추정") is None


def test_open_statuses_are_grouped() -> None:
    """조회 필터가 '아직 취득 가능한 물건'을 한 번에 고를 수 있어야 한다."""
    assert AuctionStatus.OPEN.is_open
    assert not AuctionStatus.WON.is_open
    assert not AuctionStatus.PRESUMED_ENDED.is_open


# ── 실데이터 ────────────────────────────────────────────────────────────


def test_status_on_real_rows() -> None:
    from tests.conftest import load_fixture
    for row in load_fixture("list_many")["body"]["items"]["item"]:
        assert status_of(row) is not None


def test_status_on_real_round_history() -> None:
    from tests.conftest import load_fixture
    item = load_fixture("bid_detail_usbd1")["body"]["items"]["item"][0]

    for entry in item["prcnBidClgList"]:
        assert "pbctStatCd" not in entry  # 코드가 없다는 전제를 지킨다
        assert status_of(entry) is not None
