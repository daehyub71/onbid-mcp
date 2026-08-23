"""커서 페이지네이션·정렬 테스트 (F6.8·§2.4).

**offset 을 쓰지 않는다.** 배치가 도는 동안 행이 tombstone 으로 바뀌거나 새로 들어오면
offset 기준이 밀려 **행을 건너뛰거나 같은 행을 두 번** 준다. 커서는 "마지막으로 본 행 다음"
을 가리키므로 그 사이 무슨 일이 있어도 어긋나지 않는다.

두 가지 함정을 여기서 막는다.

1. **일정 미정 행** — `bid_end` 가 null 이라 맨 뒤에 온다. 튜플 비교에 null 이 섞이면 결과가
   null 이 되어 그 행들이 통째로 사라진다.
2. **정렬이 바뀐 커서** — 가격순 커서를 마감일순 조회에 쓰면 조용히 엉뚱한 구간이 나온다.
   오류로 거부해야 한다.
"""

from datetime import UTC, datetime

import pytest

from core.store.query import (
    DEFAULT_SORT,
    SORTABLE,
    ListingQuery,
    Sort,
    build_select,
    decode_cursor,
    encode_cursor,
)

ROW = {"bid_end": datetime(2026, 9, 1, 16, 0, tzinfo=UTC),
       "cltr_mng_no": "2026-0001-001", "pbct_cdtn_no": "77"}


# ── 정렬 화이트리스트 (F6.8·§2.4) ──────────────────────────────────────


def test_default_sort_is_deadline_ascending() -> None:
    """마감 임박 순이 기본이다. 점수·추천 순 같은 기본값은 §2.4 위반이다."""
    assert DEFAULT_SORT.field == "bid_end"
    assert DEFAULT_SORT.descending is False


def test_sortable_fields_are_factual_only() -> None:
    """정렬 대상은 **사실 값**뿐이다 — 점수 컬럼을 만들지 않는다 (§2.4 랭킹 금지)."""
    assert set(SORTABLE) == {"bid_end", "min_bid_amt", "min_bid_rate", "fail_cnt"}


def test_unknown_sort_field_is_rejected() -> None:
    """임의 컬럼을 받으면 정렬을 통해 SQL 을 조작할 수 있다."""
    with pytest.raises(ValueError, match="정렬"):
        build_select(ListingQuery(), limit=10, sort=Sort("raw_payload"))


def test_sort_direction_is_applied() -> None:
    sql, _ = build_select(ListingQuery(), limit=10, sort=Sort("min_bid_amt", descending=True))

    order = sql.lower().split("order by")[1]
    assert "min_bid_amt desc" in order


def test_key_columns_always_break_ties() -> None:
    """동률에서 순서가 흔들리면 커서가 행을 건너뛴다."""
    for field in SORTABLE:
        sql, _ = build_select(ListingQuery(), limit=10, sort=Sort(field))
        order = sql.lower().split("order by")[1]
        assert "cltr_mng_no" in order and "pbct_cdtn_no" in order


# ── 커서 인코딩 ────────────────────────────────────────────────────────


def test_cursor_is_opaque() -> None:
    """클라이언트가 파싱해 쓰기 시작하면 내부 정렬을 못 바꾼다."""
    token = encode_cursor(ROW, DEFAULT_SORT)

    assert "bid_end" not in token
    assert "2026-0001-001" not in token


def test_cursor_round_trips() -> None:
    state = decode_cursor(encode_cursor(ROW, DEFAULT_SORT))

    assert state.key == ("2026-0001-001", "77")
    assert state.sort.field == "bid_end"


def test_cursor_carries_null_sort_value() -> None:
    """일정 미정 행에서 끊긴 커서도 이어져야 한다."""
    state = decode_cursor(encode_cursor({**ROW, "bid_end": None}, DEFAULT_SORT))

    assert state.value is None


def test_garbage_cursor_is_rejected() -> None:
    with pytest.raises(ValueError, match="커서"):
        decode_cursor("not-a-real-cursor")


def test_truncated_cursor_is_rejected() -> None:
    token = encode_cursor(ROW, DEFAULT_SORT)
    with pytest.raises(ValueError, match="커서"):
        decode_cursor(token[:-5])


# ── 정렬 불일치 (핵심 함정) ────────────────────────────────────────────


def test_cursor_from_another_sort_is_rejected() -> None:
    """가격순 커서를 마감일순 조회에 쓰면 **조용히** 엉뚱한 구간이 나온다."""
    token = encode_cursor({**ROW, "min_bid_amt": 100}, Sort("min_bid_amt"))

    with pytest.raises(ValueError, match="정렬"):
        build_select(ListingQuery(), limit=10, sort=DEFAULT_SORT, cursor=token)


def test_cursor_from_another_direction_is_rejected() -> None:
    token = encode_cursor(ROW, Sort("bid_end", descending=True))

    with pytest.raises(ValueError, match="정렬"):
        build_select(ListingQuery(), limit=10, sort=Sort("bid_end"), cursor=token)


def test_matching_sort_is_accepted() -> None:
    token = encode_cursor(ROW, DEFAULT_SORT)

    sql, params = build_select(ListingQuery(), limit=10, sort=DEFAULT_SORT, cursor=token)

    assert "cursor_key_mng" in params
    assert "offset" not in sql.lower()


# ── 커서 조건 (null 처리) ──────────────────────────────────────────────


def test_cursor_with_value_lets_null_rows_through() -> None:
    """일정 미정 행은 맨 뒤에 온다 — 값이 있는 커서 뒤에는 아직 남아 있어야 한다."""
    sql, _ = build_select(ListingQuery(), limit=10, sort=DEFAULT_SORT,
                          cursor=encode_cursor(ROW, DEFAULT_SORT))

    assert "bid_end is null" in sql.lower()


def test_cursor_at_null_stays_in_the_null_tail() -> None:
    """이미 꼬리에 들어왔으면 값이 있는 행으로 되돌아가면 안 된다."""
    token = encode_cursor({**ROW, "bid_end": None}, DEFAULT_SORT)

    sql, _ = build_select(ListingQuery(), limit=10, sort=DEFAULT_SORT, cursor=token)
    condition = sql.lower().split("where")[1].split("order by")[0]

    assert "bid_end is null" in condition
    assert "bid_end >" not in condition


def test_cursor_combines_with_filters() -> None:
    sql, params = build_select(ListingQuery(sgg_nm="강남구"), limit=10,
                               sort=DEFAULT_SORT, cursor=encode_cursor(ROW, DEFAULT_SORT))

    assert params["sgg_nm"] == "강남구"
    assert "cursor_key_mng" in params


def test_no_offset_anywhere() -> None:
    """offset 은 배치가 도는 동안 행을 건너뛴다 — 쓰지 않는다."""
    sql, _ = build_select(ListingQuery(), limit=10)
    assert "offset" not in sql.lower()


def test_decimal_sort_value_survives_exactly() -> None:
    """최저가율은 `numeric` 이다. float 으로 왕복하면 경계값에서 행이 새거나 겹친다."""
    from decimal import Decimal

    sort = Sort("min_bid_rate")
    row = {**ROW, "min_bid_rate": Decimal("0.58612")}

    state = decode_cursor(encode_cursor(row, sort))

    assert state.value == "0.58612"
