"""용도 트리 조회 인덱스 테스트 (F1.2·F6.6·F6.7·F7.2).

MCP 툴은 `usage` 파라미터로 **코드값과 한글 명칭을 모두** 받는다 (F6.6).
매칭에 실패하면 **후보 목록**을 돌려줘야 LLM 이 재시도할 수 있다 (F6.7).
Resource `onbid://codes/usages` 도 이 인덱스를 노출한다 (F7.2).
"""

import pytest

from core.codes.index import UsageIndex
from core.codes.usage import UsageCode

#: 실측 구조의 축소판 — 대분류 1 · 중분류 2 · 소분류 4.
NODES = [
    UsageCode("10000", "부동산", None, None, 1),
    UsageCode("10100", "토지", "10000", "부동산", 2),
    UsageCode("10101", "대지", "10100", "토지", 3),
    UsageCode("10102", "임야", "10100", "토지", 3),
    UsageCode("10200", "주거용건물", "10000", "부동산", 2),
    UsageCode("10201", "아파트", "10200", "주거용건물", 3),
    UsageCode("10202", "단독주택", "10200", "주거용건물", 3),
]


@pytest.fixture
def index() -> UsageIndex:
    return UsageIndex(NODES)


# ── 코드 조회 ───────────────────────────────────────────────────────────


def test_index_finds_by_id(index: UsageIndex) -> None:
    node = index.by_id("10201")
    assert node is not None
    assert node.ctgr_nm == "아파트"


def test_index_returns_none_for_unknown_id(index: UsageIndex) -> None:
    assert index.by_id("99999") is None
    assert index.by_id(None) is None


def test_index_size(index: UsageIndex) -> None:
    assert len(index) == len(NODES)


# ── 명칭 조회 (F6.6) ────────────────────────────────────────────────────


def test_index_finds_by_exact_name(index: UsageIndex) -> None:
    found = index.by_name("아파트")
    assert [n.ctgr_id for n in found] == ["10201"]


def test_index_name_lookup_ignores_spaces(index: UsageIndex) -> None:
    """온비드 명칭에 공백이 없는 것(`산업용및기타특수용건물`)을 사용자가 띄어 쓸 수 있다."""
    assert index.by_name(" 아파트 ")[0].ctgr_id == "10201"


def test_index_name_lookup_returns_all_matches(index: UsageIndex) -> None:
    """같은 이름이 여러 깊이에 있을 수 있으므로 목록으로 돌려준다."""
    duplicated = [*NODES, UsageCode("10999", "아파트", "10100", "토지", 3)]
    assert len(UsageIndex(duplicated).by_name("아파트")) == 2


def test_index_name_lookup_is_empty_for_unknown(index: UsageIndex) -> None:
    assert index.by_name("없는용도") == []


# ── 후보 제안 (F6.7) ────────────────────────────────────────────────────


def test_index_suggests_partial_matches(index: UsageIndex) -> None:
    """매칭 실패 시 LLM 이 재시도할 수 있게 후보를 준다."""
    assert [n.ctgr_nm for n in index.suggest("주거")] == ["주거용건물"]


def test_index_suggest_matches_anywhere_in_the_name(index: UsageIndex) -> None:
    assert "단독주택" in [n.ctgr_nm for n in index.suggest("주택")]


def test_index_suggest_caps_the_list(index: UsageIndex) -> None:
    """후보가 많으면 잘라 준다 — LLM 컨텍스트를 낭비하지 않는다."""
    assert len(index.suggest("", limit=2)) == 2
    assert len(index.suggest("주", limit=1)) == 1


def test_index_suggest_prefers_deeper_specific_names(index: UsageIndex) -> None:
    """검색어로 시작하는 이름이 먼저 온다."""
    found = [n.ctgr_nm for n in index.suggest("토지")]
    assert found[0] == "토지"


def test_index_suggest_shows_top_categories_for_blank(index: UsageIndex) -> None:
    """검색어가 없으면 상위 분류를 보여준다 — 빈 목록보다 쓸모 있다."""
    found = index.suggest(None)
    assert [n.ctgr_id for n in found] == ["10000", "10100", "10200"]
    assert all(n.depth <= 2 for n in found)


# ── 계층 이동 ───────────────────────────────────────────────────────────


def test_index_lists_children(index: UsageIndex) -> None:
    assert [n.ctgr_id for n in index.children("10100")] == ["10101", "10102"]


def test_index_children_of_leaf_is_empty(index: UsageIndex) -> None:
    assert index.children("10101") == []


def test_index_walks_ancestors(index: UsageIndex) -> None:
    """소분류에서 중·대분류를 거슬러 올라간다."""
    assert [n.ctgr_id for n in index.ancestors("10201")] == ["10200", "10000"]


def test_index_ancestors_of_root_is_empty(index: UsageIndex) -> None:
    assert index.ancestors("10000") == []


def test_index_builds_readable_path(index: UsageIndex) -> None:
    """MCP 응답에 그대로 실어 LLM 이 계층을 알게 한다."""
    assert index.path("10201") == "부동산 > 주거용건물 > 아파트"


def test_index_path_of_unknown_is_none(index: UsageIndex) -> None:
    assert index.path("99999") is None


def test_index_lists_by_depth(index: UsageIndex) -> None:
    assert [n.ctgr_id for n in index.at_depth(2)] == ["10100", "10200"]


# ── 조회 확장 (F6.6) ────────────────────────────────────────────────────


def test_index_resolve_accepts_a_code(index: UsageIndex) -> None:
    """숫자로 보이면 코드로 해석한다."""
    assert [n.ctgr_id for n in index.resolve("10201")] == ["10201"]


def test_index_resolve_accepts_a_name(index: UsageIndex) -> None:
    assert [n.ctgr_id for n in index.resolve("아파트")] == ["10201"]


def test_index_resolve_expands_to_descendants(index: UsageIndex) -> None:
    """중분류를 지정하면 그 아래 소분류까지 포함해야 검색이 맞는다."""
    ids = {n.ctgr_id for n in index.resolve("주거용건물", expand=True)}
    assert ids == {"10200", "10201", "10202"}


def test_index_resolve_is_empty_for_unknown(index: UsageIndex) -> None:
    assert index.resolve("없는용도") == []


def test_index_resolve_expands_more_than_one_level(index: UsageIndex) -> None:
    """대분류에서 소분류까지 **두 단계 아래**로 내려가야 한다.

    한 단계만 내려가면 대분류 검색이 중분류만 잡아 소분류 물건을 놓친다.
    """
    ids = {n.ctgr_id for n in index.resolve("부동산", expand=True)}
    assert ids == {n.ctgr_id for n in NODES}


def test_index_descendants_reaches_leaves(index: UsageIndex) -> None:
    found = {n.ctgr_id for n in index.descendants("10000")}
    assert {"10101", "10102", "10201", "10202"} <= found


# ── 실데이터 ────────────────────────────────────────────────────────────


def test_index_on_real_tree_shape() -> None:
    """실측 트리는 대1 · 중5 · 소110 = 116노드다."""
    tree = [
        UsageCode("10000", "부동산", None, None, 1),
        *[UsageCode(f"10{i}00", f"중분류{i}", "10000", "부동산", 2) for i in range(1, 6)],
    ]
    index = UsageIndex(tree)
    assert len(index.at_depth(2)) == 5
    assert index.path("10100") == "부동산 > 중분류1"
