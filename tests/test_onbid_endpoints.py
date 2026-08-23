"""온비드 엔드포인트 정의 테스트 (SPEC §6.4).

여기 기대값은 전부 **실호출로 검증된 것**이다 (docs/API_FINDINGS.md §1).
활용가이드 본문의 오퍼레이션명은 일부가 틀렸으므로 가이드를 근거로 이 테스트를 고치지 않는다.
"""

import pytest

from core.onbid.endpoints import (
    BASE_URL,
    COMMON_REQUIRED_PARAMS,
    ENDPOINTS,
    Endpoint,
)


def test_endpoints_base_url_is_https_apis_data_go_kr() -> None:
    """기관코드 B010003, HTTPS. 레거시 계열의 http://openapi.onbid.co.kr 이 아니다."""
    assert BASE_URL == "https://apis.data.go.kr/B010003"


def test_endpoints_realestate_list_operation_has_suffix_two() -> None:
    """가이드 본문은 getRlstCltrList 라고 하지만 실제로는 서비스명·오퍼레이션명 양쪽에 2가 붙는다.

    접미사 없이 호출하면 NO_OPENAPI_SERVICE_ERROR(12) + HTTP 400 이 반환된다.
    """
    ep = ENDPOINTS["realestate_list"]
    assert ep.service == "OnbidRlstListSrvc2"
    assert ep.operation == "getRlstCltrList2"
    assert ep.url == "https://apis.data.go.kr/B010003/OnbidRlstListSrvc2/getRlstCltrList2"


def test_endpoints_bid_detail_resolved_by_live_probe() -> None:
    """입찰정보 서비스명은 추측으로 찾지 못했고 포털 미리보기로 확정했다 (D8)."""
    ep = ENDPOINTS["bid_detail"]
    assert ep.service == "OnbidCltrBidDtlSrvc2"
    assert ep.operation == "getCltrBidInf2"


def test_endpoints_code_service_has_no_version_suffix() -> None:
    """코드 및 주소 조회서비스는 v1.0 이라 서비스명에 2가 붙지 않는다."""
    ep = ENDPOINTS["usage_code"]
    assert ep.service == "OnbidCodeSrvc"
    assert not ep.service.endswith("2")


@pytest.mark.parametrize("name", sorted(ENDPOINTS))
def test_endpoints_every_endpoint_requires_common_params(name: str) -> None:
    """serviceKey·pageNo·numOfRows·resultType 은 전 서비스 공통 필수다."""
    assert ENDPOINTS[name].required_params >= COMMON_REQUIRED_PARAMS


@pytest.mark.parametrize("name", sorted(ENDPOINTS))
def test_endpoints_url_is_built_from_base_service_operation(name: str) -> None:
    ep = ENDPOINTS[name]
    assert ep.url == f"{BASE_URL}/{ep.service}/{ep.operation}"


def test_endpoints_realestate_list_requires_prpt_div_and_pvct_trgt() -> None:
    """pvctTrgtYn 은 단일값 필수라 전량 수집에 Y·N 2회 순회가 필요하다 (F1.9)."""
    required = ENDPOINTS["realestate_list"].required_params
    assert "prptDivCd" in required
    assert "pvctTrgtYn" in required


def test_endpoints_bid_detail_requires_composite_key() -> None:
    """물건관리번호 단독으로는 조회되지 않는다. 공매조건번호가 함께 필요하다."""
    required = ENDPOINTS["bid_detail"].required_params
    assert {"cltrMngNo", "pbctCdtnNo"} <= required


def test_endpoints_pbanc_list_requires_opbd_date_window() -> None:
    """공고목록만 개찰일 구간이 필수다. 수집 본체인 물건목록은 기간이 옵션이다."""
    required = ENDPOINTS["pbanc_list"].required_params
    assert {"opbdDtStart", "opbdDtEnd"} <= required
    assert "opbdDtStart" not in ENDPOINTS["realestate_list"].required_params


def test_endpoints_missing_params_reports_only_absent_keys() -> None:
    ep = ENDPOINTS["bid_detail"]
    given = {"serviceKey": "k", "pageNo": 1, "numOfRows": 10, "resultType": "json",
             "cltrMngNo": "2024-0100-008372"}
    assert ep.missing_params(given) == frozenset({"pbctCdtnNo"})


def test_endpoints_missing_params_empty_when_satisfied() -> None:
    ep = ENDPOINTS["usage_code"]
    given = {"serviceKey": "k", "pageNo": 1, "numOfRows": 10, "resultType": "json"}
    assert ep.missing_params(given) == frozenset()


def test_endpoints_missing_params_treats_none_as_absent() -> None:
    """값이 None이면 쿼리스트링에 실리지 않으므로 누락으로 본다."""
    ep = ENDPOINTS["usage_code"]
    given = {"serviceKey": "k", "pageNo": 1, "numOfRows": None, "resultType": "json"}
    assert ep.missing_params(given) == frozenset({"numOfRows"})


def test_endpoints_bid_detail_declares_daily_traffic_limit() -> None:
    """물건당 1회 호출 · 일 1,000건. 전량 순회가 불가능하다는 근거 (F1.11)."""
    assert ENDPOINTS["bid_detail"].daily_traffic == 1000


def test_endpoints_are_immutable() -> None:
    ep = ENDPOINTS["realestate_list"]
    with pytest.raises((AttributeError, TypeError)):
        ep.service = "changed"  # type: ignore[misc]


def test_endpoints_registry_covers_all_approved_services() -> None:
    """활용신청한 5종 + 코드/주소의 오퍼레이션 2개 = 6개 엔드포인트."""
    assert set(ENDPOINTS) == {
        "realestate_list",
        "realestate_detail",
        "bid_detail",
        "pbanc_list",
        "usage_code",
        "address",
    }
    assert all(isinstance(ep, Endpoint) for ep in ENDPOINTS.values())
