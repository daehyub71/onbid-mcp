"""낙찰가율 집계 (SPEC §8.3·D18).

**이 통계는 편향돼 있고, 그 사실이 숫자보다 중요하다.**

우리가 볼 수 있는 낙찰은 "낙찰됐으나 계약이 무산되어 **다시 공매에 나온**" 건뿐이다. 정상적으로
낙찰·계약이 끝난 물건은 온비드 목록 API 에 나오지 않아 표본에 아예 없다. 그래서 이 분포를
일반적인 낙찰가율로 읽으면 결론이 뒤집힌다 — "공매는 감정가의 40%에 낙찰된다" 같은 요약은
**무산된 건들만 본 결과**다.

그래서 `caveat` 은 선택 항목이 아니다. 결과 타입이 **항상** 들고 다니며, 표본이 0건일 때도
빠지지 않는다 — 0건일 때야말로 해석 주의가 필요하다.

**두 지표를 분리한다.** 값의 범위부터 다르다.

| 지표 | 정의 | 의미 | 실측 |
|---|---|---|---|
| `win_to_appraisal` | 낙찰가 ÷ **감정가** | 통상적 낙찰가율 | 3.4%~71.4% |
| `win_to_min_bid` | 낙찰가 ÷ 그 회차 **최저입찰가** | 입찰 경쟁 강도 | 102%~300% |

섞으면 300% 짜리 경쟁 강도가 낙찰가율로 읽힌다.

**집계 단위는 낙찰 회차(이벤트)** 다. 한 물건이 여러 번 낙찰·무산되면 여러 번 기여하므로,
물건 수를 함께 실어 그 사실을 드러낸다.

> D20 이 해소되면(사라진 물건에도 입찰정보 API 가 응답한다면) 정상 낙찰 건까지 표본에 넣을 수
> 있고, 그때 이 caveat 을 재검토한다.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import psycopg

from core.stats.distribution import Bucket
from core.store.query import ListingQuery, build_where

logger = logging.getLogger(__name__)

ROUND_TABLE: Final = "onbid_cltr_bid_round"
CLTR_TABLE: Final = "onbid_cltr"

WON_STATUS: Final = "낙찰"

POPULATION: Final = "재공매 물건의 과거 낙찰 회차"

CAVEAT: Final = (
    "현재 재공매 중인 물건의 과거 낙찰 회차만 집계한 값입니다. 정상 낙찰되어 종료된 물건은 "
    "온비드 목록 API에 나오지 않아 표본에서 빠져 있으므로, 일반적인 낙찰가율로 해석하면 안 됩니다."
)

BUCKET_STEP: Final = 10
"""10%p 단위. 경쟁 강도는 100% 를 훌쩍 넘으므로 상한을 두지 않는다."""


@dataclass(frozen=True, slots=True)
class RatioStats:
    """한 지표의 분포.

    Attributes:
        metric: 지표 이름.
        buckets: 10%p 구간 분포.
        n: 표본 수 (낙찰 회차).
        median: 중앙값. 표본이 없으면 None.
    """

    metric: str
    buckets: Sequence[Bucket]
    n: int
    median: float | None


@dataclass(frozen=True, slots=True)
class WinRateStats:
    """낙찰가율 집계 결과.

    `caveat` 과 `population` 은 **기본값이 있어 빠질 수 없다** — 호출자가 넣기를 잊어도
    편향 설명이 함께 간다 (§8.3 필수 구현).

    Attributes:
        win_to_appraisal: 낙찰가÷감정가 분포.
        win_to_min_bid: 낙찰가÷회차 최저입찰가 분포.
        n: 낙찰 회차 수. **집계 단위는 이벤트다.**
        property_count: 기여한 물건 수. `n` 과 다르면 한 물건이 여러 번 낙찰·무산된 것이다.
        caveat: 모집단 편향 설명. 항상 채워진다.
        population: 표본이 무엇인지.
    """

    win_to_appraisal: RatioStats
    win_to_min_bid: RatioStats
    n: int
    property_count: int
    caveat: str = CAVEAT
    population: str = POPULATION


_SQL: Final = f"""
    select r.winning_amt::numeric / c.appraisal_amt          as to_appraisal,
           case when r.min_bid_amt > 0
                then r.winning_amt::numeric / r.min_bid_amt
                end                                          as to_min_bid,
           r.cltr_mng_no, r.pbct_cdtn_no
      from {ROUND_TABLE} r
      join {CLTR_TABLE} c
        on c.cltr_mng_no = r.cltr_mng_no
       and c.pbct_cdtn_no = r.pbct_cdtn_no
     where r.winning_amt is not null
       and c.appraisal_amt > 0
"""


def _bucket_key(ratio: float) -> str:
    """10%p 구간 키. 상한을 두지 않는다 — 경쟁 강도는 300% 까지 간다."""
    low = int(ratio * 100 // BUCKET_STEP) * BUCKET_STEP
    return f"{low}-{low + BUCKET_STEP - 1}"


def _to_buckets(values: Sequence[float]) -> list[Bucket]:
    counts: dict[str, int] = {}
    for value in values:
        key = _bucket_key(value)
        counts[key] = counts.get(key, 0) + 1

    return [
        Bucket(key=key, label=f"{key.split('-')[0]}~{key.split('-')[1]}%", count=count)
        for key, count in sorted(counts.items(), key=lambda kv: int(kv[0].split("-")[0]))
    ]


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _stats(metric: str, values: Sequence[float]) -> RatioStats:
    return RatioStats(metric=metric, buckets=_to_buckets(values),
                      n=len(values), median=_median(values))


async def aggregate_win_rates(
    conn: psycopg.AsyncConnection[Any],
    *,
    query: ListingQuery,
) -> WinRateStats:
    """낙찰 회차의 두 비율 분포를 낸다.

    **감정가가 없는 회차는 제외한다.** 0으로 채우면 분포가 왼쪽으로 무너져 "헐값 낙찰이
    많다" 는 잘못된 인상을 준다.

    Args:
        conn: 열린 연결.
        query: 물건 쪽 조건 (지역·용도·재산유형 등).

    Returns:
        두 지표의 분포와 **모집단 편향 설명**.

    Raises:
        ValueError: 조건이 잘못됐을 때.
    """
    clauses, params = build_where(query)
    # 물건 쪽 조건은 조인한 `c` 에 걸린다.
    scoped = [f"c.{clause}" if clause[0].isalpha() else clause for clause in clauses]
    where = f" and {' and '.join(scoped)}" if scoped else ""
    params["won"] = WON_STATUS

    async with conn.cursor() as cur:
        await cur.execute(f"{_SQL}   and r.status = %(won)s{where}", params)
        rows = await cur.fetchall()

    to_appraisal = [float(row[0]) for row in rows if row[0] is not None]
    to_min_bid = [float(row[1]) for row in rows if row[1] is not None]
    properties = {(row[2], row[3]) for row in rows}

    logger.debug("낙찰가율: 회차 %d · 물건 %d", len(rows), len(properties))
    return WinRateStats(
        win_to_appraisal=_stats("win_to_appraisal", to_appraisal),
        win_to_min_bid=_stats("win_to_min_bid", to_min_bid),
        n=len(rows),
        property_count=len(properties),
    )


def as_meta(stats: WinRateStats) -> Mapping[str, Any]:
    """툴 응답 `meta` 에 실을 형태 (§8.3).

    Args:
        stats: 집계 결과.

    Returns:
        `caveat`·`population`·표본 크기를 담은 매핑.
    """
    return {
        "caveat": stats.caveat,
        "population": stats.population,
        "n": stats.n,
        "property_count": stats.property_count,
    }
