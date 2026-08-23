"""수집 결과 → DB 행 매핑 테스트 (SPEC §7).

M2 정규화 모듈들을 엮어 `onbid_cltr` 한 행을 만든다. 순수 함수라 DB 없이 검증한다.

여기서 지키는 규약:

- 원본은 `raw_payload` 에 **손대지 않고** 통째로 (F1.3)
- 금액 파싱 실패는 null + 원문 보존 (F4.7)
- `2999` 는 null + `bid_date_tbd` (§7.1)
- Y/N 은 boolean, PNU 는 text (선행 0 보존)
- 최저가율은 **자체 계산** (F4.5)
"""

from datetime import datetime
from typing import Any

import pytest

from core.normalizer.datetimes import KST
from core.onbid.collector import CollectedItem
from core.store.mapping import to_cltr_row

RAW = {
    "cltrMngNo": "2024-0100-008372",
    "pbctCdtnNo": 4685522,
    "onbidCltrno": 1746406,
    "onbidPbancNo": 754512,
    "pbctNo": 9783113,
    "pbctNsq": "10",
    "pbctsn": "1",
    "onbidCltrNm": "서울특별시 서대문구 창천동 72-22 근린생활시설",
    "lctnSdnm": "서울특별시",
    "lctnSggnm": "서대문구",
    "lctnEmdNm": "창천동",
    "ltnoPnu": "1141011600000720022",
    "rdnmPnu": "1141030050570400006300000",
    "prptDivCd": "0005",
    "prptDivNm": "기타일반재산",
    "dspsMthodCd": "0001",
    "cltrUsgLclsCtgrId": "10000",
    "cltrUsgMclsCtgrId": "10300",
    "cltrUsgSclsCtgrId": "10301",
    "cltrUsgLclsCtgrNm": "부동산",
    "cltrUsgMclsCtgrNm": "상가용및업무용건물",
    "cltrUsgSclsCtgrNm": "근린생활시설",
    "apslEvlAmt": 1510000000,
    "lowstBidPrcIndctCont": "953089200",
    "usbdNft": 7,
    "bidPrgnNft": 0,
    "cltrBidBgngDt": "202508181400",
    "cltrBidEndDt": "202508201600",
    "pbctStatCd": "0001",
    "pbctStatNm": "입찰준비중",
    "cltrStatMngCd": "0002",
    "landSqms": "9.128",
    "bldSqms": "36.68",
    "alcYn": "N",
    "batcBidYn": "N",
    "pvctTrgtYn": "N",
    "crtnYn": "N",
    "orgNm": "코리아신탁주식회사",
    "rqstOrgNm": None,
    "thnlImgUrlAdr": "https://www.onbid.co.kr/thumb.jpg",
    "mdfcnDt": "20240227101419",
}


def item(**overrides: object) -> CollectedItem:
    return CollectedItem(raw={**RAW, **overrides}, group="N")


@pytest.fixture
def row() -> dict[str, Any]:
    mapped = to_cltr_row(item())
    assert mapped is not None
    return mapped


# ── 키 ──────────────────────────────────────────────────────────────────


def test_mapping_uses_composite_key_as_text(row: dict[str, Any]) -> None:
    """숫자로 오는 `pbctCdtnNo` 도 문자열로 맞춘다 — PK 타입이 text 다."""
    assert row["cltr_mng_no"] == "2024-0100-008372"
    assert row["pbct_cdtn_no"] == "4685522"


def test_mapping_returns_none_without_key() -> None:
    """복합키가 없으면 적재할 수 없다."""
    assert to_cltr_row(CollectedItem(raw={"onbidCltrNm": "이름만"}, group="N")) is None


# ── 원본 보존 (F1.3) ────────────────────────────────────────────────────


def test_mapping_keeps_raw_payload_untouched(row: dict[str, Any]) -> None:
    assert row["raw_payload"] == RAW


def test_mapping_does_not_inject_group_into_raw(row: dict[str, Any]) -> None:
    """수집 그룹은 `pvct_trgt_yn` 이 아니라 응답 값에서 온다."""
    assert row["raw_payload"]["pvctTrgtYn"] == "N"


# ── 금액 (F4.5·F4.7) ────────────────────────────────────────────────────


def test_mapping_parses_amounts(row: dict[str, Any]) -> None:
    assert row["appraisal_amt"] == 1510000000
    assert row["min_bid_amt"] == 953089200
    assert row["min_bid_amt_text"] is None


def test_mapping_preserves_undisclosed_price() -> None:
    row = to_cltr_row(item(lowstBidPrcIndctCont="비공개"))
    assert row is not None
    assert row["min_bid_amt"] is None
    assert row["min_bid_amt_text"] == "비공개"


def test_mapping_computes_min_bid_rate(row: dict[str, Any]) -> None:
    """온비드가 주는 비율 필드는 채움률 0% 라 직접 계산한다 (F4.5·F4.9)."""
    assert row["min_bid_rate"] == pytest.approx(953089200 / 1510000000, abs=1e-5)


def test_mapping_rate_is_none_without_appraisal() -> None:
    row = to_cltr_row(item(apslEvlAmt=None))
    assert row is not None
    assert row["min_bid_rate"] is None


def test_mapping_rate_is_none_when_appraisal_is_zero() -> None:
    """0 으로 나누지 않는다 (F4.5)."""
    row = to_cltr_row(item(apslEvlAmt=0))
    assert row is not None
    assert row["min_bid_rate"] is None


def test_mapping_rate_may_exceed_one() -> None:
    """실측 9.8% 가 100% 를 넘는다. 클램프하지 않는다."""
    row = to_cltr_row(item(apslEvlAmt=100, lowstBidPrcIndctCont="150"))
    assert row is not None
    assert row["min_bid_rate"] == pytest.approx(1.5)


# ── 일시 (§7.1) ─────────────────────────────────────────────────────────


def test_mapping_parses_timestamps_in_kst(row: dict[str, Any]) -> None:
    assert row["bid_start"] == datetime(2025, 8, 18, 14, 0, tzinfo=KST)
    assert row["mdfcn_dt"] == datetime(2024, 2, 27, 10, 14, 19, tzinfo=KST)


def test_mapping_flags_tbd_schedule() -> None:
    """`2999` 를 그대로 저장하면 마감일 정렬이 오염된다."""
    row = to_cltr_row(item(cltrBidBgngDt="299901021000", cltrBidEndDt="299901021600"))
    assert row is not None
    assert row["bid_start"] is None
    assert row["bid_end"] is None
    assert row["bid_date_tbd"] is True


def test_mapping_tbd_is_false_for_normal_dates(row: dict[str, Any]) -> None:
    assert row["bid_date_tbd"] is False


# ── 타입 규약 ───────────────────────────────────────────────────────────


def test_mapping_converts_yn_to_boolean(row: dict[str, Any]) -> None:
    assert row["share_yn"] is False
    assert row["pvct_trgt_yn"] is False


def test_mapping_yn_is_none_when_absent() -> None:
    row = to_cltr_row(item(alcYn=None))
    assert row is not None
    assert row["share_yn"] is None


def test_mapping_keeps_pnu_as_text(row: dict[str, Any]) -> None:
    """정수로 바꾸면 선행 0 이 사라진다."""
    assert row["ltno_pnu"] == "1141011600000720022"
    assert isinstance(row["ltno_pnu"], str)


def test_mapping_parses_areas(row: dict[str, Any]) -> None:
    assert row["land_sqms"] == pytest.approx(9.128)
    assert row["bld_sqms"] == pytest.approx(36.68)


# ── 파생 ────────────────────────────────────────────────────────────────


def test_mapping_derives_status(row: dict[str, Any]) -> None:
    assert row["status"] == "진행"
    assert row["pbct_stat_cd"] == "0001"


def test_mapping_keeps_undocumented_field(row: dict[str, Any]) -> None:
    """활용가이드에 없는 필드도 보존한다 (D16)."""
    assert row["cltr_stat_mng_cd"] == "0002"


def test_mapping_builds_onbid_url(row: dict[str, Any]) -> None:
    assert row["onbid_url"] is not None
    assert "onbidCltrno=1746406" in row["onbid_url"]


def test_mapping_url_is_none_without_all_identifiers() -> None:
    """식별자가 하나라도 없으면 깨진 링크를 만들지 않는다 (F1.15)."""
    row = to_cltr_row(item(pbctNo=None))
    assert row is not None
    assert row["onbid_url"] is None


def test_mapping_selects_geocoding_address(row: dict[str, Any]) -> None:
    """지오코딩에 넣을 주소와 그 출처를 함께 남긴다 (F2.1·F2.7)."""
    assert row["jibun_addr"] == "서울특별시 서대문구 창천동 72-22"
    assert row["addr_source"] == "pnu"


def test_mapping_addr_source_falls_back() -> None:
    row = to_cltr_row(item(ltnoPnu=None))
    assert row is not None
    assert row["addr_source"] == "item_name"


def test_mapping_sets_sync_timestamps(row: dict[str, Any]) -> None:
    """`last_seen_at`·`synced_at` 은 적재 시각이다 (F4.3)."""
    assert row["last_seen_at"] is not None
    assert row["synced_at"] == row["last_seen_at"]


# ── 실데이터 ────────────────────────────────────────────────────────────


def test_mapping_on_real_rows() -> None:
    from tests.conftest import load_fixture
    rows = load_fixture("list_many")["body"]["items"]["item"]

    for raw in rows:
        mapped = to_cltr_row(CollectedItem(raw=raw, group="N"))
        assert mapped is not None
        assert mapped["cltr_mng_no"]
        assert mapped["raw_payload"] == raw
