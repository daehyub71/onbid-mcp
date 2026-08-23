"""MCP 공통 응답 래퍼 (F6.3·F6.4·§8.6·§8.7).

**모든 툴 응답에 `meta` 와 `query_echo` 가 들어간다.** 툴마다 손으로 붙이면 언젠가 빠지고,
빠진 그 응답에서 LLM 이 "전부 보여드렸습니다" 라고 단정한다. 그래서 응답을 만드는 통로를
이 모듈 하나로 좁힌다.

두 가지를 구조로 못박는다.

- **고지는 덮어쓸 수 없다.** 툴이 `meta_extra` 로 `notice` 나 `is_realtime` 을 바꾸려 해도
  무시한다 — 판단 주체를 사용자에게 두는 문구이고(§1.5①), 배치 수집분임을 숨기면 LLM 이
  "지금 이 값" 이라고 말한다.
- **`no_result` 를 빈 배열로 주지 않는다** (§8.7). 빈 배열은 "조건에 맞는 물건이 없다" 와
  "조회에 실패했다" 를 구분하지 못해 LLM 이 잘못된 결론을 낸다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

SOURCE: Final = "온비드(한국자산관리공사) / 공공데이터포털"

NOTICE: Final = "정보 제공 목적입니다. 입찰 전 온비드 원문을 확인하세요."
"""판단 주체를 사용자에게 두는 고지 (§1.5①·F6.9)."""

#: 툴이 쓸 수 있는 오류 코드. LLM 이 취할 행동이 코드마다 정해져 있으므로 임의로 늘리지 않는다.
ERROR_CODES: Final = {
    "no_result": "조건에 맞는 물건이 없다 — 조건 완화를 제안한다",
    "invalid_param": "파라미터 오류·명칭 매칭 실패 — `candidates` 로 재시도한다",
    "not_found": "물건번호가 존재하지 않는다 — 검색으로 유도한다",
    "upstream_error": "외부 API 장애 — 재시도 또는 캐시 데이터 안내",
    "quota_exceeded": "외부 API 쿼터 소진 — 재시도를 중단한다",
}

#: 툴이 바꿀 수 없는 `meta` 항목. 고지를 지우거나 실시간인 척하지 못하게 한다.
PROTECTED_META: Final = frozenset({"source", "notice", "is_realtime"})


@dataclass(frozen=True, slots=True)
class ToolError(Exception):
    """툴이 돌려줄 오류.

    Attributes:
        code: `ERROR_CODES` 의 키.
        message: 사람이 읽는 설명.
        candidates: `invalid_param` 일 때 재시도에 쓸 후보.
    """

    code: str
    message: str
    candidates: Sequence[str] | None = None

    def __post_init__(self) -> None:
        """코드가 규약 안에 있는지 확인한다.

        Raises:
            ValueError: 알 수 없는 코드일 때. 임의 코드를 만들면 LLM 이 어떻게 행동할지
                정해지지 않는다.
        """
        if self.code not in ERROR_CODES:
            raise ValueError(
                f"알 수 없는 오류 코드: {self.code!r} (가능: {sorted(ERROR_CODES)})"
            )


def build_meta(
    *,
    count: int,
    truncated: bool = False,
    synced_at: datetime | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """공통 `meta` 블록을 만든다 (§8.6).

    Args:
        count: 이번 응답에 담은 건수.
        truncated: 더 남았는지 여부. "전부 보여줬다" 는 오해를 막는다.
        synced_at: 배치 기준 시각. 신선도를 알린다 (N2.3).
        extra: 툴별 추가 항목 (`caveat` 등). **보호 항목은 덮어쓰지 못한다.**

    Returns:
        `meta` 매핑.
    """
    safe_extra = {k: v for k, v in (extra or {}).items() if k not in PROTECTED_META}
    return {
        "source": SOURCE,
        "synced_at": synced_at.isoformat() if synced_at else None,
        "is_realtime": False,
        "count": count,
        "truncated": truncated,
        "notice": NOTICE,
        **safe_extra,
    }


def ok_response(
    payload: Mapping[str, Any],
    *,
    query_echo: Mapping[str, Any],
    count: int,
    truncated: bool = False,
    synced_at: datetime | None = None,
    meta_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """정상 응답을 만든다.

    Args:
        payload: 툴 고유 본문 (`items`·`buckets` 등).
        query_echo: **실제 적용된** 조건 (F6.4).
        count: 담은 건수.
        truncated: 더 남았는지 여부.
        synced_at: 배치 기준 시각.
        meta_extra: 툴별 `meta` 추가 항목.

    Returns:
        본문 + `query_echo` + `meta`.
    """
    return {
        **payload,
        "query_echo": dict(query_echo),
        "meta": build_meta(count=count, truncated=truncated,
                           synced_at=synced_at, extra=meta_extra),
    }


def error_response(
    error: ToolError,
    *,
    query_echo: Mapping[str, Any],
    synced_at: datetime | None = None,
) -> dict[str, Any]:
    """오류 응답을 만든다 (§8.7).

    **본문 배열을 담지 않는다.** 빈 배열로 주면 "없다" 와 "실패했다" 가 구분되지 않는다.
    오류일 때도 `query_echo` 를 돌려줘야 LLM 이 무엇을 완화할지 판단할 수 있다.

    Args:
        error: 돌려줄 오류.
        query_echo: 실제 적용된 조건.
        synced_at: 배치 기준 시각.

    Returns:
        `error` + `query_echo` + `meta`.
    """
    body: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.candidates is not None:
        body["candidates"] = list(error.candidates)

    return {
        "error": body,
        "query_echo": dict(query_echo),
        "meta": build_meta(count=0, truncated=False, synced_at=synced_at),
    }
