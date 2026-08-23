"""분포 집계 테스트 (`pytest -m db`, SPEC §8.3).

집계는 **개별 물건이 아니라 분포**를 준다. 그래서 조용히 틀려도 눈에 안 띈다 — 어떤 행이
어느 칸에도 안 들어가면 합계만 줄어들 뿐 오류가 나지 않는다.

세 가지를 여기서 못박는다.

1. **버킷 합계 = 전체 건수.** 최저가율이 없는 행(감정가 결측 4.9%)을 조용히 버리지 않는다.
2. **100% 초과 구간을 따로 둔다.** 100%에 합치거나 버리면 전체의 9.8%가 사라진다 (F4.5).
3. **재산유형 혼재 경고를 강제한다.** 저감 체계가 다른 유형이 한 분포에 섞이면 LLM 이 단일
   모집단으로 해석한다 (§8.3 필수 구현).
"""

import pytest

from core.stats.distribution import AXES, aggregate
from core.store.query import ListingQuery
from tests.conftest import Conn

pytestmark = pytest.mark.db

SEOUL = ListingQuery(sd_nm="서울특별시")


# ── 축 ─────────────────────────────────────────────────────────────────


def test_axes_match_the_tool_contract() -> None:
    """§8.3 의 `group_by` enum 과 같아야 한다 — 어긋나면 툴이 부를 수 없는 축이 생긴다."""
    assert set(AXES) == {
        "min_bid_rate_bucket", "fail_cnt", "usage", "region", "prpt_div", "pvct_trgt"
    }


async def test_unknown_axis_is_rejected(conn: Conn) -> None:
    with pytest.raises(ValueError, match="집계 축"):
        await aggregate(conn, group_by="price_score", query=SEOUL)


@pytest.mark.parametrize("axis", sorted(AXES))
async def test_every_axis_runs(conn: Conn, axis: str) -> None:
    """컬럼명 오타는 실행해야 드러난다."""
    result = await aggregate(conn, group_by=axis, query=SEOUL)
    assert result.n > 0


# ── 합계 보존 (핵심) ───────────────────────────────────────────────────


@pytest.mark.parametrize("axis", sorted(AXES))
async def test_buckets_sum_to_the_total(conn: Conn, axis: str) -> None:
    """어떤 행도 칸 밖으로 새지 않는다 — 새면 합계만 줄고 오류는 안 난다."""
    result = await aggregate(conn, group_by=axis, query=SEOUL)

    assert sum(b.count for b in result.buckets) == result.n


async def test_rows_without_rate_get_their_own_bucket(conn: Conn) -> None:
    """감정가가 없어 최저가율을 못 낸 행(실측 4.9%)을 버리지 않는다."""
    result = await aggregate(conn, group_by="min_bid_rate_bucket", query=SEOUL)

    labels = [b.label for b in result.buckets]
    assert any("미산출" in label for label in labels)


async def test_rate_buckets_keep_above_one_separate(conn: Conn) -> None:
    """100% 초과가 실측 9.8% 다. 합치거나 버리면 그만큼 사라진다."""
    result = await aggregate(conn, group_by="min_bid_rate_bucket", query=SEOUL)

    over = [b for b in result.buckets if b.key == "100+"]
    assert over and over[0].count > 0


async def test_rate_buckets_are_ordered(conn: Conn) -> None:
    """분포는 순서대로 읽힌다 — 뒤섞이면 사람이 해석할 수 없다."""
    result = await aggregate(conn, group_by="min_bid_rate_bucket", query=SEOUL)

    keys = [b.key for b in result.buckets if b.key != "unknown"]
    assert keys == sorted(keys, key=lambda k: int(k.split("-")[0].rstrip("+")))


# ── 필터 결합 ──────────────────────────────────────────────────────────


async def test_filters_narrow_the_population(conn: Conn) -> None:
    everything = await aggregate(conn, group_by="region", query=SEOUL)
    gangnam = await aggregate(conn, group_by="region",
                              query=ListingQuery(sd_nm="서울특별시", sgg_nm="강남구"))

    assert 0 < gangnam.n < everything.n
    assert len(gangnam.buckets) == 1


async def test_status_filter_applies(conn: Conn) -> None:
    result = await aggregate(conn, group_by="fail_cnt",
                             query=ListingQuery(statuses=("진행",)))
    assert result.n > 0


# ── 재산유형 혼재 경고 (§8.3 필수) ─────────────────────────────────────


async def test_mixed_property_types_are_flagged(conn: Conn) -> None:
    """저감 체계가 다른 유형이 섞이면 LLM 이 단일 모집단으로 해석한다."""
    result = await aggregate(conn, group_by="min_bid_rate_bucket", query=SEOUL)

    assert result.caveat is not None
    assert "재산유형" in result.caveat
    assert result.prpt_div_breakdown


async def test_fail_count_axis_is_also_flagged(conn: Conn) -> None:
    result = await aggregate(conn, group_by="fail_cnt", query=SEOUL)
    assert result.caveat is not None


async def test_filtered_property_type_drops_the_warning(conn: Conn) -> None:
    """유형을 하나로 좁혔으면 섞이지 않았으므로 경고할 이유가 없다."""
    result = await aggregate(conn, group_by="min_bid_rate_bucket",
                             query=ListingQuery(sd_nm="서울특별시", prpt_div_cds=("0007",)))

    assert result.caveat is None


async def test_other_axes_are_not_flagged(conn: Conn) -> None:
    """지역·용도 분포는 유형 혼재가 해석을 뒤집지 않는다."""
    result = await aggregate(conn, group_by="region", query=SEOUL)
    assert result.caveat is None


async def test_breakdown_sums_to_the_total(conn: Conn) -> None:
    result = await aggregate(conn, group_by="fail_cnt", query=SEOUL)

    assert result.prpt_div_breakdown is not None
    assert sum(result.prpt_div_breakdown.values()) == result.n


# ── 개별 식별정보 비노출 (§8.3) ────────────────────────────────────────


async def test_result_carries_only_aggregates(conn: Conn) -> None:
    """집계는 집계값만 준다 — 물건 식별정보를 섞으면 조회형의 경계가 흐려진다."""
    result = await aggregate(conn, group_by="region", query=SEOUL)

    import dataclasses

    for bucket in result.buckets:
        assert {f.name for f in dataclasses.fields(bucket)} == {"key", "label", "count"}
