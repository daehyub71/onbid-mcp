"""변경 이력 적재 (F4.4).

온비드 응답은 **현재 상태만** 준다. 어제 최저가가 얼마였는지는 어디에도 없다. 그래서
값이 바뀌는 순간을 우리가 남겨야 한다 — 놓치면 다시는 복원할 수 없다.

남기는 것은 세 가지뿐이다: ``min_bid_amt``·``fail_cnt``·``status``. 이름·썸네일까지
남기면 이력이 잡음으로 덮여 하락 곡선이 보이지 않는다.

**호출 순서가 곧 정확성이다.** 적재 뒤에 비교하면 DB 값이 이미 새 값이라 차이가 0이 되고,
예외도 없이 조용히 아무것도 기록되지 않는다. `upsert_with_history` 를 쓰면 순서를 잊을
수 없다 — `record_changes` 를 직접 부를 때는 **반드시 upsert 앞**이다.

F1.7 의 회차 이력(`prcnBidClgList`)과 상호 보완이다. 회차 이력은 유찰 물건의 과거를
통째로 주지만 유찰 0회 물건과 상태 변경은 담지 못한다 — 그 몫이 여기다.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import psycopg

from core.store.cltr import KEY_COLUMNS, upsert_cltr

logger = logging.getLogger(__name__)

TABLE: Final = "onbid_cltr_history"

TRACKED_FIELDS: Final = ("min_bid_amt", "fail_cnt", "status")
"""이력을 남기는 컬럼. 늘리기 전에 잡음이 되지 않을지 따져본다."""

DEFAULT_CHUNK_SIZE: Final = 500

_INSERT: Final = f"""
    insert into {TABLE} (cltr_mng_no, pbct_cdtn_no, field, old_value, new_value)
    values (%(cltr_mng_no)s, %(pbct_cdtn_no)s, %(field)s, %(old_value)s, %(new_value)s)
"""

_SELECT: Final = f"""
    select {", ".join((*KEY_COLUMNS, *TRACKED_FIELDS))}
      from onbid_cltr
     where cltr_mng_no = any(%(mng_nos)s)
       and pbct_cdtn_no = any(%(cdtn_nos)s)
"""


@dataclass(frozen=True, slots=True)
class LoadResult:
    """적재 결과.

    Attributes:
        upserted: 적재한 물건 수.
        changes: 남긴 이력 행 수.
    """

    upserted: int
    changes: int


def _text(value: Any) -> str | None:
    """이력 컬럼은 text 다 — 타입이 달라도 비교가 성립하도록 문자열로 맞춘다."""
    return None if value is None else str(value)


def _key(row: Mapping[str, Any]) -> tuple[str, str] | None:
    mng, cdtn = row.get("cltr_mng_no"), row.get("pbct_cdtn_no")
    return (str(mng), str(cdtn)) if mng is not None and cdtn is not None else None


async def _current(
    conn: psycopg.AsyncConnection[Any], rows: Sequence[Mapping[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """지금 DB에 있는 값을 한 번에 읽는다.

    두 컬럼을 각각 ``any()`` 로 거르면 교차곱이 섞여 들어올 수 있으므로,
    **복합키가 정확히 일치하는 행만** 남긴다.
    """
    wanted = {key for key in (_key(row) for row in rows) if key is not None}
    if not wanted:
        return {}

    params = {
        "mng_nos": sorted({mng for mng, _ in wanted}),
        "cdtn_nos": sorted({cdtn for _, cdtn in wanted}),
    }
    async with conn.cursor() as cur:
        await cur.execute(_SELECT, params)
        found = await cur.fetchall()

    current: dict[tuple[str, str], dict[str, Any]] = {}
    for record in found:
        key = (str(record[0]), str(record[1]))
        if key in wanted:
            current[key] = dict(zip(TRACKED_FIELDS, record[2:], strict=True))
    return current


def _diff(
    row: Mapping[str, Any], before: Mapping[str, Any], key: tuple[str, str]
) -> list[dict[str, Any]]:
    """한 행의 추적 컬럼 차이를 이력 행으로 만든다."""
    entries: list[dict[str, Any]] = []
    for field in TRACKED_FIELDS:
        old, new = _text(before.get(field)), _text(row.get(field))
        if old == new:
            continue
        entries.append({
            "cltr_mng_no": key[0], "pbct_cdtn_no": key[1],
            "field": field, "old_value": old, "new_value": new,
        })
    return entries


async def record_changes(
    conn: psycopg.AsyncConnection[Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """새 값과 DB 값을 비교해 변경 이력을 남긴다.

    **반드시 `upsert_cltr` 앞에서 호출한다** — 뒤에서 부르면 차이가 0이 된다.

    처음 보는 물건은 이력을 남기지 않는다. null→값 은 '변경' 이 아니라 '등장' 이고,
    그 시각은 ``first_seen_at`` 이 이미 갖고 있다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        rows: `to_cltr_row` 가 만든 이번 배치의 행들.
        chunk_size: 한 번에 비교·삽입할 행 수.

    Returns:
        남긴 이력 행 수.
    """
    if not rows:
        return 0

    recorded = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        before = await _current(conn, chunk)

        entries: list[dict[str, Any]] = []
        for row in chunk:
            key = _key(row)
            # 처음 보는 물건은 비교 대상이 없다 — 등장은 변경이 아니다.
            if key is None or key not in before:
                continue
            entries.extend(_diff(row, before[key], key))

        if entries:
            async with conn.cursor() as cur:
                await cur.executemany(_INSERT, entries)
            recorded += len(entries)

    if recorded:
        logger.info("변경 이력: %d건", recorded)
    return recorded


async def upsert_with_history(
    conn: psycopg.AsyncConnection[Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> LoadResult:
    """이력을 남긴 뒤 적재한다 — **순서를 잊을 수 없게 묶어둔 경로다.**

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        rows: `to_cltr_row` 가 만든 행들.
        chunk_size: 한 번에 처리할 행 수.

    Returns:
        적재 건수와 이력 건수.
    """
    changes = await record_changes(conn, rows, chunk_size=chunk_size)
    upserted = await upsert_cltr(conn, rows, chunk_size=chunk_size)
    return LoadResult(upserted=upserted, changes=changes)
