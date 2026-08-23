"""입찰 상태 파생 (SPEC §7.1).

온비드가 상태를 주는 방식이 응답마다 다르다.

- **물건목록**: ``pbctStatCd`` 코드 + ``pbctStatNm`` 이름
- **회차 이력**(``prcnBidClgList``): **이름만** — 코드 필드가 아예 없다

그래서 코드 경로와 이름 경로를 모두 지원한다. 코드가 있으면 코드를 쓴다 —
표기(이름)는 바뀔 수 있지만 코드는 안정적이다. 실제로 ``bidDivCd=0001`` 의 이름이
가이드("인터넷")와 응답("전자입찰")에서 다른 전례가 있다.

원본 코드는 `pbct_stat_cd` 에 그대로 보존하므로, 아래 묶음 규칙이 바뀌어도 재계산할 수 있다.
"""

from collections.abc import Mapping
from enum import Enum
from typing import Any, Final

from core.onbid.parser import as_str


class AuctionStatus(Enum):
    """DB ``status`` 컬럼에 들어가는 파생 상태.

    값 문자열이 곧 저장값이다 (SPEC §7.1).
    """

    OPEN = "진행"
    CLOSED = "마감"
    WON = "낙찰"
    FAILED = "유찰"
    CANCELLED = "취소"
    PRESUMED_ENDED = "종료추정"
    """목록에서 사라진 물건. **응답에 없는 상태**이며 적재 계층이 부여한다 (F4.2)."""

    @property
    def is_open(self) -> bool:
        """아직 취득할 수 있는 상태인지 여부."""
        return self is AuctionStatus.OPEN


_BY_CODE: Final[dict[str, AuctionStatus]] = {
    "0001": AuctionStatus.OPEN,        # 입찰준비중
    "0002": AuctionStatus.OPEN,        # 입찰진행중
    "0009": AuctionStatus.OPEN,        # 수의계약가능
    "0003": AuctionStatus.CLOSED,      # 입찰마감
    "0006": AuctionStatus.CLOSED,      # 개찰중
    "0010": AuctionStatus.WON,         # 낙찰
    "0011": AuctionStatus.FAILED,      # 유찰
    "0012": AuctionStatus.CANCELLED,   # 취소
}
"""``pbctStatCd`` → 파생 상태.

입찰준비중·입찰진행중·수의계약가능을 모두 `진행` 으로 묶는다 — 셋 다 아직 취득할 수 있다.
수의계약과 입찰의 구분은 상태가 아니라 `pvct_trgt_yn` 컬럼이 담당한다.
"""

_BY_NAME: Final[dict[str, AuctionStatus]] = {
    "입찰준비중": AuctionStatus.OPEN,
    "입찰진행중": AuctionStatus.OPEN,
    "수의계약가능": AuctionStatus.OPEN,
    "입찰마감": AuctionStatus.CLOSED,
    "개찰중": AuctionStatus.CLOSED,
    "낙찰": AuctionStatus.WON,
    "유찰": AuctionStatus.FAILED,
    "취소": AuctionStatus.CANCELLED,
}
"""``pbctStatNm`` → 파생 상태. 코드가 없는 회차 이력에서 쓴다."""


def status_from_code(code: Any) -> AuctionStatus | None:
    """``pbctStatCd`` 를 파생 상태로 바꾼다.

    Args:
        code: 입찰결과구분코드. 숫자로 와도 4자리로 맞춰 해석한다.

    Returns:
        파생 상태. 모르는 코드면 ``None`` — 임의의 상태로 넘겨짚지 않는다.
    """
    text = as_str(code)
    if text is None:
        return None
    return _BY_CODE.get(text.zfill(4))


def status_from_name(name: Any) -> AuctionStatus | None:
    """``pbctStatNm`` 을 파생 상태로 바꾼다.

    Args:
        name: 입찰결과구분코드명.

    Returns:
        파생 상태. 모르는 이름이면 ``None``.
    """
    text = as_str(name)
    if text is None:
        return None
    return _BY_NAME.get(text)


def status_of(row: Any) -> AuctionStatus | None:
    """응답 행에서 파생 상태를 얻는다.

    코드를 먼저 보고, 없거나 모르는 값이면 이름으로 폴백한다.

    Args:
        row: 물건목록 행 또는 회차 이력 행.

    Returns:
        파생 상태. 둘 다 없으면 ``None``.
    """
    if not isinstance(row, Mapping):
        return None
    return status_from_code(row.get("pbctStatCd")) or status_from_name(row.get("pbctStatNm"))
