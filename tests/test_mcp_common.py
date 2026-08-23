"""MCP 공통 응답 래퍼 테스트 (F6.3·F6.4·§8.6·§8.7).

**모든 툴 응답에 `meta` 와 `query_echo` 가 들어간다.** 툴마다 손으로 붙이면 언젠가 빠지고,
빠진 그 응답에서 LLM 이 "전부 보여드렸습니다" 라고 단정한다.

`no_result` 를 **빈 배열로 주지 않는 것**이 이 모듈의 존재 이유 중 하나다 (§8.7). 빈 배열은
"조건에 맞는 물건이 없다" 와 "조회에 실패했다" 를 구분하지 못한다.
"""

from datetime import UTC, datetime

import pytest

from onbid_mcp.common import (
    ERROR_CODES,
    NOTICE,
    ToolError,
    error_response,
    ok_response,
)

# ── meta 강제 (F6.3·§8.6) ──────────────────────────────────────────────


def test_response_always_carries_meta() -> None:
    body = ok_response({"items": []}, query_echo={}, count=0)

    assert set(body["meta"]) >= {
        "source", "synced_at", "is_realtime", "count", "truncated", "notice"
    }


def test_batch_origin_is_declared() -> None:
    """실시간으로 오인하면 '지금 이 값이다' 라고 단정한다 (§8.6)."""
    body = ok_response({"items": []}, query_echo={}, count=0)

    assert body["meta"]["is_realtime"] is False


def test_notice_is_present() -> None:
    """판단 주체를 사용자에게 두는 문구다 (§1.5①·F6.9)."""
    body = ok_response({"items": []}, query_echo={}, count=0)

    assert body["meta"]["notice"] == NOTICE
    assert "온비드 원문" in NOTICE


def test_truncated_is_explicit() -> None:
    """'전부 보여줬다' 는 오해가 여기서 시작된다."""
    body = ok_response({"items": [1, 2]}, query_echo={}, count=2, truncated=True)

    assert body["meta"]["truncated"] is True


def test_synced_at_is_carried() -> None:
    stamp = datetime(2026, 8, 23, 4, 0, tzinfo=UTC)
    body = ok_response({}, query_echo={}, count=0, synced_at=stamp)

    assert body["meta"]["synced_at"] == stamp.isoformat()


def test_extra_meta_merges() -> None:
    """caveat 같은 툴별 항목을 얹을 수 있어야 한다 (§8.3)."""
    body = ok_response({}, query_echo={}, count=0, meta_extra={"caveat": "주의"})

    assert body["meta"]["caveat"] == "주의"


def test_extra_meta_cannot_hide_the_notice() -> None:
    """툴이 실수로든 고의로든 고지를 덮어쓰지 못하게 한다."""
    body = ok_response({}, query_echo={}, count=0,
                       meta_extra={"notice": "", "is_realtime": True})

    assert body["meta"]["notice"] == NOTICE
    assert body["meta"]["is_realtime"] is False


# ── query_echo 강제 (F6.4) ─────────────────────────────────────────────


def test_query_echo_is_always_present() -> None:
    """LLM 이 '강남구로 검색했습니다' 라고 사실과 다르게 말하는 것을 막는다 (§8.7)."""
    body = ok_response({"items": []}, query_echo={"sgg_nm": "강남구"}, count=0)

    assert body["query_echo"] == {"sgg_nm": "강남구"}


def test_empty_query_echo_is_still_a_key() -> None:
    """키 자체가 없으면 '조건을 안 걸었다' 와 '되돌려주지 않았다' 를 구분할 수 없다."""
    body = ok_response({"items": []}, query_echo={}, count=0)

    assert "query_echo" in body


# ── 오류 규약 (§8.7) ───────────────────────────────────────────────────


def test_error_codes_match_the_spec() -> None:
    assert set(ERROR_CODES) == {
        "no_result", "invalid_param", "not_found", "upstream_error", "quota_exceeded"
    }


def test_error_response_is_not_an_empty_list() -> None:
    """**빈 배열로 주지 않는다** — '없다' 와 '실패했다' 를 구분하지 못한다 (§8.7)."""
    body = error_response(ToolError("no_result", "조건에 맞는 물건이 없다"), query_echo={})

    assert "items" not in body
    assert body["error"]["code"] == "no_result"


def test_error_carries_meta_and_echo() -> None:
    """오류 응답에서도 조건을 되돌려줘야 LLM 이 무엇을 완화할지 안다."""
    body = error_response(ToolError("no_result", "없음"),
                          query_echo={"sgg_nm": "강남구"})

    assert body["query_echo"]["sgg_nm"] == "강남구"
    assert body["meta"]["notice"]


def test_error_can_suggest_candidates() -> None:
    """`invalid_param` 은 후보를 줘야 LLM 이 재시도할 수 있다 (§8.7)."""
    body = error_response(
        ToolError("invalid_param", "용도를 찾을 수 없다", candidates=["아파트", "연립주택"]),
        query_echo={})

    assert body["error"]["candidates"] == ["아파트", "연립주택"]


def test_unknown_error_code_is_rejected() -> None:
    """임의 코드를 만들면 LLM 이 어떻게 행동할지 정해지지 않는다."""
    with pytest.raises(ValueError, match="오류 코드"):
        ToolError("weird_failure", "무언가 잘못됨")


def test_error_message_is_human_readable() -> None:
    body = error_response(ToolError("quota_exceeded", "외부 API 일일 한도를 소진했다"),
                          query_echo={})

    assert body["error"]["message"]
    assert body["error"]["code"] == "quota_exceeded"
