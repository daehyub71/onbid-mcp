"""분포 집계 (SPEC §8.3).

집계는 **개별 물건이 아니라 분포**를 돌려준다. 그래서 조용히 틀려도 눈에 띄지 않는다 —
어떤 행이 어느 칸에도 들어가지 않으면 합계만 줄어들 뿐 오류가 나지 않는다. 그래서 이 모듈은
**모든 행을 어딘가에 넣는다.** 값이 없는 행에는 `미산출` 칸을 준다.

**100% 초과 구간을 따로 둔다.** 최저가율은 100% 를 넘고(실측 9.8%, 최대 150.2%), 100% 칸에
합치거나 버리면 그만큼이 분포에서 사라진다 (F4.5).

**재산유형 혼재 경고는 선택이 아니다** (§8.3 필수 구현). 최저가율·유찰횟수 분포를 유형 필터
없이 뽑으면 저감 체계가 다른 10종이 한 분포에 섞인다. LLM 이 이를 단일 모집단으로 해석하면
"강남 물건은 평균 62%" 같은 잘못된 요약이 나온다.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import psycopg

from core.store.query import TABLE, ListingQuery, build_where

logger = logging.getLogger(__name__)

UNKNOWN_KEY: Final = "unknown"

#: 최저가율 구간. 10%p 단위 + **100% 초과 구간**. 실측 중앙값 62.5%, 최빈 60~69%.
RATE_STEP: Final = 10
RATE_MAX: Final = 100

#: 유찰횟수는 0~9 를 그대로 두고 10회 이상을 묶는다. 실측 최대 87회라 펼치면 꼬리가 길다.
FAIL_CAP: Final = 10

#: 축마다 `(그룹 키 식, 라벨 식)`. 값이 없으면 `미산출` 로 모은다.
AXES: Final[dict[str, tuple[str, str]]] = {
    "min_bid_rate_bucket": ("", ""),   # 아래에서 따로 만든다
    "fail_cnt": ("", ""),
    "usage": ("coalesce(usg_mcls_id, usg_lcls_id)", "coalesce(usg_mcls_nm, usg_lcls_nm)"),
    "region": ("sgg_nm", "sgg_nm"),
    "prpt_div": ("prpt_div_cd", "prpt_div_nm"),
    "pvct_trgt": ("", ""),
}

#: 이 축들은 재산유형이 섞이면 해석이 뒤집힌다 (§8.3).
MIXING_SENSITIVE: Final = frozenset({"min_bid_rate_bucket", "fail_cnt"})

MIXED_CAVEAT: Final = (
    "재산유형 10종이 합산된 분포입니다. 유형별로 감정가 산정과 유찰 저감 체계가 다르므로 "
    "prpt_div로 구분해 조회하는 것이 정확합니다."
)

_RATE_GROUP: Final = f"""
    case
      when min_bid_rate is null then '{UNKNOWN_KEY}'
      when min_bid_rate >= {RATE_MAX / 100.0} then '{RATE_MAX}+'
      else (floor(min_bid_rate * 100 / {RATE_STEP}) * {RATE_STEP})::int::text
           || '-' ||
           (floor(min_bid_rate * 100 / {RATE_STEP}) * {RATE_STEP} + {RATE_STEP - 1})::int::text
    end
"""

_FAIL_GROUP: Final = f"""
    case
      when fail_cnt is null then '{UNKNOWN_KEY}'
      when fail_cnt >= {FAIL_CAP} then '{FAIL_CAP}+'
      else fail_cnt::text
    end
"""

_PVCT_GROUP: Final = f"""
    case
      when pvct_trgt_yn is null then '{UNKNOWN_KEY}'
      when pvct_trgt_yn then 'Y'
      else 'N'
    end
"""

_PVCT_LABELS: Final = {"Y": "수의계약 가능", "N": "수의계약 불가", UNKNOWN_KEY: "미산출"}


@dataclass(frozen=True, slots=True)
class Bucket:
    """분포 한 칸. **집계값만** 담는다 — 개별 물건 식별정보를 섞지 않는다 (§8.3).

    Attributes:
        key: 기계용 키.
        label: 사람이 읽는 이름.
        count: 건수.
    """

    key: str
    label: str
    count: int


@dataclass(frozen=True, slots=True)
class Distribution:
    """집계 결과.

    Attributes:
        group_by: 집계 축.
        buckets: 분포. **합계가 `n` 과 같다.**
        n: 대상 건수.
        caveat: 해석 주의. 재산유형이 섞였을 때만 채워진다.
        prpt_div_breakdown: 유형별 건수. caveat 이 있을 때 함께 준다.
    """

    group_by: str
    buckets: Sequence[Bucket]
    n: int
    caveat: str | None = None
    prpt_div_breakdown: Mapping[str, int] | None = None


def _group_expression(axis: str) -> tuple[str, str | None]:
    """축의 그룹 키 식과 라벨 식."""
    if axis == "min_bid_rate_bucket":
        return _RATE_GROUP, None
    if axis == "fail_cnt":
        return _FAIL_GROUP, None
    if axis == "pvct_trgt":
        return _PVCT_GROUP, None
    key, label = AXES[axis]
    return key, label


def _rate_label(key: str) -> str:
    if key == UNKNOWN_KEY:
        return "미산출 (감정가 없음)"
    if key.endswith("+"):
        return f"{key[:-1]}% 초과"
    low, high = key.split("-")
    return f"{low}~{high}%"


def _fail_label(key: str) -> str:
    if key == UNKNOWN_KEY:
        return "미산출"
    return f"{key[:-1]}회 이상" if key.endswith("+") else f"{key}회"


def _sort_key(axis: str, key: str) -> tuple[int, float, str]:
    """분포는 순서대로 읽힌다 — 뒤섞이면 사람이 해석할 수 없다."""
    if axis in {"min_bid_rate_bucket", "fail_cnt"}:
        if key == UNKNOWN_KEY:
            return (1, 0.0, key)          # 미산출은 맨 뒤
        head = key.split("-")[0].rstrip("+")
        return (0, float(head), key)
    return (0, 0.0, key)


def _label_for(axis: str, key: str, raw_label: Any) -> str:
    if axis == "min_bid_rate_bucket":
        return _rate_label(key)
    if axis == "fail_cnt":
        return _fail_label(key)
    if axis == "pvct_trgt":
        return _PVCT_LABELS[key]
    return str(raw_label) if raw_label is not None else "미상"


async def _breakdown(
    conn: psycopg.AsyncConnection[Any], where: str, params: Mapping[str, Any]
) -> dict[str, int]:
    """재산유형별 건수. 혼재 경고와 함께 준다."""
    sql = (
        f"select coalesce(prpt_div_nm, '미상'), count(*)"
        f"\n  from {TABLE}{where}\n group by 1 order by 2 desc"
    )
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return {str(row[0]): int(row[1]) for row in await cur.fetchall()}


async def aggregate(
    conn: psycopg.AsyncConnection[Any],
    *,
    group_by: str,
    query: ListingQuery,
) -> Distribution:
    """조건에 맞는 물건의 분포를 낸다.

    Args:
        conn: 열린 연결.
        group_by: 집계 축. `AXES` 의 키여야 한다.
        query: 모집단을 좁히는 조건.

    Returns:
        분포. **버킷 합계가 `n` 과 같다.**

    Raises:
        ValueError: 알 수 없는 축이거나 조건이 잘못됐을 때.
    """
    if group_by not in AXES:
        raise ValueError(f"알 수 없는 집계 축: {group_by!r} (가능: {sorted(AXES)})")

    clauses, params = build_where(query)
    where = f"\n where {' and '.join(clauses)}" if clauses else ""

    key_expr, label_expr = _group_expression(group_by)
    label_select = label_expr or "null"
    sql = (
        f"select ({key_expr}) as bucket_key, max({label_select}) as bucket_label, count(*)"
        f"\n  from {TABLE}{where}\n group by 1"
    )

    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()

    buckets = [
        Bucket(key=str(row[0]), label=_label_for(group_by, str(row[0]), row[1]),
               count=int(row[2]))
        for row in rows
    ]
    buckets.sort(key=lambda b: _sort_key(group_by, b.key))
    total = sum(b.count for b in buckets)

    # 유형을 이미 좁혔으면 섞이지 않았으므로 경고할 이유가 없다.
    mixed = group_by in MIXING_SENSITIVE and not query.prpt_div_cds
    breakdown = await _breakdown(conn, where, params) if mixed else None

    logger.debug("분포 집계: %s · %d건 · %d칸", group_by, total, len(buckets))
    return Distribution(
        group_by=group_by, buckets=buckets, n=total,
        caveat=MIXED_CAVEAT if mixed else None,
        prpt_div_breakdown=breakdown,
    )
