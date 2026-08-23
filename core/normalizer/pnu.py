"""PNU(필지고유번호) 처리 (SPEC §7.1).

온비드가 물건목록에 ``ltnoPnu``(지번PNU 19자리)를 담아 준다. 계획서 v0.3이 Phase 2의
"전체의 관문"이라 했던 **주소→PNU 변환이 필요 없다**는 뜻이다.

구조::

    1141011600 0 0072 0022
    └────────┘ │ └──┘ └──┘
     법정동코드  산  본번  부번

**문자열로 다뤄야 한다.** 정수로 저장하면 선행 0이 사라져 법정동코드가 깨진다.
앞 10자리는 Phase 2의 공적장부 조회(건축물대장·토지대장)에 그대로 쓰인다.
"""

from dataclasses import dataclass
from typing import Any, Final

from core.onbid.parser import as_str

PNU_LENGTH: Final = 19
LEGAL_DONG_CODE_LENGTH: Final = 10
MOUNTAIN_FLAG: Final = "1"


@dataclass(frozen=True, slots=True)
class Pnu:
    """필지고유번호.

    Attributes:
        raw: 원본 19자리. 조립 규칙이 바뀌어도 재계산할 수 있도록 보존한다.
        legal_dong_code: 법정동코드 10자리 **문자열**.
        is_mountain: 산번지 여부.
        main_no: 본번.
        sub_no: 부번. 없으면 0.
    """

    raw: str
    legal_dong_code: str
    is_mountain: bool
    main_no: int
    sub_no: int

    def jibun(self, district: Any) -> str | None:
        """``읍면동 본번-부번`` 형태의 지번주소를 만든다.

        Args:
            district: 읍면동명. PNU는 코드만 담으므로 이름은 응답 필드에서 와야 한다.

        Returns:
            지번주소. 읍면동명이 없으면 ``None``.
        """
        name = as_str(district)
        if name is None:
            return None
        prefix = "산" if self.is_mountain else ""
        suffix = f"-{self.sub_no}" if self.sub_no else ""
        return f"{name} {prefix}{self.main_no}{suffix}"


def parse_pnu(raw: Any) -> Pnu | None:
    """PNU 문자열을 분해한다.

    Args:
        raw: 지번PNU 19자리.

    Returns:
        분해 결과. 19자리 숫자가 아니거나 본번이 0이면 ``None``.

        애매하면 만들지 않는다 — 틀린 지번으로 지오코딩하면 엉뚱한 좌표가 나오고,
        그건 좌표가 없는 것보다 나쁘다.
    """
    if isinstance(raw, bool):
        return None
    code = as_str(raw)
    if code is None or len(code) != PNU_LENGTH or not code.isdigit():
        return None

    main_no = int(code[11:15])
    if main_no == 0:
        return None

    return Pnu(
        raw=code,
        legal_dong_code=code[:LEGAL_DONG_CODE_LENGTH],
        is_mountain=code[LEGAL_DONG_CODE_LENGTH] == MOUNTAIN_FLAG,
        main_no=main_no,
        sub_no=int(code[15:19]),
    )
