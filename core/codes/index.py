"""용도 트리 조회 인덱스 (F1.2·F6.6·F6.7·F7.2).

`fetch_usage_tree` 가 돌려주는 것은 **평평한 노드 리스트**(실측 116개)다. MCP 툴이 쓰려면
네 가지 조회가 필요하다.

1. **이름 → 코드** — 사용자가 ``"아파트"`` 라고 입력한다 (F6.6).
2. **하위 확장** — ``"주거용건물"``(중분류)을 지정하면 그 아래 소분류까지 포함해야 한다.
   물건 데이터에는 소분류 코드가 들어 있으므로, 중분류 코드만으로 조회하면 **0건**이 나온다.
3. **후보 제안** — 매칭에 실패했을 때 LLM 이 재시도할 수 있게 한다 (F6.7).
4. **경로 표시** — ``부동산 > 주거용건물 > 아파트`` 를 응답에 실어 계층을 알린다.

리스트를 매번 순회하는 대신 한 번 만들어 두고 재사용한다.
"""

from collections.abc import Iterable, Sequence
from typing import Any, Final

from core.codes.usage import UsageCode
from core.onbid.parser import as_str

DEFAULT_SUGGEST_LIMIT: Final = 10
"""후보 목록 상한. LLM 컨텍스트를 낭비하지 않는다 (N3.2와 같은 취지)."""

PATH_SEPARATOR: Final = " > "


def _normalize(text: str) -> str:
    """명칭 비교용 정규화. 온비드 명칭에는 공백이 없다(`산업용및기타특수용건물`)."""
    return text.replace(" ", "").strip()


class UsageIndex:
    """용도 트리의 조회 구조.

    Args:
        nodes: `fetch_usage_tree` 가 돌려준 노드 목록.
    """

    def __init__(self, nodes: Iterable[UsageCode]) -> None:
        self._nodes: list[UsageCode] = list(nodes)
        self._by_id: dict[str, UsageCode] = {n.ctgr_id: n for n in self._nodes}
        self._by_name: dict[str, list[UsageCode]] = {}
        self._children: dict[str, list[UsageCode]] = {}
        for node in self._nodes:
            self._by_name.setdefault(_normalize(node.ctgr_nm), []).append(node)
            if node.up_ctgr_id is not None:
                self._children.setdefault(node.up_ctgr_id, []).append(node)

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def nodes(self) -> Sequence[UsageCode]:
        """전 노드. Resource 노출에 쓴다 (F7.2)."""
        return self._nodes

    # ── 단건 조회 ───────────────────────────────────────────────────────

    def by_id(self, ctgr_id: Any) -> UsageCode | None:
        """코드로 찾는다."""
        key = as_str(ctgr_id)
        return None if key is None else self._by_id.get(key)

    def by_name(self, name: Any) -> list[UsageCode]:
        """명칭으로 찾는다.

        같은 이름이 여러 깊이에 있을 수 있으므로 **목록**으로 돌려준다.
        """
        key = as_str(name)
        return [] if key is None else list(self._by_name.get(_normalize(key), []))

    # ── 계층 이동 ───────────────────────────────────────────────────────

    def children(self, ctgr_id: Any) -> list[UsageCode]:
        """바로 아래 자식들."""
        key = as_str(ctgr_id)
        return [] if key is None else list(self._children.get(key, []))

    def descendants(self, ctgr_id: Any) -> list[UsageCode]:
        """자기 자신을 포함한 모든 하위 노드."""
        root = self.by_id(ctgr_id)
        if root is None:
            return []
        found: list[UsageCode] = [root]
        queue = self.children(root.ctgr_id)
        while queue:
            node = queue.pop(0)
            found.append(node)
            queue.extend(self.children(node.ctgr_id))
        return found

    def ancestors(self, ctgr_id: Any) -> list[UsageCode]:
        """부모부터 루트까지 거슬러 올라간다."""
        node = self.by_id(ctgr_id)
        if node is None:
            return []
        chain: list[UsageCode] = []
        seen = {node.ctgr_id}
        parent = self.by_id(node.up_ctgr_id)
        while parent is not None and parent.ctgr_id not in seen:
            chain.append(parent)
            seen.add(parent.ctgr_id)
            parent = self.by_id(parent.up_ctgr_id)
        return chain

    def path(self, ctgr_id: Any) -> str | None:
        """``부동산 > 주거용건물 > 아파트`` 형태의 경로."""
        node = self.by_id(ctgr_id)
        if node is None:
            return None
        names = [n.ctgr_nm for n in reversed(self.ancestors(node.ctgr_id))]
        return PATH_SEPARATOR.join([*names, node.ctgr_nm])

    def at_depth(self, depth: int) -> list[UsageCode]:
        """특정 깊이의 노드들 (1 대분류 · 2 중분류 · 3 소분류)."""
        return [n for n in self._nodes if n.depth == depth]

    # ── 툴 파라미터 해석 ────────────────────────────────────────────────

    def resolve(self, term: Any, *, expand: bool = False) -> list[UsageCode]:
        """코드값이든 명칭이든 받아 노드로 해석한다 (F6.6).

        Args:
            term: 용도 코드 또는 명칭.
            expand: 참이면 하위 노드까지 포함한다.

                중분류를 지정했을 때 필수다 — 물건 데이터에는 **소분류 코드**가
                들어 있어, 확장하지 않으면 조회 결과가 0건이 된다.

        Returns:
            해석된 노드. 매칭 실패면 빈 목록 (후보는 `suggest` 로 얻는다).
        """
        key = as_str(term)
        if key is None:
            return []

        matched = [node] if (node := self.by_id(key)) else self.by_name(key)
        if not expand:
            return matched

        expanded: dict[str, UsageCode] = {}
        for found in matched:
            for node in self.descendants(found.ctgr_id):
                expanded[node.ctgr_id] = node
        return list(expanded.values())

    def suggest(self, term: Any, *, limit: int = DEFAULT_SUGGEST_LIMIT) -> list[UsageCode]:
        """부분 일치 후보를 돌려준다 (F6.7).

        매칭에 실패했을 때 ``invalid_param`` 오류에 실어 LLM 이 재시도하게 한다.
        검색어로 **시작하는** 이름을 앞에 둔다.

        검색어가 비었으면 **상위 분류**(대·중분류)를 돌려준다 — 빈 목록보다
        "무엇을 고를 수 있는지" 보여주는 편이 LLM 에 쓸모 있다.

        Args:
            term: 사용자가 입력한 문자열.
            limit: 후보 개수 상한.

        Returns:
            후보 노드 목록.
        """
        key = as_str(term)
        if key is None:
            return [n for n in self._nodes if n.depth <= 2][:limit]

        needle = _normalize(key)
        starts = [n for n in self._nodes if _normalize(n.ctgr_nm).startswith(needle)]
        contains = [
            n for n in self._nodes
            if needle in _normalize(n.ctgr_nm) and n not in starts
        ]
        return (starts + contains)[:limit]
