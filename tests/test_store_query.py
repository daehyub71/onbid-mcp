"""조회 쿼리 빌더 테스트 (F5.1·F6.7).

**생성된 SQL 과 파라미터를 직접 본다** (PLAN §5.1). 네트워크도 DB도 없이 돌아야 조건 조합을
빠르게 검증할 수 있다.

여기서 지키는 규약 셋:

- **값은 전부 파라미터 바인딩**한다. 문자열로 이어 붙이면 주입 통로가 된다.
- **빈 필터는 조건을 만들지 않는다.** 조건을 하나라도 잘못 붙이면 전체 조회가 0건이 된다.
- **정렬은 결정적**이어야 한다. 커서 페이지네이션이 그 위에 서기 때문이다 (F6.8).
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from core.store.query import ListingQuery, build_select, build_where


def sql_of(query: ListingQuery) -> str:
    return build_select(query, limit=20)[0]


def where_of(query: ListingQuery) -> str:
    """조건절만 본다 — SELECT 컬럼 목록에도 같은 이름이 들어 있다."""
    return " and ".join(build_where(query)[0])


# ── 조건 없음 ──────────────────────────────────────────────────────────


def test_empty_query_selects_everything() -> None:
    """조건이 없으면 좁히지 않는다 — 잘못 붙은 조건 하나가 전체를 0건으로 만든다."""
    clauses, where_params = build_where(ListingQuery())
    _sql, params = build_select(ListingQuery(), limit=20)

    assert clauses == []
    assert where_params == {}
    assert params["limit"] == 20


def test_limit_is_bound_not_interpolated() -> None:
    _sql, params = build_select(ListingQuery(), limit=50)
    assert params["limit"] == 50


# ── 지역 ───────────────────────────────────────────────────────────────


def test_district_filter() -> None:
    query = ListingQuery(sd_nm="서울특별시", sgg_nm="강남구")

    sql, params = build_select(query, limit=20)

    assert "sd_nm = %(sd_nm)s" in sql
    assert "sgg_nm = %(sgg_nm)s" in sql
    assert params["sgg_nm"] == "강남구"


def test_dong_filter() -> None:
    assert "emd_nm = %(emd_nm)s" in sql_of(ListingQuery(emd_nm="개포동"))


# ── 용도 (F6.12) ───────────────────────────────────────────────────────


def test_usage_matches_any_of_the_three_levels() -> None:
    """중분류로 그냥 걸면 0건이다 — 소분류까지 훑어야 한다 (실측 3,506건 대 0건)."""
    sql = sql_of(ListingQuery(usage_ids=("10300", "10301")))

    assert "usg_lcls_id" in sql and "usg_mcls_id" in sql and "usg_scls_id" in sql


def test_usage_ids_are_bound_as_one_array() -> None:
    """id 마다 조건을 만들면 SQL 이 길이에 따라 달라져 계획 재사용이 깨진다."""
    _sql, params = build_select(ListingQuery(usage_ids=("10300", "10301")), limit=20)

    assert params["usage_ids"] == ["10300", "10301"]


def test_empty_usage_tuple_adds_no_condition() -> None:
    assert "usg_lcls_id" not in where_of(ListingQuery(usage_ids=()))


# ── 재산유형 · 수의계약 ────────────────────────────────────────────────


def test_property_type_filter() -> None:
    _sql, params = build_select(ListingQuery(prpt_div_cds=("0007",)), limit=20)
    assert params["prpt_div_cds"] == ["0007"]


def test_private_contract_true() -> None:
    """수의계약 가능 여부로 취득 방법이 갈린다."""
    sql, params = build_select(ListingQuery(pvct_trgt=True), limit=20)

    assert "pvct_trgt_yn = %(pvct_trgt)s" in sql
    assert params["pvct_trgt"] is True


def test_private_contract_false_is_a_real_filter() -> None:
    """`False` 는 '무관' 이 아니라 '수의계약 불가만' 이다 — None 과 구분해야 한다."""
    sql, params = build_select(ListingQuery(pvct_trgt=False), limit=20)

    assert "pvct_trgt_yn = %(pvct_trgt)s" in sql
    assert params["pvct_trgt"] is False


def test_private_contract_none_adds_no_condition() -> None:
    assert "pvct_trgt_yn" not in where_of(ListingQuery(pvct_trgt=None))


# ── 가격 · 최저가율 · 유찰횟수 ─────────────────────────────────────────


def test_price_range() -> None:
    sql, params = build_select(
        ListingQuery(min_bid_amt_min=100_000_000, min_bid_amt_max=500_000_000), limit=20)

    assert "min_bid_amt >= %(min_bid_amt_min)s" in sql
    assert "min_bid_amt <= %(min_bid_amt_max)s" in sql
    assert params["min_bid_amt_max"] == 500_000_000


def test_only_lower_bound() -> None:
    where = where_of(ListingQuery(min_bid_amt_min=100_000_000))
    assert "min_bid_amt >=" in where and "min_bid_amt <=" not in where


def test_rate_range_allows_above_one() -> None:
    """최저가율은 100% 를 넘는다 — 실측 9.8%, 최대 150.2% (F4.5)."""
    _sql, params = build_select(ListingQuery(min_bid_rate_max=1.5), limit=20)
    assert params["min_bid_rate_max"] == 1.5


def test_fail_count_range() -> None:
    sql = sql_of(ListingQuery(fail_cnt_min=3))
    assert "fail_cnt >= %(fail_cnt_min)s" in sql


# ── 마감일 · 상태 ──────────────────────────────────────────────────────


def test_deadline_range() -> None:
    sql, params = build_select(
        ListingQuery(bid_end_from=datetime(2026, 8, 1, tzinfo=UTC)), limit=20)

    assert "bid_end >= %(bid_end_from)s" in sql
    value: Any = params["bid_end_from"]
    assert value.year == 2026


def test_deadline_filter_leaves_undated_rows_to_null_semantics() -> None:
    """일정 미정(`2999`)은 `bid_end` 가 null 이라 범위 조건에서 자연히 빠진다 — 의도된 동작이다."""
    where = where_of(ListingQuery(bid_end_to=datetime(2026, 9, 1, tzinfo=UTC)))

    assert "bid_date_tbd" not in where


def test_status_filter() -> None:
    _sql, params = build_select(ListingQuery(statuses=("진행", "유찰")), limit=20)
    assert params["statuses"] == ["진행", "유찰"]


def test_status_rejects_unknown_value() -> None:
    """오타가 조용히 0건을 만들면 '물건이 없다' 로 오해한다."""
    with pytest.raises(ValueError, match="상태"):
        build_select(ListingQuery(statuses=("진행중",)), limit=20)


# ── 조합 · 정렬 ────────────────────────────────────────────────────────


def test_conditions_are_combined_with_and() -> None:
    query = ListingQuery(sgg_nm="강남구", fail_cnt_min=2, pvct_trgt=False)

    sql, params = build_select(query, limit=20)

    assert sql.lower().count(" and ") >= 2
    assert {"sgg_nm", "fail_cnt_min", "pvct_trgt"} <= set(params)


def test_default_order_is_deadline_then_key() -> None:
    """마감 임박 순이 기본이고(F6.8), 동률은 키로 갈라 **결정적**으로 만든다 — 커서의 전제다."""
    sql = sql_of(ListingQuery())

    order = sql.lower().split("order by")[1]
    assert "bid_end" in order
    assert "cltr_mng_no" in order and "pbct_cdtn_no" in order


def test_no_value_is_interpolated_into_sql() -> None:
    """값이 SQL 문자열에 박히면 주입 통로가 된다."""
    query = ListingQuery(sgg_nm="'; drop table onbid_cltr; --", fail_cnt_min=3)

    sql, params = build_select(query, limit=20)

    assert "drop table" not in sql
    assert params["sgg_nm"] == "'; drop table onbid_cltr; --"
