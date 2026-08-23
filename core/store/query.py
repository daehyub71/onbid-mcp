"""조회 쿼리 빌더 (F5.1·F6.7).

MCP 툴과 조회 API 가 함께 쓰는 필터를 SQL 로 바꾼다. **순수 함수다** — DB 없이 조건 조합을
검증할 수 있어야 조합 폭발을 감당할 수 있다.

세 가지를 지킨다.

- **값은 전부 파라미터 바인딩.** 문자열로 이어 붙이면 주입 통로가 된다.
- **빈 필터는 조건을 만들지 않는다.** 조건 하나를 잘못 붙이면 전체 조회가 조용히 0건이 된다.
- **정렬은 결정적이어야 한다.** 마감일만으로 정렬하면 동률에서 순서가 흔들려 커서
  페이지네이션이 행을 건너뛰거나 중복시킨다 (F6.8).

**offset 을 쓰지 않는다** (F6.8). 배치가 도는 동안 행이 tombstone 으로 바뀌거나 새로 들어오면
offset 기준이 밀려 행을 건너뛰거나 같은 행을 두 번 준다. 커서는 "마지막으로 본 행 다음" 을
가리키므로 그 사이 무슨 일이 있어도 어긋나지 않는다.

**정렬은 사실 값만** 허용한다 (§2.4 랭킹 금지). 점수 컬럼을 만들지 않으며 기본값은 마감 임박
순이다 — "추천순" 같은 기본 정렬은 판단 주체를 우리 쪽으로 옮기는 일이다.

**용도 확장은 여기서 하지 않는다.** 중분류로 그냥 걸면 0건이므로(실측 3,506건 대 0건, F6.12)
호출자가 `UsageIndex` 로 소분류까지 펼친 id 목록을 넘긴다 — 코드표는 DB 에 있고 이 모듈은
SQL 만 만든다.
"""

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from core.normalizer.status import AuctionStatus

TABLE: Final = "onbid_cltr"

#: 조회에 노출하는 컬럼. `raw_payload` 는 크고 조회에 쓰이지 않아 뺀다.
COLUMNS: Final = (
    "cltr_mng_no", "pbct_cdtn_no", "cltr_nm",
    "jibun_addr", "road_addr", "sd_nm", "sgg_nm", "emd_nm", "lat", "lng",
    "geocode_status", "geocode_level",
    "prpt_div_cd", "prpt_div_nm",
    "usg_lcls_nm", "usg_mcls_nm", "usg_scls_nm",
    "appraisal_amt", "min_bid_amt", "min_bid_amt_text", "min_bid_rate",
    "fail_cnt", "bid_start", "bid_end", "bid_date_tbd",
    "status", "pbct_stat_nm", "pvct_trgt_yn", "share_yn",
    "land_sqms", "bld_sqms", "org_nm", "onbid_url", "synced_at",
)

VALID_STATUSES: Final = frozenset(status.value for status in AuctionStatus)

#: 정렬 가능한 컬럼. **사실 값만** 둔다 — 점수·추천 컬럼은 만들지 않는다 (§2.4).
SORTABLE: Final = {
    "bid_end": "마감일",
    "min_bid_amt": "최저입찰가",
    "min_bid_rate": "최저가율",
    "fail_cnt": "유찰횟수",
}

#: 동률에서 순서가 흔들리면 커서가 행을 건너뛴다 — 복합키로 항상 갈라 준다.
KEY_ORDER: Final = ("cltr_mng_no", "pbct_cdtn_no")


@dataclass(frozen=True, slots=True)
class Sort:
    """정렬 조건.

    Attributes:
        field: `SORTABLE` 의 컬럼명.
        descending: 내림차순 여부.
    """

    field: str = "bid_end"
    descending: bool = False

    def validate(self) -> None:
        """허용된 컬럼인지 확인한다.

        Raises:
            ValueError: 화이트리스트에 없는 컬럼일 때. 임의 컬럼을 받으면 정렬을 통해
                SQL 을 조작할 수 있다.
        """
        if self.field not in SORTABLE:
            raise ValueError(
                f"정렬할 수 없는 항목: {self.field!r} (가능: {sorted(SORTABLE)})"
            )

    @property
    def direction(self) -> str:
        """SQL 방향 키워드."""
        return "desc" if self.descending else "asc"


DEFAULT_SORT: Final = Sort()
"""기본은 마감 임박 순이다 (F6.8)."""


@dataclass(frozen=True, slots=True)
class CursorState:
    """커서가 담고 있는 위치.

    Attributes:
        sort: 이 커서를 만든 정렬. **다른 정렬에 쓰면 거부한다.**
        value: 정렬 컬럼의 값. 일정 미정 행이면 None.
        key: 마지막으로 본 행의 복합키.
    """

    sort: Sort
    value: Any
    key: tuple[str, str]


def _portable(value: Any) -> Any:
    """JSON 으로 실어 보낼 수 있는 값으로 바꾼다.

    **`Decimal` 을 float 으로 바꾸지 않는다.** 최저가율은 `numeric` 이라 float 으로 왕복하면
    경계값에서 미세하게 어긋나 행을 건너뛰거나 중복시킬 수 있다. 문자열은 정확히 왕복하고,
    Postgres 가 비교 대상 컬럼의 타입으로 해석해 준다.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def encode_cursor(row: Mapping[str, Any], sort: Sort) -> str:
    """마지막 행의 위치를 **불투명한** 토큰으로 만든다.

    클라이언트가 파싱해 쓰기 시작하면 내부 정렬 방식을 바꿀 수 없게 된다.

    Args:
        row: 마지막으로 돌려준 행.
        sort: 이번 조회의 정렬.

    Returns:
        base64 토큰.
    """
    sort.validate()
    payload = {
        "s": sort.field,
        "d": int(sort.descending),
        "v": _portable(row.get(sort.field)),
        "k": [str(row[KEY_ORDER[0]]), str(row[KEY_ORDER[1]])],
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> CursorState:
    """토큰을 위치로 되돌린다.

    Args:
        token: `encode_cursor` 가 만든 토큰.

    Returns:
        커서 위치.

    Raises:
        ValueError: 토큰이 깨졌거나 형식이 맞지 않을 때.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        sort = Sort(field=str(payload["s"]), descending=bool(payload["d"]))
        key = (str(payload["k"][0]), str(payload["k"][1]))
    except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
        raise ValueError(f"커서를 읽을 수 없다: {exc}") from exc

    sort.validate()
    return CursorState(sort=sort, value=payload["v"], key=key)


def _order_by(sort: Sort) -> str:
    """정렬절. 일정 미정(null)은 항상 맨 뒤로 보낸다 — 방향이 바뀌어도 마찬가지다."""
    keys = ", ".join(f"{column} {sort.direction}" for column in KEY_ORDER)
    return f"{sort.field} {sort.direction} nulls last, {keys}"


def _cursor_clause(state: CursorState, sort: Sort) -> tuple[str, dict[str, Any]]:
    """"이 행 다음" 을 뜻하는 조건절.

    **null 을 튜플 비교에 섞지 않는다.** 섞으면 결과가 null 이 되어 일정 미정 행이 통째로
    사라진다. 값이 있는 구간과 null 꼬리를 나눠 쓴다.

    Raises:
        ValueError: 커서의 정렬과 요청한 정렬이 다를 때.
    """
    if state.sort != sort:
        raise ValueError(
            "정렬이 바뀌었다 — 다른 정렬로 만든 커서는 쓸 수 없다. 첫 페이지부터 다시 조회한다"
        )

    field, after = sort.field, ">" if not sort.descending else "<"
    params: dict[str, Any] = {
        "cursor_key_mng": state.key[0],
        "cursor_key_cdtn": state.key[1],
    }
    tie = (
        f"(cltr_mng_no, pbct_cdtn_no) {after} "
        "(%(cursor_key_mng)s, %(cursor_key_cdtn)s)"
    )

    if state.value is None:
        # 이미 null 꼬리에 들어왔다. 값이 있는 행으로 되돌아가면 안 된다.
        return f"({field} is null and {tie})", params

    params["cursor_value"] = state.value
    return (
        f"(({field} {after} %(cursor_value)s)"
        f" or ({field} = %(cursor_value)s and {tie})"
        f" or {field} is null)",
        params,
    )


@dataclass(frozen=True, slots=True)
class ListingQuery:
    """물건 조회 조건. 모든 항목이 선택이며, 없으면 좁히지 않는다.

    Attributes:
        sd_nm: 시도명.
        sgg_nm: 시군구명.
        emd_nm: 읍면동명.
        usage_ids: 용도 카테고리 id. **호출자가 소분류까지 펼쳐서** 넘긴다 (F6.12).
        prpt_div_cds: 재산유형코드.
        pvct_trgt: 수의계약 가능 여부. `None` 은 무관, `False` 는 **불가만**이다.
        min_bid_amt_min: 최저입찰가 하한(원).
        min_bid_amt_max: 최저입찰가 상한(원).
        min_bid_rate_min: 최저가율 하한. 1.0 을 넘을 수 있다 (F4.5).
        min_bid_rate_max: 최저가율 상한.
        fail_cnt_min: 유찰횟수 하한.
        fail_cnt_max: 유찰횟수 상한.
        bid_end_from: 마감일 시작. **일정 미정 행은 `bid_end` 가 null 이라 빠진다.**
        bid_end_to: 마감일 종료.
        statuses: 상태. 허용값을 검증한다.
    """

    sd_nm: str | None = None
    sgg_nm: str | None = None
    emd_nm: str | None = None
    usage_ids: tuple[str, ...] = ()
    prpt_div_cds: tuple[str, ...] = ()
    pvct_trgt: bool | None = None
    min_bid_amt_min: int | None = None
    min_bid_amt_max: int | None = None
    min_bid_rate_min: float | None = None
    min_bid_rate_max: float | None = None
    fail_cnt_min: int | None = None
    fail_cnt_max: int | None = None
    bid_end_from: datetime | None = None
    bid_end_to: datetime | None = None
    statuses: tuple[str, ...] = ()


def _validate(query: ListingQuery) -> None:
    """조용히 0건을 만드는 입력을 미리 거른다.

    Raises:
        ValueError: 알 수 없는 상태값이 섞였을 때.
    """
    unknown = [s for s in query.statuses if s not in VALID_STATUSES]
    if unknown:
        raise ValueError(
            f"알 수 없는 상태: {unknown} (가능: {sorted(VALID_STATUSES)})"
        )


def build_where(query: ListingQuery) -> tuple[list[str], dict[str, Any]]:
    """조건절 목록과 파라미터를 만든다.

    Args:
        query: 조회 조건.

    Returns:
        ``(조건절 목록, 파라미터)``. 조건이 없으면 빈 목록이다.

    Raises:
        ValueError: 허용되지 않은 값이 섞였을 때.
    """
    _validate(query)

    clauses: list[str] = []
    params: dict[str, Any] = {}

    def equals(column: str, value: Any) -> None:
        if value is None:
            return
        clauses.append(f"{column} = %({column})s")
        params[column] = value

    def any_of(column: str, name: str, values: Sequence[str]) -> None:
        if not values:
            return
        clauses.append(f"{column} = any(%({name})s)")
        params[name] = list(values)

    def at_least(column: str, name: str, value: Any) -> None:
        if value is None:
            return
        clauses.append(f"{column} >= %({name})s")
        params[name] = value

    def at_most(column: str, name: str, value: Any) -> None:
        if value is None:
            return
        clauses.append(f"{column} <= %({name})s")
        params[name] = value

    equals("sd_nm", query.sd_nm)
    equals("sgg_nm", query.sgg_nm)
    equals("emd_nm", query.emd_nm)

    # 대·중·소 어느 단계에 맞아도 통과시킨다. 호출자가 이미 펼친 id 목록이다 (F6.12).
    if query.usage_ids:
        clauses.append(
            "(usg_lcls_id = any(%(usage_ids)s)"
            " or usg_mcls_id = any(%(usage_ids)s)"
            " or usg_scls_id = any(%(usage_ids)s))"
        )
        params["usage_ids"] = list(query.usage_ids)

    any_of("prpt_div_cd", "prpt_div_cds", query.prpt_div_cds)
    any_of("status", "statuses", query.statuses)

    # None(무관) 과 False(불가만) 를 구분해야 한다 — `equals` 가 None 만 건너뛴다.
    if query.pvct_trgt is not None:
        clauses.append("pvct_trgt_yn = %(pvct_trgt)s")
        params["pvct_trgt"] = query.pvct_trgt

    at_least("min_bid_amt", "min_bid_amt_min", query.min_bid_amt_min)
    at_most("min_bid_amt", "min_bid_amt_max", query.min_bid_amt_max)
    at_least("min_bid_rate", "min_bid_rate_min", query.min_bid_rate_min)
    at_most("min_bid_rate", "min_bid_rate_max", query.min_bid_rate_max)
    at_least("fail_cnt", "fail_cnt_min", query.fail_cnt_min)
    at_most("fail_cnt", "fail_cnt_max", query.fail_cnt_max)
    at_least("bid_end", "bid_end_from", query.bid_end_from)
    at_most("bid_end", "bid_end_to", query.bid_end_to)

    return clauses, params


def build_select(
    query: ListingQuery,
    *,
    limit: int,
    sort: Sort = DEFAULT_SORT,
    cursor: str | None = None,
    columns: Sequence[str] = COLUMNS,
) -> tuple[str, Mapping[str, Any]]:
    """조회 SQL 과 파라미터를 만든다.

    Args:
        query: 조회 조건.
        limit: 최대 건수. **바인딩**한다.
        sort: 정렬. 화이트리스트 밖이면 거부한다.
        cursor: 이어 볼 위치. `encode_cursor` 가 만든 토큰이다.
        columns: 선택할 컬럼.

    Returns:
        ``(sql, params)``.

    Raises:
        ValueError: 허용되지 않은 값이거나, 커서의 정렬이 요청과 다를 때.
    """
    sort.validate()
    clauses, params = build_where(query)

    if cursor is not None:
        clause, cursor_params = _cursor_clause(decode_cursor(cursor), sort)
        clauses.append(clause)
        params.update(cursor_params)

    params["limit"] = limit
    where = f"\n where {' and '.join(clauses)}" if clauses else ""
    return (
        f"select {', '.join(columns)}\n  from {TABLE}{where}\n"
        f" order by {_order_by(sort)}\n limit %(limit)s",
        params,
    )
