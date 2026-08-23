"""물건 적재 (F4.1·F4.3·F4.10).

복합키 ``(cltr_mng_no, pbct_cdtn_no)`` 로 upsert 한다. 온비드 상세·입찰정보 조회가
두 값을 함께 요구하므로 PK 도 복합키다.

**행 단위 왕복을 만들지 않는다** (F4.10). 서울 전량이 6,910건이라 한 건씩 넣으면
배치가 끝나지 않는다. psycopg 의 ``executemany`` 는 파이프라인으로 처리한다.

``first_seen_at`` 은 갱신 시 **덮어쓰지 않는다** — 처음 본 시각이라는 의미가 사라진다.
``last_seen_at`` 은 매번 갱신한다. tombstone 판정의 기준이기 때문이다 (F4.2).
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

TABLE: Final = "onbid_cltr"

KEY_COLUMNS: Final = ("cltr_mng_no", "pbct_cdtn_no")

#: 매 배치에서 갱신하지 않는 컬럼. 나머지는 전부 최신 값으로 덮어쓴다.
IMMUTABLE_COLUMNS: Final = frozenset({*KEY_COLUMNS, "first_seen_at"})

#: `raw_payload` 는 dict 로 오므로 psycopg 에 jsonb 임을 알려야 한다.
JSON_COLUMNS: Final = frozenset({"raw_payload"})

DEFAULT_CHUNK_SIZE: Final = 500
"""한 번에 보낼 행 수. 너무 크면 파라미터 상한에 걸린다."""


def _adapt(row: Mapping[str, Any]) -> dict[str, Any]:
    """psycopg 가 이해할 수 있는 값으로 바꾼다."""
    return {
        key: Jsonb(value) if key in JSON_COLUMNS and value is not None else value
        for key, value in row.items()
    }


def _statement(columns: Sequence[str]) -> str:
    """컬럼 목록에 맞는 upsert 문을 만든다."""
    placeholders = ", ".join(f"%({column})s" for column in columns)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in columns
        if column not in IMMUTABLE_COLUMNS
    )
    conflict = ", ".join(KEY_COLUMNS)
    return (
        f"insert into {TABLE} ({', '.join(columns)}) values ({placeholders}) "
        f"on conflict ({conflict}) do update set {updates}"
    )


async def upsert_cltr(
    conn: psycopg.AsyncConnection[Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """물건을 배치로 적재한다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다 — 배치 전체를 한 트랜잭션으로 묶을 수 있다.
        rows: `to_cltr_row` 가 만든 행들.
        chunk_size: 한 번에 보낼 행 수.

    Returns:
        처리한 행 수.

    Raises:
        KeyError: 복합키 컬럼이 없는 행이 섞였을 때.
    """
    if not rows:
        return 0

    columns = list(rows[0].keys())
    for column in KEY_COLUMNS:
        if column not in columns:
            raise KeyError(f"복합키 컬럼 {column} 이 없다")

    sql = _statement(columns)
    processed = 0
    async with conn.cursor() as cur:
        for start in range(0, len(rows), chunk_size):
            chunk = [_adapt(row) for row in rows[start : start + chunk_size]]
            await cur.executemany(sql, chunk)
            processed += len(chunk)

    logger.info("물건 적재: %d건", processed)
    return processed
