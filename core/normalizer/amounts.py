"""금액 정규화 (F4.7).

온비드의 최저입찰가(``lowstBidPrcIndctCont``)는 **VARCHAR** 이며,
활용가이드가 *"최저입찰가격이 표시되거나 비공개로 표시"* 라고 밝힌 대로
``"비공개"`` 문자열이 섞여 온다. 실측상 6,910건 중 **1건**으로 드물지만,
정수 캐스팅을 그대로 하면 그 1건에서 배치가 죽는다.

값이 **없는 것**과 **가려진 것**을 구분해서 저장한다 — 전자는 데이터가 없는 것이고
후자는 온비드가 의도적으로 감춘 것이라 의미가 다르다.
"""

from dataclasses import dataclass
from typing import Any

from core.onbid.parser import as_str


@dataclass(frozen=True, slots=True)
class Amount:
    """금액 한 건.

    Attributes:
        value: 원 단위 정수. 파싱하지 못했으면 ``None``.
        text: 파싱하지 못한 원문. 값이 아예 없었으면 ``None``.
    """

    value: int | None
    text: str | None

    @property
    def is_disclosed(self) -> bool:
        """금액을 알 수 있는지 여부."""
        return self.value is not None


def parse_amount(raw: Any) -> Amount:
    """금액을 원 단위 정수로 바꾼다.

    천 단위 쉼표를 허용한다. **음수와 소수는 거부한다** — 금액이 음수일 수 없고,
    원 단위 필드에 소수가 오면 데이터 문제이지 반올림 대상이 아니다.
    조용히 받아들이면 최저가율이 음수가 되거나 금액이 어긋난다.

    Args:
        raw: 온비드 응답의 금액 필드.

    Returns:
        파싱 결과. 실패하면 ``value`` 는 ``None`` 이고 ``text`` 에 원문이 남는다.
    """
    if isinstance(raw, bool):
        return Amount(value=None, text=None)
    if isinstance(raw, int):
        return Amount(value=raw, text=None) if raw >= 0 else Amount(value=None, text=str(raw))

    text = as_str(raw)
    if text is None:
        return Amount(value=None, text=None)

    digits = text.replace(",", "")
    if digits.isdigit():
        return Amount(value=int(digits), text=None)
    return Amount(value=None, text=text)
