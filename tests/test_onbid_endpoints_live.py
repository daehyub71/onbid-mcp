"""엔드포인트 상수가 실제 API와 일치하는지 검증한다 (`pytest -m live`).

`endpoints.py` 의 값은 순수 상수라 단위 테스트로는 "우리가 적은 대로인지"만 확인된다.
경로가 실제로 살아 있는지는 호출해 봐야 알 수 있고, 실제로 활용가이드 본문이 틀려서
`getRlstCltrList` 로는 호출되지 않았다. 이 테스트가 그 회귀를 막는다.

온비드 쿼터를 소모하므로 기본 실행에서 제외된다 (`addopts = -m 'not live and not db'`).
"""

import json
import os
import pathlib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from core.onbid.endpoints import ENDPOINTS

pytestmark = pytest.mark.live

NO_SERVICE_ERROR = "12"
"""NO_OPENAPI_SERVICE_ERROR. 서비스명·오퍼레이션명이 틀렸을 때 반환된다."""


def _service_key() -> str:
    """`.env` 에서 서비스키를 읽어 정규화한다 (SPEC §6.4).

    포털이 Encoding/Decoding 두 표현으로 보여주지만 같은 키다. 서비스키에 `%` 가
    포함되지 않으므로 `unquote` 는 멱등하고, 어느 쪽을 받아도 안전하다.
    """
    raw = os.environ.get("ONBID_SERVICE_KEY", "")
    if not raw:
        env = pathlib.Path(__file__).resolve().parents[1] / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("ONBID_SERVICE_KEY="):
                    raw = line.split("=", 1)[1].strip()
                    break
    if not raw:
        pytest.skip("ONBID_SERVICE_KEY 가 없어 live 테스트를 건너뛴다")
    return urllib.parse.unquote(raw)


def _ssl_context() -> ssl.SSLContext:
    """macOS 시스템 파이썬의 인증서 체인 미설정을 certifi로 우회한다."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _call(url: str, params: dict[str, Any]) -> tuple[int, Any]:
    """온비드를 호출해 (HTTP 상태, 파싱된 본문)을 반환한다."""
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def _result_code(payload: Any) -> str | None:
    """응답 봉투 3종에서 결과코드를 뽑는다 (SPEC §6.4.1).

    - 정상:       {"header": {"resultCode": ...}}
    - 게이트웨이: {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": ...}}}
    - 입찰정보:   {"result": {"resultCode": ...}}
    """
    if not isinstance(payload, dict):
        return None
    gateway = payload.get("OpenAPI_ServiceResponse")
    if isinstance(gateway, dict):
        code = gateway.get("cmmMsgHeader", {}).get("returnReasonCode")
        return str(code) if code is not None else None
    for envelope in ("header", "result"):
        block = payload.get(envelope)
        if isinstance(block, dict) and "resultCode" in block:
            return str(block["resultCode"])
    return None


#: 각 엔드포인트를 최소 비용으로 호출하기 위한 인자. numOfRows=1 로 응답을 줄인다.
MINIMAL_ARGS: dict[str, dict[str, Any]] = {
    "realestate_list": {"prptDivCd": "0007", "pvctTrgtYn": "N"},
    "realestate_detail": {"cltrMngNo": "2024-0100-008372"},
    "bid_detail": {"cltrMngNo": "2024-0100-008372", "pbctCdtnNo": "4685522"},
    "pbanc_list": {
        "cltrTypeCd": "0001",
        "prptDivCd": "0007",
        "opbdDtStart": "20260801",
        "opbdDtEnd": "20260831",
    },
    "usage_code": {"upCtgrId": "10000"},
    "address": {"sdnm": "서울특별시"},
}


@pytest.mark.parametrize("name", sorted(ENDPOINTS))
def test_endpoint_path_is_alive(name: str) -> None:
    """경로가 살아 있어야 한다 — NO_OPENAPI_SERVICE_ERROR 가 나오면 상수가 틀린 것이다."""
    endpoint = ENDPOINTS[name]
    params: dict[str, Any] = {
        "serviceKey": _service_key(),
        "pageNo": 1,
        "numOfRows": 1,
        "resultType": "json",
        **MINIMAL_ARGS[name],
    }
    assert endpoint.missing_params(params) == frozenset()

    status, payload = _call(endpoint.url, params)
    code = _result_code(payload)

    assert code != NO_SERVICE_ERROR, (
        f"{name}: 경로가 존재하지 않는다 ({endpoint.service}/{endpoint.operation}). "
        f"HTTP {status} · {str(payload)[:200]}"
    )
    # 03(NODATA_ERROR)은 조회 조건 문제일 뿐 경로는 유효하다.
    assert code in {"00", "03"}, f"{name}: 예상 밖 결과코드 {code} · {str(payload)[:200]}"


def test_realestate_list_guide_operation_name_is_wrong() -> None:
    """가이드 본문의 `getRlstCltrList`(접미사 없음)는 호출되지 않는다.

    이 사실이 뒤집히면(온비드가 별칭을 추가하면) 알림을 받기 위한 테스트다.
    """
    endpoint = ENDPOINTS["realestate_list"]
    wrong_url = endpoint.url.removesuffix("2")
    status, payload = _call(
        wrong_url,
        {
            "serviceKey": _service_key(),
            "pageNo": 1,
            "numOfRows": 1,
            "resultType": "json",
            "prptDivCd": "0007",
            "pvctTrgtYn": "N",
        },
    )
    assert _result_code(payload) == NO_SERVICE_ERROR, (
        f"가이드 표기가 이제 동작한다 — SPEC §6.4 재검토 필요. HTTP {status}"
    )
