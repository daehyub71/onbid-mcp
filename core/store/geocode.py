"""좌표 적재 (F3.6·PLAN §4.1①).

지오코딩은 수집과 **분리된 패스**로 돈다. 붙여 두면 카카오 장애 하나가 온비드 수집까지 멈춰
세우고, 좌표만 다시 채우고 싶을 때 전량을 다시 받아야 한다.

대상은 ``lat is null`` 인 행이다 — 마이그레이션의 부분 인덱스가 이 조건으로 걸려 있다.
**실패한 행도 대상에 남는다.** 좌표가 없으니 다시 시도하는 것이 맞고, 재호출은 지오코딩
캐시가 막아 준다 (F3.2).
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

import psycopg

from core.geocoder.resolver import GeocodeResult, GeocodeTarget

logger = logging.getLogger(__name__)

TABLE: Final = "onbid_cltr"

DEFAULT_CHUNK_SIZE: Final = 500

_SELECT_HEAD: Final = f"""
    select cltr_mng_no, pbct_cdtn_no, road_addr, jibun_addr, sd_nm, sgg_nm, emd_nm
      from {TABLE}
     where lat is null
"""

_SELECT_TAIL: Final = """
     order by bid_end asc nulls last, cltr_mng_no
     limit %(limit)s
"""

_UPDATE: Final = f"""
    update {TABLE}
       set lat = %(lat)s,
           lng = %(lng)s,
           geocode_status = %(status)s,
           geocode_level = %(level)s,
           geocode_src = %(src)s
     where cltr_mng_no = %(cltr_mng_no)s
       and pbct_cdtn_no = %(pbct_cdtn_no)s
"""


async def select_geocode_targets(
    conn: psycopg.AsyncConnection[Any],
    *,
    limit: int,
    sd_nm: str | None = None,
    sgg_nm: str | None = None,
) -> list[GeocodeTarget]:
    """좌표가 없는 물건을 골라 온다.

    **마감이 임박한 순**이다. 예산이 모자라 다 못 돌리면 곧 사라질 물건부터 좌표를 갖는 편이
    쓸모 있다.

    Args:
        conn: 열린 연결.
        limit: 최대 건수. 호출 예산과 맞춘다.
        sd_nm: 시도명으로 범위를 좁힌다. 특정 지역만 다시 채울 때 쓴다.
        sgg_nm: 시군구명으로 범위를 좁힌다.

    Returns:
        폴백 사다리에 넣을 대상들.
    """
    params: dict[str, Any] = {"limit": limit}
    clauses = ""
    if sd_nm:
        clauses += " and sd_nm = %(sd_nm)s"
        params["sd_nm"] = sd_nm
    if sgg_nm:
        clauses += " and sgg_nm = %(sgg_nm)s"
        params["sgg_nm"] = sgg_nm

    async with conn.cursor() as cur:
        await cur.execute(_SELECT_HEAD + clauses + _SELECT_TAIL, params)
        rows = await cur.fetchall()

    return [
        GeocodeTarget(
            key=(str(row[0]), str(row[1])),
            road_addr=row[2], jibun_addr=row[3],
            sd_nm=row[4], sgg_nm=row[5], emd_nm=row[6],
        )
        for row in rows
    ]


async def update_geocode(
    conn: psycopg.AsyncConnection[Any],
    results: Sequence[GeocodeResult],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """좌표와 출처를 기록한다 (F3.6).

    실패도 기록한다 — 상태를 남겨야 `approx`·`failed` 전수 검수(AC4)를 할 수 있다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        results: 사다리가 돌려준 결과들.
        chunk_size: 한 번에 보낼 행 수.

    Returns:
        처리한 행 수.
    """
    rows: list[Mapping[str, Any]] = [
        {
            "cltr_mng_no": result.key[0], "pbct_cdtn_no": result.key[1],
            "lat": result.lat, "lng": result.lng,
            "status": result.status, "level": result.level, "src": result.src,
        }
        for result in results
    ]
    if not rows:
        return 0

    processed = 0
    async with conn.cursor() as cur:
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            await cur.executemany(_UPDATE, chunk)
            processed += len(chunk)

    logger.info("좌표 적재: %d건", processed)
    return processed
