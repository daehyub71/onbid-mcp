"""`get_auction_detail` — 단건 상세 (SPEC §8.2).

**물건관리번호 하나에 공매조건번호가 최대 10개 붙는다** (F4.1). 조건을 지정하지 않으면 하나를
골라 줄 수밖에 없는데, 그 사실을 숨기면 사용자는 자기가 본 회차가 아닌 것을 보고도 모른다.
그래서 **형제 조건번호를 `meta` 에 함께** 싣는다.

**`raw_payload` 를 싣지 않는다.** 원본 전체는 수십 KB 라 LLM 컨텍스트를 잡아먹고, 조회에 쓰이지
않는 필드가 대부분이다.

없는 물건은 `not_found` 다 — `no_result` 와 다르다. 전자는 검색으로 유도하고 후자는 조건 완화를
제안한다 (§8.7).
"""

import logging
from datetime import datetime
from typing import Any, Final

import psycopg

from core.store.query import COLUMNS
from onbid_mcp.common import ToolError, ok_response

logger = logging.getLogger(__name__)

TABLE: Final = "onbid_cltr"

#: 최신 회차를 먼저 준다 — 생략 시 "가장 최근" 이 자연스러운 기대다 (§8.2).
ORDER: Final = "bid_end desc nulls last, pbct_cdtn_no desc"


async def get_auction_detail(
    conn: psycopg.AsyncConnection[Any],
    *,
    cltr_mng_no: str,
    pbct_cdtn_no: str | None = None,
) -> dict[str, Any]:
    """물건관리번호로 단건 상세를 조회한다.

    Args:
        conn: 열린 연결.
        cltr_mng_no: 물건관리번호.
        pbct_cdtn_no: 공매조건번호. 생략하면 최신 회차를 준다.

    Returns:
        `cltr` · `query_echo` · `meta`. `meta.sibling_conditions` 에 형제 조건번호가 있다.

    Raises:
        ToolError: 물건이 없으면 `not_found`.
    """
    if not (cltr_mng_no or "").strip():
        raise ToolError("invalid_param", "cltr_mng_no 는 필수입니다.")

    where = "where cltr_mng_no = %(mng)s"
    params: dict[str, Any] = {"mng": cltr_mng_no.strip()}
    if pbct_cdtn_no:
        where += " and pbct_cdtn_no = %(cdtn)s"
        params["cdtn"] = pbct_cdtn_no

    async with conn.cursor() as cur:
        await cur.execute(
            f"select {', '.join(COLUMNS)} from {TABLE} {where} order by {ORDER}", params)
        rows = list(await cur.fetchall())

    if not rows:
        # 검색으로 유도해야 하므로 `no_result` 가 아니라 `not_found` 다 (§8.7).
        raise ToolError("not_found",
                        f"물건을 찾을 수 없습니다: {cltr_mng_no}. 검색으로 먼저 찾아보세요.")

    items = [dict(zip(COLUMNS, row, strict=True)) for row in rows]
    synced: datetime | None = items[0].get("synced_at")

    return ok_response(
        {"cltr": items[0]},
        query_echo={"cltr_mng_no": cltr_mng_no, "pbct_cdtn_no": pbct_cdtn_no},
        count=1,
        synced_at=synced,
        meta_extra={"sibling_conditions": [i["pbct_cdtn_no"] for i in items]},
    )
