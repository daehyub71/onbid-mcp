"""`get_auction_stats` — 분포 집계 (SPEC §8.3).

**집계는 개별 물건을 담지 않는다.** 조회형의 경계를 지키기 위해서다.

두 종류의 caveat 을 강제한다.

- **재산유형 혼재** — 최저가율·유찰횟수를 유형 필터 없이 뽑으면 저감 체계가 다른 10종이 섞인다.
  LLM 이 단일 모집단으로 읽으면 "강남 물건은 평균 62%" 같은 잘못된 요약이 나온다.
- **낙찰가율 모집단 편향** — 표본이 "낙찰됐다가 무산되어 다시 나온" 건뿐이다. 일반적인
  낙찰가율로 읽으면 결론이 뒤집힌다 (D18·D20).
"""

import logging
from typing import Any, Final

import psycopg

from core.codes.resolve import RegionIndex, resolve_property_type
from core.stats.distribution import AXES, aggregate
from core.stats.win_rate import aggregate_win_rates, as_meta
from core.store.codes import load_address_entries
from core.store.query import ListingQuery
from onbid_mcp.common import ToolError, ok_response

logger = logging.getLogger(__name__)

WIN_RATE_AXIS: Final = "win_rate"
"""낙찰가율은 분포 축이 아니라 별도 집계다 — 두 지표를 함께 돌려준다."""

GROUP_BY_CHOICES: Final = (*sorted(AXES), WIN_RATE_AXIS)

DEFAULT_STATUS: Final = "진행"
STATUS_CHOICES: Final = {"진행": ("진행",), "종료추정": ("종료추정",), "전체": ()}


async def _query(
    conn: psycopg.AsyncConnection[Any],
    region: str | None,
    prpt_div: str | None,
    status: str | None,
) -> ListingQuery:
    """필터를 조회 조건으로 옮긴다.

    Raises:
        ToolError: 명칭 매칭 실패 시 후보와 함께 (F6.7).
    """
    sgg_nm = emd_nm = None
    if region:
        index = RegionIndex(await load_address_entries(conn))
        resolution = index.resolve(region)
        if not resolution.is_resolved:
            raise ToolError(
                "invalid_param", f"지역을 찾을 수 없습니다: {region!r}",
                candidates=[str(c) for c in resolution.candidates]
                or list(index.districts[:10]))
        matched = resolution.matched[0]
        sgg_nm, emd_nm = matched.sgg_nm, matched.emd_nm

    prpt_div_cds: tuple[str, ...] = ()
    if prpt_div:
        resolved = resolve_property_type(prpt_div)
        if not resolved.matched:
            raise ToolError("invalid_param", f"재산유형을 찾을 수 없습니다: {prpt_div!r}",
                            candidates=[str(c) for c in resolved.candidates][:10])
        prpt_div_cds = tuple(node.code for node in resolved.matched)

    picked = status or DEFAULT_STATUS
    if picked not in STATUS_CHOICES:
        raise ToolError("invalid_param", f"status 값이 올바르지 않습니다: {picked!r}",
                        candidates=list(STATUS_CHOICES))

    return ListingQuery(sgg_nm=sgg_nm, emd_nm=emd_nm, prpt_div_cds=prpt_div_cds,
                        statuses=STATUS_CHOICES[picked])


async def get_auction_stats(
    conn: psycopg.AsyncConnection[Any],
    *,
    group_by: str,
    region: str | None = None,
    prpt_div: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """조건에 맞는 물건의 분포를 집계한다.

    Args:
        conn: 열린 연결.
        group_by: `GROUP_BY_CHOICES` 중 하나. `win_rate` 는 낙찰가율 두 지표를 준다.
        region: 시군구·읍면동명.
        prpt_div: 재산유형명 또는 코드.
        status: `진행`(기본) | `종료추정` | `전체`.

    Returns:
        `buckets` · `n` · `query_echo` · `meta`. 낙찰가율이면 두 지표를 따로 담는다.

    Raises:
        ToolError: 축·조건이 올바르지 않을 때 `invalid_param`.
    """
    if group_by not in GROUP_BY_CHOICES:
        raise ToolError("invalid_param", f"집계 축이 올바르지 않습니다: {group_by!r}",
                        candidates=list(GROUP_BY_CHOICES))

    query = await _query(conn, region, prpt_div, status)
    echo = {"group_by": group_by, "region": region, "prpt_div": prpt_div,
            "status": status or DEFAULT_STATUS}

    if group_by == WIN_RATE_AXIS:
        stats = await aggregate_win_rates(conn, query=query)
        return ok_response(
            {
                "win_to_appraisal": _ratio(stats.win_to_appraisal),
                "win_to_min_bid": _ratio(stats.win_to_min_bid),
                "n": stats.n,
                "buckets": _ratio(stats.win_to_appraisal)["buckets"],
            },
            query_echo=echo, count=stats.n,
            meta_extra=dict(as_meta(stats)),
        )

    result = await aggregate(conn, group_by=group_by, query=query)
    extra: dict[str, Any] = {}
    if result.caveat:
        extra["caveat"] = result.caveat
        extra["prpt_div_breakdown"] = result.prpt_div_breakdown

    return ok_response(
        {
            "group_by": result.group_by,
            "buckets": [{"key": b.key, "label": b.label, "count": b.count}
                        for b in result.buckets],
            "n": result.n,
        },
        query_echo=echo, count=len(result.buckets), meta_extra=extra,
    )


def _ratio(stats: Any) -> dict[str, Any]:
    """비율 분포를 응답 형태로."""
    return {
        "metric": stats.metric,
        "buckets": [{"key": b.key, "label": b.label, "count": b.count}
                    for b in stats.buckets],
        "n": stats.n,
        "median": stats.median,
    }
