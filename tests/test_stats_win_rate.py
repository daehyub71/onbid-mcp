"""낙찰가율 집계 테스트 (`pytest -m db`, SPEC §8.3·D18·D20).

**이 통계는 편향돼 있고, 그 사실이 숫자보다 중요하다.**

우리가 보는 낙찰은 "낙찰됐으나 계약이 무산되어 **다시 공매에 나온**" 건뿐이다. 정상적으로
낙찰·계약이 끝난 물건은 목록 API 에 나오지 않아 표본에 아예 없다. 그래서 이 분포를 일반적인
낙찰가율로 읽으면 결론이 뒤집힌다.

그래서 caveat 은 **선택 항목이 아니다.** 호출자가 빠뜨릴 수 있으면 언젠가 빠진다 —
결과 타입이 항상 들고 다니게 만든다.

두 지표를 **분리**하는 것도 같은 이유다. 낙찰가÷감정가(통상적 낙찰가율)와 낙찰가÷그 회차
최저입찰가(경쟁 강도)는 값의 범위부터 다르다 — 섞으면 300% 짜리 숫자가 낙찰가율로 읽힌다.
"""

import pytest

from core.stats.win_rate import POPULATION, aggregate_win_rates
from core.store.query import ListingQuery
from tests.conftest import Conn

pytestmark = pytest.mark.db

SEOUL = ListingQuery(sd_nm="서울특별시")


# ── caveat 강제 (§8.3 필수 구현) ───────────────────────────────────────


async def test_caveat_is_always_present(conn: Conn) -> None:
    """호출자가 빠뜨릴 수 있으면 언젠가 빠진다."""
    result = await aggregate_win_rates(conn, query=SEOUL)

    assert result.caveat
    assert "재공매" in result.caveat


async def test_caveat_says_the_sample_is_incomplete(conn: Conn) -> None:
    """'정상 낙찰 건이 빠져 있다' 는 말이 없으면 편향을 알 수 없다."""
    result = await aggregate_win_rates(conn, query=SEOUL)

    assert "표본" in result.caveat or "빠져" in result.caveat


async def test_population_is_named(conn: Conn) -> None:
    result = await aggregate_win_rates(conn, query=SEOUL)
    assert result.population == POPULATION


async def test_caveat_survives_an_empty_sample(conn: Conn) -> None:
    """0건일 때야말로 해석 주의가 필요하다 — 빈 결과에서 caveat 이 빠지면 안 된다."""
    result = await aggregate_win_rates(
        conn, query=ListingQuery(sd_nm="없는시도"))

    assert result.n == 0
    assert result.caveat


# ── 두 지표 분리 ───────────────────────────────────────────────────────


async def test_two_metrics_are_reported_separately(conn: Conn) -> None:
    """섞으면 300% 짜리 경쟁 강도가 낙찰가율로 읽힌다."""
    result = await aggregate_win_rates(conn, query=SEOUL)

    assert result.win_to_appraisal.buckets
    assert result.win_to_min_bid.buckets


async def test_appraisal_ratio_stays_in_a_plausible_range(conn: Conn) -> None:
    """낙찰가÷감정가는 실측 3.4%~71.4% 였다. 100% 를 크게 넘으면 분모가 틀린 것이다."""
    result = await aggregate_win_rates(conn, query=SEOUL)

    assert result.win_to_appraisal.median is not None
    assert 0 < result.win_to_appraisal.median < 2.0


async def test_competition_ratio_is_at_least_one(conn: Conn) -> None:
    """낙찰가는 그 회차 최저입찰가 이상이다 — 미만이면 회차 짝짓기가 틀렸다."""
    result = await aggregate_win_rates(conn, query=SEOUL)

    assert result.win_to_min_bid.median is not None
    assert result.win_to_min_bid.median >= 1.0


# ── 집계 단위 (§8.3) ───────────────────────────────────────────────────


async def test_counts_events_not_properties(conn: Conn) -> None:
    """한 물건이 여러 번 낙찰·무산되면 여러 번 기여한다 — 그 사실을 드러낸다."""
    result = await aggregate_win_rates(conn, query=SEOUL)

    assert result.n >= result.property_count > 0


async def test_sample_size_matches_the_buckets(conn: Conn) -> None:
    result = await aggregate_win_rates(conn, query=SEOUL)

    assert sum(b.count for b in result.win_to_appraisal.buckets) == result.win_to_appraisal.n


# ── 필터 ───────────────────────────────────────────────────────────────


async def test_region_filter_narrows(conn: Conn) -> None:
    everything = await aggregate_win_rates(conn, query=SEOUL)
    one_gu = await aggregate_win_rates(
        conn, query=ListingQuery(sd_nm="서울특별시", sgg_nm="강서구"))

    assert one_gu.n <= everything.n


async def test_rows_without_appraisal_are_excluded_not_zeroed(conn: Conn) -> None:
    """감정가가 없으면 비율을 낼 수 없다. 0으로 채우면 분포가 왼쪽으로 무너진다."""
    result = await aggregate_win_rates(conn, query=SEOUL)

    assert all(b.key != "0-9" or b.count < result.win_to_appraisal.n
               for b in result.win_to_appraisal.buckets)


# ── 사건 중복 (실데이터가 잡아낸 결함) ────────────────────────────────


async def test_the_same_event_is_counted_once(conn: Conn) -> None:
    """한 물건에 공매조건번호가 여러 개 붙고, **같은 낙찰 사건이 조건마다 저장**된다.

    입찰정보 API 가 (물건, 조건) 으로 물어도 그 물건의 이력을 통째로 돌려주기 때문이다.
    실측에서 낙찰 행 62건이 실제로는 사건 13건이었다 — 그대로 세면 평균 4.8배 부풀고,
    조건번호가 많은 물건이 분포를 좌우한다.
    """
    async with conn.cursor() as cur:
        await cur.execute("""
            select count(distinct (r.cltr_mng_no, r.opbd_dt, r.pbct_nsq))
              from onbid_cltr_bid_round r
              join onbid_cltr c on c.cltr_mng_no = r.cltr_mng_no
                               and c.pbct_cdtn_no = r.pbct_cdtn_no
             where r.winning_amt is not null and c.appraisal_amt > 0
               and c.sd_nm = '서울특별시'""")
        found = await cur.fetchone()
    assert found is not None

    result = await aggregate_win_rates(conn, query=SEOUL)

    assert result.n == found[0]


async def test_property_count_is_distinct_properties(conn: Conn) -> None:
    """물건 수는 **물건관리번호** 기준이다 — 조건번호까지 세면 부풀어 중복을 못 드러낸다."""
    async with conn.cursor() as cur:
        await cur.execute("""
            select count(distinct r.cltr_mng_no)
              from onbid_cltr_bid_round r
              join onbid_cltr c on c.cltr_mng_no = r.cltr_mng_no
                               and c.pbct_cdtn_no = r.pbct_cdtn_no
             where r.winning_amt is not null and c.appraisal_amt > 0
               and c.sd_nm = '서울특별시'""")
        found = await cur.fetchone()
    assert found is not None

    result = await aggregate_win_rates(conn, query=SEOUL)

    assert result.property_count == found[0]
