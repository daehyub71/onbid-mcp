"""주소 꼬리표 제거 (F2.2).

**측정하고 나서 설계했다.** 카카오 지오코딩에 꼬리표 유형별로 실호출해 본 결과
(기준 주소 ``서울특별시 강남구 도곡동 467-6``):

===========================  ======  =====================================
꼬리표                        결과    비고
===========================  ======  =====================================
``외 2필지`` / ``외 2필``      **0건**  유일하게 깨뜨린다
``외 3개호``                   1건    흡수
``대림아크로빌``                1건    건물명 흡수
``제17층 제1714호``            1건    층·호 흡수
``제지하2층 제101호``           1건    지하층 흡수
``101동 1503호``               1건    동·호 흡수
``(건물 및 토지)``              1건    괄호 부기 흡수
``, 제17층 (도곡동, 건물명)``    1건    도로명 상세 흡수
===========================  ======  =====================================

그래서 `strip_address_trailers` 는 **깨뜨리는 것만** 걷어낸다. 과하게 자르면
같은 지번이 여러 곳에 있을 때 엉뚱한 좌표로 이어지므로, 흡수되는 정보는 남겨
카카오가 더 정확히 찍도록 둔다.

`strip_detail_suffix` 는 그보다 공격적인 절단으로, **지오코딩이 실패했을 때의
폴백**에서만 쓴다 (SPEC F3.1).
"""

import re
from typing import Any, Final

from core.onbid.parser import as_str

BREAKING_TRAILERS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"\s*외\s*\d+\s*필(지)?\b.*$"),
        "여러 필지 표기. 카카오가 0건을 반환한다 (실측)",
    ),
)
"""지오코딩을 실제로 깨뜨리는 꼬리표와 그 근거.

여기 없는 꼬리표는 **일부러 남긴다** — 카카오가 흡수하며, 건물명이 있으면 오히려
정확도가 올라간다.
"""

#: 상세주소로 보이는 부분. 폴백 절단에만 쓴다.
_DETAIL_SUFFIXES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\s*,.*$"),                        # 도로명주소의 쉼표 이후 상세
    re.compile(r"\s*\(.*$"),                       # 괄호 부기
    re.compile(r"\s*제?\s*지하\s*\d+\s*층.*$"),      # 제지하2층 …
    re.compile(r"\s*제\s*\d+\s*층.*$"),             # 제17층 …
    re.compile(r"\s*제?\s*[\dA-Za-z]+\s*호\b.*$"),  # 제1714호 / 101호
    re.compile(r"\s*제?\s*\d+\s*동\b.*$"),          # 제101동 / 101동
)


#: 주소로서 의미가 남아 있는지 판정한다. 한글이나 숫자가 하나도 없으면 주소가 아니다.
_MEANINGFUL: Final = re.compile(r"[가-힣0-9]")


def _apply(
    text: str,
    patterns: tuple[re.Pattern[str], ...] | tuple[tuple[re.Pattern[str], str], ...],
) -> str:
    """패턴을 차례로 적용하되 **의미가 남지 않는 절단은 되돌린다.**

    빈 문자열이나 구두점만 남은 결과로는 지오코딩할 수 없으므로,
    그렇게 될 바에는 직전 상태를 유지한다.
    """
    result = text
    for entry in patterns:
        pattern = entry[0] if isinstance(entry, tuple) else entry
        candidate = pattern.sub("", result).strip()
        if candidate and _MEANINGFUL.search(candidate):
            result = candidate
    return result


def strip_address_trailers(address: Any) -> str | None:
    """지오코딩을 깨뜨리는 꼬리표만 제거한다.

    Args:
        address: 주소 문자열.

    Returns:
        정제된 주소. 입력이 비었으면 ``None``.

        **흡수 가능한 꼬리표는 남긴다** — 건물명 같은 정보가 오히려 정확도를 높인다.
    """
    text = as_str(address)
    if text is None:
        return None
    return _apply(text, BREAKING_TRAILERS)


def strip_detail_suffix(address: Any) -> str | None:
    """상세주소(층·호·동·괄호 부기·쉼표 이후)를 잘라낸다.

    `strip_address_trailers` 보다 공격적이라 **지오코딩 실패 시 폴백**에서만 쓴다.
    평상시에 쓰면 건물명이 사라져 정확도가 떨어진다.

    Args:
        address: 주소 문자열.

    Returns:
        상세주소를 뗀 주소. 입력이 비었으면 ``None``.
    """
    text = as_str(address)
    if text is None:
        return None
    return _apply(text, _DETAIL_SUFFIXES)
