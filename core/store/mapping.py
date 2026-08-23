"""수집 결과 → `onbid_cltr` 행 매핑 (SPEC §7).

M2 정규화 모듈들을 엮어 DB 한 행을 만든다. 여기서 지키는 규약:

- **원본은 손대지 않고** ``raw_payload`` 에 통째로 (F1.3)
- 금액 파싱 실패는 null + 원문 보존 (F4.7)
- ``2999`` 는 null + ``bid_date_tbd`` (§7.1)
- Y/N 은 boolean, PNU 는 **text** (선행 0 보존)
- 최저가율은 **자체 계산** — 온비드 비율 필드는 채움률 0% (F4.5·F4.9)
"""

from datetime import UTC, datetime
from typing import Any, Final

from core.normalizer.address import select_address
from core.normalizer.amounts import parse_amount
from core.normalizer.datetimes import parse_datetime
from core.normalizer.status import status_of
from core.onbid.collector import CollectedItem
from core.onbid.links import detail_url
from core.onbid.parser import as_bool_yn, as_float, as_str

RATE_PRECISION: Final = 5
"""``numeric(8,5)`` 에 맞춘 반올림 자릿수."""


def _rate(min_bid_amt: int | None, appraisal_amt: int | None) -> float | None:
    """최저가율을 계산한다.

    감정가가 없거나 0이면 ``None`` — 0으로 나누지 않는다 (F4.5).
    **1.0 을 넘을 수 있다** (실측 최대 1.502, 9.8%). 클램프하지 않는다.
    """
    if min_bid_amt is None or not appraisal_amt:
        return None
    return round(min_bid_amt / appraisal_amt, RATE_PRECISION)


def to_cltr_row(item: CollectedItem) -> dict[str, Any] | None:
    """수집 항목을 `onbid_cltr` 행으로 바꾼다.

    Args:
        item: 수집한 물건.

    Returns:
        컬럼명 → 값. **복합키를 만들 수 없으면 ``None``** — 적재할 수 없는 행이다.
    """
    raw = item.raw
    cltr_mng_no = as_str(raw.get("cltrMngNo"))
    pbct_cdtn_no = as_str(raw.get("pbctCdtnNo"))
    if cltr_mng_no is None or pbct_cdtn_no is None:
        return None

    appraisal = parse_amount(raw.get("apslEvlAmt"))
    min_bid = parse_amount(raw.get("lowstBidPrcIndctCont"))
    bid_start = parse_datetime(raw.get("cltrBidBgngDt"))
    bid_end = parse_datetime(raw.get("cltrBidEndDt"))
    address = select_address(raw)
    status = status_of(raw)
    now = datetime.now(UTC)

    return {
        # 키·식별자
        "cltr_mng_no": cltr_mng_no,
        "pbct_cdtn_no": pbct_cdtn_no,
        "onbid_cltr_no": as_str(raw.get("onbidCltrno")),
        "onbid_pbanc_no": as_str(raw.get("onbidPbancNo")),
        "pbct_no": as_str(raw.get("pbctNo")),
        "pbct_nsq": as_str(raw.get("pbctNsq")),
        "pbct_sn": as_str(raw.get("pbctsn")),
        "cltr_nm": as_str(raw.get("onbidCltrNm")),
        # 주소·위치 — 좌표는 M4 지오코딩이 채운다
        "jibun_addr": address.query if address else None,
        "road_addr": as_str(raw.get("cltrRadr")),
        "sd_nm": as_str(raw.get("lctnSdnm")),
        "sgg_nm": as_str(raw.get("lctnSggnm")),
        "emd_nm": as_str(raw.get("lctnEmdNm")),
        "ltno_pnu": as_str(raw.get("ltnoPnu")),
        "rdnm_pnu": as_str(raw.get("rdnmPnu")),
        "addr_source": address.source.value if address else None,
        # 분류
        "prpt_div_cd": as_str(raw.get("prptDivCd")),
        "prpt_div_nm": as_str(raw.get("prptDivNm")),
        "dsps_mthod_cd": as_str(raw.get("dspsMthodCd")),
        "usg_lcls_id": as_str(raw.get("cltrUsgLclsCtgrId")),
        "usg_mcls_id": as_str(raw.get("cltrUsgMclsCtgrId")),
        "usg_scls_id": as_str(raw.get("cltrUsgSclsCtgrId")),
        "usg_lcls_nm": as_str(raw.get("cltrUsgLclsCtgrNm")),
        "usg_mcls_nm": as_str(raw.get("cltrUsgMclsCtgrNm")),
        "usg_scls_nm": as_str(raw.get("cltrUsgSclsCtgrNm")),
        # 금액·비율
        "appraisal_amt": appraisal.value,
        "min_bid_amt": min_bid.value,
        "min_bid_amt_text": min_bid.text,
        "min_bid_rate": _rate(min_bid.value, appraisal.value),
        # 진행 상태
        "fail_cnt": _int(raw.get("usbdNft")),
        "bid_prgn_cnt": _int(raw.get("bidPrgnNft")),
        "bid_start": bid_start.value,
        "bid_end": bid_end.value,
        "bid_date_tbd": bid_start.is_tbd or bid_end.is_tbd,
        "pbct_stat_cd": as_str(raw.get("pbctStatCd")),
        "pbct_stat_nm": as_str(raw.get("pbctStatNm")),
        "cltr_stat_mng_cd": as_str(raw.get("cltrStatMngCd")),
        "status": status.value if status else None,
        # 물건 속성
        "land_sqms": as_float(raw.get("landSqms")),
        "bld_sqms": as_float(raw.get("bldSqms")),
        "share_yn": as_bool_yn(raw.get("alcYn")),
        "batch_bid_yn": as_bool_yn(raw.get("batcBidYn")),
        "pvct_trgt_yn": as_bool_yn(raw.get("pvctTrgtYn")),
        "crtn_yn": as_bool_yn(raw.get("crtnYn")),
        "org_nm": as_str(raw.get("orgNm")),
        "rqst_org_nm": as_str(raw.get("rqstOrgNm")),
        "thumb_url": as_str(raw.get("thnlImgUrlAdr")),
        # 메타
        "onbid_url": detail_url(raw),
        "mdfcn_dt": parse_datetime(raw.get("mdfcnDt")).value,
        "raw_payload": dict(raw),
        "last_seen_at": now,
        "synced_at": now,
    }


def _int(value: Any) -> int | None:
    """횟수 필드용 정수 변환. 금액과 달리 음수를 따로 막지 않는다."""
    parsed = parse_amount(value)
    return parsed.value
