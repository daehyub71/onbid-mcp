"""용도 코드 트리 수집 (F1.2·F7.2).

온비드 용도 코드는 **대/중/소 3단 계층**이며 `getOnbidUsgCodeInfo` 로 한 단계씩 내려간다.
물건목록의 `cltrUsgLclsCtgrId`(대) · `cltrUsgMclsCtgrId`(중) · `cltrUsgSclsCtgrId`(소)가
이 트리의 노드다.

실측으로 확인한 순회 규칙:

- **루트는 값을 줘야 한다.** ``upCtgrId`` 를 빈 문자열로 보내면 ``99 UNKNOWN_ERROR`` 다.
  부동산의 루트는 ``10000`` 이다.
- **리프는 ``03 NODATA_ERROR``** 로 응답한다. 오류가 아니라 재귀 종료 신호다.
- 실제 규모: ``10000`` → 중분류 5개, ``10100``(토지) → 소분류 33개.
"""

import logging
from dataclasses import dataclass
from typing import Final

from core.onbid.client import OnbidClient
from core.onbid.parser import as_str, items_of

logger = logging.getLogger(__name__)

REALESTATE_ROOT: Final = "10000"
"""부동산 대분류. 자동차·동산은 다른 루트이며 수집 범위 밖이다 (SPEC §2.1)."""

REALESTATE_ROOT_NAME: Final = "부동산"

DEFAULT_MAX_DEPTH: Final = 5
"""실제 계층은 3단이다. 그보다 깊어지면 응답 구조가 바뀐 것이므로 멈춘다."""

DEFAULT_PAGE_SIZE: Final = 1000


@dataclass(frozen=True, slots=True)
class UsageCode:
    """용도 코드 한 노드.

    Attributes:
        ctgr_id: 코드 ID.
        ctgr_nm: 코드명.
        up_ctgr_id: 상위 코드 ID. 루트는 ``None``.
        up_ctgr_nm: 상위 코드명. 루트는 ``None``.
        depth: 1 대분류 · 2 중분류 · 3 소분류.
    """

    ctgr_id: str
    ctgr_nm: str
    up_ctgr_id: str | None
    up_ctgr_nm: str | None
    depth: int


async def fetch_usage_tree(
    client: OnbidClient,
    *,
    root: str = REALESTATE_ROOT,
    root_name: str = REALESTATE_ROOT_NAME,
    max_depth: int = DEFAULT_MAX_DEPTH,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[UsageCode]:
    """용도 트리를 루트부터 재귀 순회해 전 노드를 수집한다.

    Args:
        client: 온비드 클라이언트.
        root: 시작 코드 ID.
        root_name: 시작 코드명.
        max_depth: 최대 깊이. 응답 구조가 바뀌었을 때의 방어선.
        page_size: 페이지 크기.

    Returns:
        루트를 포함한 전 노드. 방문 순서(깊이 우선)를 유지한다.
    """
    nodes = [UsageCode(ctgr_id=root, ctgr_nm=root_name, up_ctgr_id=None,
                       up_ctgr_nm=None, depth=1)]
    visited = {root}
    await _walk(client, root, root_name, 1, max_depth, page_size, nodes, visited)
    logger.info("용도 트리 수집: %d노드 (최대 깊이 %d)",
                len(nodes), max(node.depth for node in nodes))
    return nodes


async def _walk(
    client: OnbidClient,
    parent_id: str,
    parent_name: str,
    depth: int,
    max_depth: int,
    page_size: int,
    nodes: list[UsageCode],
    visited: set[str],
) -> None:
    """`parent_id` 의 자식을 받아 `nodes` 에 추가하고 한 단계 더 내려간다."""
    if depth >= max_depth:
        return

    response = await client.call(
        "usage_code", pageNo=1, numOfRows=page_size, upCtgrId=parent_id
    )
    # 리프는 03 으로 응답한다. 파서가 빈 목록으로 흡수하므로 여기서는 조기 반환만 하면 된다.
    for row in items_of(response.payload):
        ctgr_id = as_str(row.get("ctgrId"))
        if ctgr_id is None or ctgr_id in visited:
            # 응답이 자기 자신이나 이미 본 노드를 돌려주면 순환이다.
            continue
        visited.add(ctgr_id)
        child = UsageCode(
            ctgr_id=ctgr_id,
            ctgr_nm=as_str(row.get("ctgrNm")) or "",
            up_ctgr_id=as_str(row.get("upCtgrId")) or parent_id,
            up_ctgr_nm=as_str(row.get("upCtgrNm")) or parent_name,
            depth=depth + 1,
        )
        nodes.append(child)
        await _walk(client, child.ctgr_id, child.ctgr_nm, child.depth,
                    max_depth, page_size, nodes, visited)
