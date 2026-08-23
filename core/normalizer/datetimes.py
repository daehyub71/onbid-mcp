"""일시 정규화 (SPEC §7.1).

온비드 응답에는 **타임존 표기가 없다.** KST로 간주하지 않으면 저장 시점에 9시간이
어긋나 마감일 필터가 하루씩 밀린다.

형식이 필드마다 다르다 (실측 6,910건):

============================  ==================  ==========
필드                          형식                건수
============================  ==================  ==========
``cltrBidBgngDt`` / ``EndDt``  ``yyyyMMddHHmm``    전량
``cltrOpbdDt`` (회차 이력)     ``yyyyMMddHHmm``    전량
``mdfcnDt``                    ``yyyyMMddHHmmss``  전량
``dtbtRqrEdtmCont``            ``yyyy/MM/dd``      4,548
============================  ==================  ==========

**입찰일시에 ``2999...`` sentinel 이 있다** (18건, 0.26%). 일정 미정을 뜻하며,
그대로 저장하면 마감일 정렬·필터가 오염된다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from core.onbid.parser import as_str

KST: Final = timezone(timedelta(hours=9))
"""온비드 일시의 기준 시간대. 응답에 표기가 없으므로 여기서 부여한다."""

TBD_YEAR: Final = 2900
"""이 연도 이상은 '일정 미정' 으로 본다.

실측에서 관측된 sentinel 은 ``2999`` 뿐이지만, ``9999`` 같은 다른 표기가 나와도
잘못된 날짜로 저장되지 않도록 임계값으로 둔다.
"""

_FORMATS: Final = (
    ("%Y%m%d%H%M%S", 14),
    ("%Y%m%d%H%M", 12),
    ("%Y%m%d", 8),
    ("%Y/%m/%d", 10),
)


@dataclass(frozen=True, slots=True)
class ParsedDateTime:
    """일시 한 건.

    Attributes:
        value: KST 기준 aware datetime. 파싱하지 못했거나 미정이면 ``None``.
        is_tbd: 일정 미정 sentinel 이었는지 여부.

            ``value`` 가 ``None`` 인 이유가 **"값이 이상해서"** 인지
            **"아직 정해지지 않아서"** 인지 가른다.
    """

    value: datetime | None
    is_tbd: bool


def parse_datetime(raw: Any) -> ParsedDateTime:
    """온비드 일시 문자열을 KST aware datetime 으로 바꾼다.

    Args:
        raw: 온비드 응답의 일시 필드.

    Returns:
        파싱 결과. 형식이 맞지 않거나 존재하지 않는 날짜면 ``value`` 가 ``None`` 이다.
    """
    text = as_str(raw)
    if text is None:
        return ParsedDateTime(value=None, is_tbd=False)

    for fmt, length in _FORMATS:
        if len(text) != length:
            continue
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            return ParsedDateTime(value=None, is_tbd=False)
        if naive.year >= TBD_YEAR:
            return ParsedDateTime(value=None, is_tbd=True)
        return ParsedDateTime(value=naive.replace(tzinfo=KST), is_tbd=False)

    return ParsedDateTime(value=None, is_tbd=False)


def to_iso(value: datetime | None) -> str | None:
    """MCP 응답용 ISO8601 문자열로 직렬화한다 (SPEC §7.1).

    다른 시간대의 값이 들어와도 KST 로 환산해 ``+09:00`` 오프셋을 붙인다.

    Args:
        value: aware datetime.

    Returns:
        ``2025-08-18T16:00:00+09:00`` 형태. 입력이 ``None`` 이면 ``None``.
    """
    if value is None:
        return None
    return value.astimezone(KST).isoformat()
