"""지오코딩 캐시 (F3.2).

**모든 시도 전에 여기를 먼저 본다.** 카카오 앱을 다른 프로젝트와 공유하므로, 같은 주소를 두 번
묻는 것은 남의 쿼터까지 태우는 일이다.

**실패도 캐시한다.** 좌표를 못 찾은 주소를 캐시하지 않으면 매일 다시 묻게 되고, 성공한 주소는
한 번 캐시되면 다시 안 물으므로 **결국 호출의 대부분이 실패 주소에 쓰인다.** 다시 시도하고
싶으면 해당 행을 지운다 — 캐시가 조용히 만료되지 않는 편이 예측하기 쉽다.

주소는 **정규화해서** 넣고 뺀다. 공백 차이로 같은 주소가 두 행이 되면 적중률이 떨어진다.
"""

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

import psycopg

logger = logging.getLogger(__name__)

TABLE: Final = "onbid_geocode_cache"

_WHITESPACE: Final = re.compile(r"\s+")

_LOOKUP: Final = f"""
    select addr, lat, lng, src, level
      from {TABLE}
     where addr = any(%(addrs)s)
"""

_UPSERT: Final = f"""
    insert into {TABLE} (addr, lat, lng, src, level, cached_at)
    values (%(addr)s, %(lat)s, %(lng)s, %(src)s, %(level)s, now())
    on conflict (addr) do update set
        lat = excluded.lat,
        lng = excluded.lng,
        src = excluded.src,
        level = excluded.level,
        cached_at = now()
"""


def normalize_addr(addr: str | None) -> str:
    """캐시 키로 쓸 형태로 다듬는다.

    Args:
        addr: 원본 주소.

    Returns:
        연속 공백을 하나로 줄이고 양끝을 자른 주소. 값이 없으면 빈 문자열.
    """
    if not addr:
        return ""
    return _WHITESPACE.sub(" ", addr).strip()


@dataclass(frozen=True, slots=True)
class CachedPoint:
    """캐시된 지오코딩 결과.

    Attributes:
        addr: 주소 (정규화 전 값을 넣어도 저장 시 정규화된다).
        lat: 위도. 실패면 None.
        lng: 경도. 실패면 None.
        src: ``kakao`` | ``vworld``. 실패면 None 일 수 있다.
        level: ``road`` | ``jibun`` | ``trimmed`` | ``dong_center``.
    """

    addr: str
    lat: float | None
    lng: float | None
    src: str | None = None
    level: str | None = None

    @property
    def is_failure(self) -> bool:
        """좌표를 얻지 못한 결과인지 여부."""
        return self.lat is None or self.lng is None


async def lookup_cache(
    conn: psycopg.AsyncConnection[Any], addrs: Sequence[str]
) -> dict[str, CachedPoint]:
    """주소들의 캐시를 **한 번의 질의로** 조회한다.

    Args:
        conn: 열린 연결.
        addrs: 조회할 주소들. 정규화는 이 함수가 한다.

    Returns:
        정규화된 주소 → 캐시값. **없는 주소는 키 자체가 없다** — 캐시 미스와
        '캐시된 실패' 를 구분하기 위해서다.
    """
    keys = sorted({normalize_addr(a) for a in addrs if normalize_addr(a)})
    if not keys:
        return {}

    async with conn.cursor() as cur:
        await cur.execute(_LOOKUP, {"addrs": keys})
        rows = await cur.fetchall()

    return {
        str(row[0]): CachedPoint(
            addr=str(row[0]),
            lat=float(row[1]) if row[1] is not None else None,
            lng=float(row[2]) if row[2] is not None else None,
            src=row[3], level=row[4],
        )
        for row in rows
    }


async def store_cache(
    conn: psycopg.AsyncConnection[Any], points: Iterable[CachedPoint]
) -> int:
    """결과를 캐시에 넣는다. 이미 있으면 덮어쓴다.

    폴백 단계가 올라가 더 정확한 좌표를 얻으면 갱신돼야 하므로 덮어쓰기가 맞다.
    **빈 주소는 넣지 않는다** — 모든 빈 주소가 서로의 결과를 물려받게 된다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        points: 저장할 결과들.

    Returns:
        저장한 행 수.
    """
    rows = []
    for point in points:
        addr = normalize_addr(point.addr)
        if not addr:
            continue
        rows.append({"addr": addr, "lat": point.lat, "lng": point.lng,
                     "src": point.src, "level": point.level})
    if not rows:
        return 0

    async with conn.cursor() as cur:
        await cur.executemany(_UPSERT, rows)

    logger.debug("지오코딩 캐시 저장: %d건", len(rows))
    return len(rows)
