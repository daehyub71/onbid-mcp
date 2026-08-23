"""지오코딩에 넣을 주소 선택 (F2.1·F2.6).

온비드는 **좌표를 주지 않는다.** 지도에 찍으려면 주소를 좌표로 바꿔야 하고, 그러려면
어떤 필드를 지오코딩에 넣을지 정해야 한다. 잘못 고르면 지오코딩이 통째로 실패한다 —
실측상 카카오는 ``"... 123-4 외 2필지"`` 같은 꼬리표 주소에 **0건**을 반환한다.

물건목록에서 쓸 수 있는 주소 소스는 셋뿐이며, 정확한 순서로 시도한다.

===================================  ======  =========================================
소스                                 커버    성격
===================================  ======  =========================================
``ltnoPnu`` 지번PNU 19자리            76%    구조화된 코드. 파싱 실패가 없다
``onbidCltrNm`` 물건명               100%    전체 주소를 담지만 건물명·층·호가 붙는다
``lctnSdnm``+``Sggnm``+``EmdNm``     100%    읍면동까지만. 동 중심 근사
===================================  ======  =========================================

``cltrRadr``(도로명 전체)·``zadrNm``(지번 전체)은 **물건상세 응답에만 있다.** 물건상세는
물건당 1회 호출이라 전량에 쓸 수 없으므로 여기서는 쓰지 않는다.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from core.normalizer.pnu import parse_pnu
from core.onbid.parser import as_str

#: 물건명에서 지번을 뽑는다. `245-42`, `산181-11`, `1` 을 잡되 뒤따르는 꼬리표는 버린다.
_LOT_RE: Final = re.compile(r"(산\s*)?(\d+)(?:\s*-\s*(\d+))?")

#: 지번 뒤에 붙어 지오코딩을 방해하는 꼬리표. 실측상 카카오가 0건을 반환하게 만든다.
_TRAILER_RE: Final = re.compile(
    r"\s*(외\s*\d+\s*(필지|필|개호|호)|제?\s*지하\s*\d+\s*층|제\s*\d+\s*층|제?\s*[\dA-Za-z]+\s*호).*"
)


class AddressSource(Enum):
    """주소를 어디서 얻었는지. 지오코딩 결과의 신뢰도 판정에 쓴다."""

    PNU = "pnu"
    ITEM_NAME = "item_name"
    DISTRICT = "district"


@dataclass(frozen=True, slots=True)
class SelectedAddress:
    """지오코딩에 넣을 주소.

    Attributes:
        query: 지오코딩 질의 문자열. 시도·시군구를 앞에 붙여 동명 중복을 가른다.
        source: 이 주소를 만든 소스.
        lot: 지번 부분. 읍면동까지만 있는 경우 ``None``.
    """

    query: str
    source: AddressSource
    lot: str | None

    @property
    def is_exact(self) -> bool:
        """지번까지 특정됐는지 여부. 거짓이면 동 중심 근사가 된다."""
        return self.source is not AddressSource.DISTRICT


def jibun_from_pnu(pnu: Any, district: Any) -> str | None:
    """지번PNU에서 ``읍면동 본번-부번`` 을 만든다.

    분해는 `core.normalizer.pnu` 가 맡는다. 여기서는 주소 문자열로 조립만 한다.

    Args:
        pnu: 지번PNU 19자리.
        district: 읍면동명.

    Returns:
        지번주소. PNU가 유효하지 않거나 읍면동명이 없으면 ``None``.
    """
    parsed = parse_pnu(pnu)
    return None if parsed is None else parsed.jibun(district)


def _lot_from_item_name(item_name: Any, district: Any) -> str | None:
    """물건명에서 지번을 뽑는다.

    물건명은 ``서울특별시 마포구 연남동 245-42 삼정도나빌 제2층 제204호`` 형태다.
    읍면동 뒤부터 읽되 건물명·층·호는 버린다.
    """
    text = as_str(item_name)
    name = as_str(district)
    if text is None or name is None or name not in text:
        return None

    tail = _TRAILER_RE.sub("", text.split(name, 1)[1]).strip()
    match = _LOT_RE.match(tail)
    if match is None:
        return None

    prefix = "산" if match.group(1) else ""
    sub = f"-{int(match.group(3))}" if match.group(3) else ""
    return f"{name} {prefix}{int(match.group(2))}{sub}"


def select_address(row: Any) -> SelectedAddress | None:
    """지오코딩에 넣을 주소를 고른다.

    Args:
        row: 온비드 물건목록 응답 행.

    Returns:
        선택된 주소. 읍면동조차 없으면 ``None`` (실측 결측률 0%라 드물다).
    """
    if not isinstance(row, Mapping):
        return None

    sido = as_str(row.get("lctnSdnm"))
    sigungu = as_str(row.get("lctnSggnm"))
    emd = as_str(row.get("lctnEmdNm"))
    if emd is None:
        return None

    region = " ".join(part for part in (sido, sigungu) if part)

    for source, lot in (
        (AddressSource.PNU, jibun_from_pnu(row.get("ltnoPnu"), emd)),
        (AddressSource.ITEM_NAME, _lot_from_item_name(row.get("onbidCltrNm"), emd)),
    ):
        if lot is not None:
            return SelectedAddress(
                query=f"{region} {lot}".strip(), source=source, lot=lot
            )

    return SelectedAddress(
        query=f"{region} {emd}".strip(), source=AddressSource.DISTRICT, lot=None
    )
