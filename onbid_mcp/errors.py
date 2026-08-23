"""오류 매핑 (F6.5·§8.7).

**코드마다 LLM 이 취할 행동이 다르다.** 쿼터 소진을 일시 장애로 매핑하면 LLM 이 재시도해
남은 쿼터까지 태우고, 조건 오류를 장애로 매핑하면 고쳐야 할 쪽이 요청인데 기다린다.

온비드는 오류를 **두 봉투**로 준다 — 정상 응답 안의 `resultCode`(HTTP 200)와 게이트웨이
`returnReasonCode`(HTTP 400). `core.onbid.client` 가 이미 둘을 같은 예외 체계로 흡수했으므로,
여기서는 그 예외를 MCP 오류로 옮긴다.

**예외 문자열을 그대로 내보내지 않는다.** 접속 문자열이나 내부 경로가 섞일 수 있고, 그대로
LLM 에게 가면 대화에 남는다 (N4.1).
"""

import logging
import re
from typing import Final

from core.geocoder.kakao import KakaoAuthError, KakaoError, KakaoQuotaExceededError
from core.onbid.client import (
    OnbidApiError,
    OnbidAuthError,
    OnbidError,
    OnbidQuotaExceededError,
)
from onbid_mcp.common import ToolError

logger = logging.getLogger(__name__)

#: 온비드 결과코드 → MCP 오류 (§8.7 표).
RESULT_CODE_MAP: Final = {
    "00": None,          # 정상
    "03": "no_result",
    "10": "invalid_param",
    "11": "invalid_param",
    "22": "quota_exceeded",
}

#: 키 문제. 사용자가 조건을 바꿔서 풀 수 없다 — 운영자가 고쳐야 한다.
KEY_PROBLEM_CODES: Final = frozenset({"20", "21", "30", "31", "32", "33"})

FALLBACK_CODE: Final = "upstream_error"
"""모르는 코드는 **요청 탓으로 돌리지 않는다** — LLM 이 조건을 헛되이 고치게 된다."""

#: 메시지에서 지워야 할 것들. 접속 문자열이 예외에 섞여 나오는 일이 흔하다.
_SECRETISH: Final = re.compile(
    r"(postgres(?:ql)?://\S+|serviceKey=\S+|KakaoAK\s+\S+|/Users/\S+)", re.I
)

GENERIC_MESSAGE: Final = "조회 중 내부 오류가 발생했습니다"


def map_result_code(code: str | None) -> str | None:
    """온비드 결과코드를 MCP 오류 코드로 옮긴다.

    Args:
        code: 온비드 `resultCode` 또는 게이트웨이 `returnReasonCode`.

    Returns:
        MCP 오류 코드. 정상(`00`)이면 None.
    """
    if code is None:
        return FALLBACK_CODE
    if code in RESULT_CODE_MAP:
        return RESULT_CODE_MAP[code]
    return FALLBACK_CODE


def _scrub(message: str) -> str:
    """메시지에서 접속 정보·경로를 지운다 (N4.1)."""
    return _SECRETISH.sub("[생략]", message)


def to_tool_error(exc: BaseException) -> ToolError:
    """예외를 툴 오류로 옮긴다.

    Args:
        exc: 잡은 예외.

    Returns:
        LLM 이 어떻게 행동할지 정해진 오류.
    """
    if isinstance(exc, OnbidQuotaExceededError | KakaoQuotaExceededError):
        return ToolError("quota_exceeded",
                         "외부 API 일일 한도를 소진했습니다. 오늘은 재시도하지 마세요.")

    if isinstance(exc, OnbidAuthError | KakaoAuthError):
        # 사용자가 조건을 바꿔서 풀 수 없다. 운영자에게 알려야 한다.
        logger.error("외부 API 키 문제 — 운영자 확인 필요: %s", _scrub(str(exc)))
        return ToolError("upstream_error",
                         "외부 API 인증에 실패했습니다. 서비스키 점검이 필요합니다(운영자 확인).")

    if isinstance(exc, OnbidApiError):
        code = map_result_code(exc.result_code)
        if code == "no_result":
            return ToolError("no_result", "조건에 맞는 물건이 없습니다.")
        if code == "invalid_param":
            return ToolError(
                "invalid_param",
                f"요청 조건이 올바르지 않습니다: {_scrub(exc.result_msg)}")
        return ToolError(FALLBACK_CODE, f"외부 API 오류: {_scrub(exc.result_msg)}")

    if isinstance(exc, OnbidError | KakaoError):
        return ToolError(FALLBACK_CODE, f"외부 API 통신에 실패했습니다: {_scrub(str(exc))}")

    if isinstance(exc, ValueError):
        # 정렬·상태 오타 같은 요청 잘못. 장애로 주면 LLM 이 기다린다.
        return ToolError("invalid_param", _scrub(str(exc)))

    logger.exception("예상치 못한 오류")
    return ToolError(FALLBACK_CODE, GENERIC_MESSAGE)
