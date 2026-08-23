"""툴 파라미터 해석 — 명칭↔코드 (F6.6·F6.7).

MCP 툴은 `region`·`usage`·`prpt_div` 를 **코드값과 한글 명칭 양쪽으로** 받는다.
셋의 성격이 다르다.

===========  ==========================  ==================================
파라미터      코드                        해석 방식
===========  ==========================  ==================================
`region`     **없음**                    명칭 전용. 온비드는 법정동코드를 쓰지 않는다
`usage`      3단 계층                    코드·명칭. 중분류는 하위까지 확장 (F6.12)
`prpt_div`   10종 고정                   코드·명칭. 쉼표 복수 지정
===========  ==========================  ==================================

**해석이 하나로 좁혀지지 않으면 후보를 함께 돌려준다** (F6.7). 실측상 서울 199개 읍면동 중
``신사동`` 이 강남구·은평구 양쪽에 있어 실제로 모호해진다.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Generic, TypeVar

from core.codes.address import AddressEntry
from core.codes.constants import PRPT_DIV_NAMES
from core.codes.index import DEFAULT_SUGGEST_LIMIT, UsageIndex
from core.codes.usage import UsageCode
from core.onbid.parser import as_str

#: 해석 결과가 담는 값의 타입. PEP 695 문법은 3.12+ 전용이라 쓰지 않는다 (N8.1: Python 3.11+).
T = TypeVar("T")


def _normalize(text: str) -> str:
    """비교용 정규화. 사용자가 띄어쓰기를 다르게 쓸 수 있다."""
    return text.replace(" ", "").strip()


@dataclass(frozen=True, slots=True)
class Resolution(Generic[T]):
    """파라미터 해석 결과.

    Attributes:
        term: 사용자가 입력한 원문.
        matched: 해석된 값. 비었으면 실패, 둘 이상이면 모호하다.
        candidates: 재시도용 후보. `invalid_param` 응답에 싣는다 (F6.7).
    """

    term: str | None
    matched: tuple[T, ...] = ()
    candidates: tuple[T, ...] = ()

    @property
    def is_resolved(self) -> bool:
        """하나로 좁혀졌는지 여부."""
        return len(self.matched) == 1

    @property
    def is_ambiguous(self) -> bool:
        """여러 곳에 해당해 사용자가 골라야 하는지 여부."""
        return len(self.matched) > 1

    @property
    def is_unknown(self) -> bool:
        """전혀 매칭되지 않았는지 여부."""
        return not self.matched

    def describe_candidates(self) -> str:
        """오류 메시지에 실을 후보 문자열.

        모호할 때는 **매칭된 것들**을 보여준다 — 사용자가 그 중에서 골라야 한다.
        """
        shown = self.matched if self.is_ambiguous else self.candidates
        return ", ".join(str(item) for item in shown)


@dataclass(frozen=True, slots=True)
class RegionMatch:
    """해석된 지역.

    Attributes:
        sgg_nm: 시군구명.
        emd_nm: 읍면동명. 시군구까지만 지정했으면 ``None``.
    """

    sgg_nm: str
    emd_nm: str | None

    def __str__(self) -> str:
        return self.sgg_nm if self.emd_nm is None else f"{self.sgg_nm} {self.emd_nm}"


@dataclass(frozen=True, slots=True)
class PropertyType:
    """재산유형.

    Attributes:
        code: `prptDivCd`.
        name: 재산유형명.
    """

    code: str
    name: str

    def __str__(self) -> str:
        return f"{self.name}({self.code})"


class RegionIndex:
    """지역 명칭 해석기.

    온비드는 지역 코드를 쓰지 않으므로 **명칭 전용**이다. `fetch_address_list` 가 돌려준
    "물건이 실제로 존재하는 조합"으로 만들면, 검색해도 물건이 없는 지역을 걸러 준다.

    Args:
        entries: 시도·시군구·읍면동 조합.
    """

    def __init__(self, entries: Iterable[AddressEntry]) -> None:
        self._entries = list(entries)
        self._districts: list[str] = sorted({e.sgg_nm for e in self._entries})
        self._by_emd: dict[str, list[AddressEntry]] = {}
        for entry in self._entries:
            self._by_emd.setdefault(_normalize(entry.emd_nm), []).append(entry)

    @property
    def districts(self) -> tuple[str, ...]:
        """시군구 목록."""
        return tuple(self._districts)

    def resolve(self, term: Any) -> Resolution[RegionMatch]:
        """지역 문자열을 해석한다.

        ``"강남구"`` · ``"개포동"`` · ``"강남구 개포동"`` 을 모두 받는다.

        Args:
            term: 사용자가 입력한 지역 문자열.

        Returns:
            해석 결과. 동명이 여러 자치구에 있으면 `is_ambiguous` 가 참이 된다.
        """
        text = as_str(term)
        if text is None:
            return Resolution(term=None, candidates=self._candidates(""))

        needle = _normalize(text)

        exact_district = next((d for d in self._districts if _normalize(d) == needle), None)
        if exact_district is not None:
            return Resolution(term=text, matched=(RegionMatch(exact_district, None),))

        by_emd = self._by_emd.get(needle)
        if by_emd:
            return Resolution(
                term=text,
                matched=tuple(RegionMatch(e.sgg_nm, e.emd_nm) for e in by_emd),
            )

        phrase = self._resolve_phrase(needle)
        if phrase is not None:
            return Resolution(term=text, matched=(phrase,))

        return Resolution(term=text, candidates=self._candidates(needle))

    def _resolve_phrase(self, needle: str) -> RegionMatch | None:
        """``강남구개포동`` 처럼 시군구와 읍면동이 붙은 입력을 가른다."""
        for entry in self._entries:
            if _normalize(f"{entry.sgg_nm}{entry.emd_nm}") == needle:
                return RegionMatch(entry.sgg_nm, entry.emd_nm)
        return None

    def _candidates(self, needle: str) -> tuple[RegionMatch, ...]:
        """부분 일치 후보. 검색어가 비었으면 시군구 목록을 보여준다."""
        if not needle:
            return tuple(RegionMatch(d, None) for d in self._districts)[:DEFAULT_SUGGEST_LIMIT]

        found: list[RegionMatch] = [
            RegionMatch(d, None) for d in self._districts if needle in _normalize(d)
        ]
        found.extend(
            RegionMatch(e.sgg_nm, e.emd_nm)
            for e in self._entries
            if needle in _normalize(e.emd_nm)
        )
        return tuple(found)[:DEFAULT_SUGGEST_LIMIT]


_PROPERTY_TYPES: Final[tuple[PropertyType, ...]] = tuple(
    PropertyType(code, name) for code, name in PRPT_DIV_NAMES.items()
)


def resolve_property_type(term: Any) -> Resolution[PropertyType]:
    """재산유형을 해석한다. 코드·명칭 양쪽을 받고 쉼표 복수 지정을 지원한다.

    Args:
        term: ``"압류재산"`` · ``"0007"`` · ``"압류재산,국유재산"``.

    Returns:
        해석 결과. **일부만 맞으면 실패로 본다** — 조용히 일부를 버리면 결과가 틀린다.
    """
    text = as_str(term)
    if text is None:
        return Resolution(term=None, candidates=_PROPERTY_TYPES)

    matched: list[PropertyType] = []
    for part in (p.strip() for p in text.split(",")):
        found = _match_property_type(part)
        if found is None:
            return Resolution(term=text, candidates=_suggest_property_types(text))
        if found not in matched:
            matched.append(found)

    return Resolution(term=text, matched=tuple(matched))


def _match_property_type(part: str) -> PropertyType | None:
    key = _normalize(part)
    if not key:
        return None
    if key.isdigit():
        padded = key.zfill(4)
        return next((p for p in _PROPERTY_TYPES if p.code == padded), None)
    return next((p for p in _PROPERTY_TYPES if _normalize(p.name) == key), None)


def _suggest_property_types(term: str) -> tuple[PropertyType, ...]:
    needle = _normalize(term.split(",")[0])
    found = tuple(p for p in _PROPERTY_TYPES if needle and needle in _normalize(p.name))
    return found or _PROPERTY_TYPES


def resolve_usage(
    term: Any,
    index: UsageIndex,
    *,
    expand: bool = False,
    limit: int = DEFAULT_SUGGEST_LIMIT,
) -> Resolution[UsageCode]:
    """용도를 해석한다. 해석 자체는 `UsageIndex` 가 맡는다.

    Args:
        term: 용도 코드 또는 명칭.
        index: 용도 트리 인덱스.
        expand: 중분류 지정 시 하위 소분류까지 포함할지 여부 (F6.12).
        limit: 후보 개수 상한.

    Returns:
        해석 결과.
    """
    text = as_str(term)
    matched = tuple(index.resolve(text, expand=expand))
    if matched:
        return Resolution(term=text, matched=matched)
    return Resolution(term=text, candidates=tuple(index.suggest(text, limit=limit)))

