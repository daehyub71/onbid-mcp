"""온비드 응답 파서 — 전송 계층의 모양과 원시 타입만 다룬다.

이 모듈이 흡수하는 온비드의 특이사항:

1. **0건 응답에는 ``body`` 가 통째로 없다.** ``{"result": {"resultCode": "03"}}`` 만 온다.
   ``payload["body"]["items"]`` 로 접근하면 ``KeyError`` 가 난다.
2. **결측 표현이 세 가지다.** ``""``, ``" "``, ``"-"`` 를 모두 결측으로 통일한다.
3. **숫자가 문자열로 온다.** 최저입찰가는 ``VARCHAR`` 이고 ``"비공개"`` 가 섞일 수 있다.
4. **배열이 단건일 때 dict 로 올 수 있다** (활용가이드 §4의 XML/JSON 차이 경고).
   JSON 모드 실측에서는 항상 배열이었으나, 중첩 배열까지 확인하지 못해 방어를 유지한다.

주소 선택·상태 파생·``2999`` sentinel 같은 **도메인 규칙은 여기 두지 않는다** —
`core.normalizer` 의 몫이다.
"""

from dataclasses import dataclass
from typing import Any, Final

BLANK_SENTINELS: Final = ("", "-")
"""공백을 제거한 뒤 결측으로 볼 문자열."""


def _rows(value: Any) -> list[dict[str, Any]]:
    """배열 또는 단건 dict 를 dict 리스트로 정규화한다."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def items_of(payload: Any) -> list[dict[str, Any]]:
    """응답에서 항목 목록을 꺼낸다.

    Args:
        payload: 파싱된 전체 응답.

    Returns:
        항목 dict 리스트. ``body`` 나 ``items`` 가 없으면 빈 리스트.
    """
    if not isinstance(payload, dict):
        return []
    body = payload.get("body")
    if not isinstance(body, dict):
        return []
    items = body.get("items")
    if isinstance(items, list):
        return _rows(items)
    if not isinstance(items, dict):
        return []
    return _rows(items.get("item"))


def sub_list(item: Any, key: str) -> list[dict[str, Any]]:
    """항목 안의 중첩 배열을 꺼낸다 (``prcnBidClgList`` 등).

    Args:
        item: 항목 dict.
        key: 중첩 배열 필드명.

    Returns:
        dict 리스트. 없거나 모양이 다르면 빈 리스트.
    """
    if not isinstance(item, dict):
        return []
    return _rows(item.get(key))


@dataclass(frozen=True, slots=True)
class PageInfo:
    """페이지 정보.

    Attributes:
        total_count: 전체 건수. 0건 응답에서는 0.
        page_no: 현재 페이지. 응답에 없으면 ``None``.
        num_of_rows: 페이지 크기. 응답에 없으면 ``None``.
    """

    total_count: int
    page_no: int | None
    num_of_rows: int | None

    @property
    def has_more(self) -> bool:
        """다음 페이지가 있는지 여부."""
        if not self.total_count or self.page_no is None or not self.num_of_rows:
            return False
        return self.page_no * self.num_of_rows < self.total_count


def page_info(payload: Any) -> PageInfo:
    """응답에서 페이지 정보를 꺼낸다.

    0건 응답에는 ``body`` 가 없으므로 ``total_count=0`` 으로 해석해 순회를 끝낼 수 있게 한다.
    """
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        return PageInfo(total_count=0, page_no=None, num_of_rows=None)
    return PageInfo(
        total_count=as_int(body.get("totalCount")) or 0,
        page_no=as_int(body.get("pageNo")),
        num_of_rows=as_int(body.get("numOfRows")),
    )


def as_str(
    value: Any,
    *,
    blank_sentinels: tuple[str, ...] = BLANK_SENTINELS,
) -> str | None:
    """문자열로 변환하고 결측 표현을 ``None`` 으로 통일한다.

    Args:
        value: 원본 값.
        blank_sentinels: 결측으로 볼 문자열. ``"-"`` 가 의미를 갖는 필드에서는 빈 튜플을 넘긴다.

    Returns:
        앞뒤 공백을 제거한 문자열. 결측이면 ``None``.
    """
    if value is None or isinstance(value, (list, dict, bool)):
        return None
    text = str(value).strip()
    return None if text in blank_sentinels else text


def as_int(value: Any) -> int | None:
    """정수로 변환한다. 변환할 수 없으면 ``None``.

    천 단위 쉼표를 허용한다. 소수는 **거부**한다 — 정수 필드에 소수가 오면 데이터 문제이지
    반올림 대상이 아니다. 값을 조용히 바꾸면 금액이 틀어진다.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = as_str(value)
    if text is None:
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def as_float(value: Any) -> float | None:
    """실수로 변환한다. 변환할 수 없으면 ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = as_str(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def as_bool_yn(value: Any) -> bool | None:
    """온비드의 ``"Y"``/``"N"`` 을 불리언으로 바꾼다.

    그 밖의 값은 ``None`` 이다. ``"1"`` 이나 ``"T"`` 를 참으로 넘겨짚지 않는다 —
    온비드가 쓰지 않는 표현이므로 추측하면 오히려 오류를 숨긴다.
    """
    if isinstance(value, bool):
        return None
    text = as_str(value)
    if text is None:
        return None
    upper = text.upper()
    if upper == "Y":
        return True
    if upper == "N":
        return False
    return None
