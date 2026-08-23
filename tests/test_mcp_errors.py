"""오류 매핑 테스트 (F6.5·§8.7).

**코드마다 LLM 이 취할 행동이 다르다.** 쿼터 소진을 일시 장애로 매핑하면 LLM 이 재시도해
남은 쿼터까지 태우고, 조건 오류를 장애로 매핑하면 고쳐야 할 쪽이 요청인데 기다린다.

온비드는 오류를 **두 봉투**로 준다 — 정상 응답 안의 `resultCode`(HTTP 200)와 게이트웨이
`returnReasonCode`(HTTP 400). 둘을 같은 체계로 옮겨야 한다 (§6.4.1).
"""

import pytest

from core.geocoder.kakao import KakaoAuthError, KakaoQuotaExceededError
from core.onbid.client import OnbidApiError, OnbidAuthError, OnbidQuotaExceededError
from onbid_mcp.errors import map_result_code, to_tool_error

# ── 온비드 resultCode → MCP (§8.7 표) ──────────────────────────────────


@pytest.mark.parametrize(("code", "expected"), [
    ("03", "no_result"),
    ("10", "invalid_param"),
    ("11", "invalid_param"),
    ("22", "quota_exceeded"),
    ("21", "upstream_error"),
    ("30", "upstream_error"),
    ("31", "upstream_error"),
    ("01", "upstream_error"),
    ("99", "upstream_error"),
])
def test_result_codes_map_to_the_spec(code: str, expected: str) -> None:
    assert map_result_code(code) == expected


def test_success_code_maps_to_nothing() -> None:
    """정상을 오류로 바꾸면 조회가 통째로 실패한다."""
    assert map_result_code("00") is None


def test_unknown_code_is_upstream_not_invalid() -> None:
    """모르는 코드를 요청 탓으로 돌리면 LLM 이 조건을 헛되이 고친다."""
    assert map_result_code("77") == "upstream_error"


# ── 예외 → MCP ─────────────────────────────────────────────────────────


def test_quota_error_stops_retries() -> None:
    """재시도하면 남은 쿼터까지 태운다."""
    error = to_tool_error(OnbidQuotaExceededError("22", "일일 제한 초과"))

    assert error.code == "quota_exceeded"


def test_kakao_quota_maps_the_same_way(  ) -> None:
    """카카오 앱은 다른 프로젝트와 공유한다 — 재시도가 남의 서비스를 막는다."""
    assert to_tool_error(KakaoQuotaExceededError("쿼터 소진")).code == "quota_exceeded"


def test_key_problem_is_upstream_not_quota() -> None:
    """키 문제는 사용자가 조건을 바꿔서 풀 수 없다 — 운영자가 고쳐야 한다."""
    error = to_tool_error(OnbidAuthError("30", "미등록 키"))

    assert error.code == "upstream_error"
    assert "키" in error.message or "운영" in error.message


def test_kakao_auth_is_upstream() -> None:
    assert to_tool_error(KakaoAuthError("HTTP 401")).code == "upstream_error"


def test_transient_api_error_is_upstream() -> None:
    assert to_tool_error(OnbidApiError("04", "서버 오류")).code == "upstream_error"


def test_no_data_is_not_a_failure() -> None:
    """`03` 은 실패가 아니라 '없음' 이다 — 조건 완화를 제안해야 한다."""
    assert to_tool_error(OnbidApiError("03", "데이터 없음")).code == "no_result"


def test_value_error_is_a_client_mistake() -> None:
    """정렬·상태 오타는 요청 잘못이다. 장애로 주면 LLM 이 기다린다."""
    error = to_tool_error(ValueError("알 수 없는 상태: ['진행중']"))

    assert error.code == "invalid_param"
    assert "진행중" in error.message


def test_unexpected_exception_is_upstream() -> None:
    """모르는 예외를 요청 탓으로 돌리지 않는다."""
    assert to_tool_error(RuntimeError("무언가 터짐")).code == "upstream_error"


def test_message_never_leaks_internals() -> None:
    """예외 문자열에 접속 정보가 섞일 수 있다 — 그대로 LLM 에게 보내지 않는다."""
    error = to_tool_error(RuntimeError(
        "connection to postgresql://user:pw@host/db failed"))

    assert "postgresql://" not in error.message
