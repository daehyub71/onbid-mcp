"""회차 이력 적재 (F1.7·§7·§8.3).

`prcnBidClgList` 는 회차 이력을 **통째로** 준다. 유찰 물건은 첫 배치부터 과거 전체를
확보한다 — 자체 diff 누적을 몇 달 기다릴 필요가 없다.

**`pbct_nsq` 는 유일하지 않다.** 한 물건의 이력에 여러 공매 사건이 섞여 있어 회차 번호가
사건마다 1부터 다시 매겨진다 (실측 25건 표본에서 184건 충돌). 개찰일시를 키에 넣지 않으면
서로를 덮어써 이력이 남지 않는다.

**회차 이력에는 `pbctStatCd` 가 없다.** 상태는 이름(`pbctStatNm`)으로 파생한다 (§7.1).
낙찰 회차의 `scfbAmt` 가 낙찰가율 통계의 분자가 된다 (§8.3).
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

import psycopg

from core.normalizer.amounts import parse_amount
from core.normalizer.datetimes import parse_datetime
from core.normalizer.status import status_from_name
from core.onbid.bidinfo import BidDetail, BidTarget
from core.onbid.parser import as_int, as_str

logger = logging.getLogger(__name__)

TABLE: Final = "onbid_cltr_bid_round"

KEY_COLUMNS: Final = ("cltr_mng_no", "pbct_cdtn_no", "opbd_dt", "pbct_nsq")

COLUMNS: Final = (
    *KEY_COLUMNS, "pbct_sn", "result_nm", "status",
    "min_bid_amt", "min_bid_amt_text", "winning_amt",
)

DEFAULT_CHUNK_SIZE: Final = 500

_UPSERT: Final = (
    f"insert into {TABLE} ({', '.join(COLUMNS)}, synced_at) values ("
    + ", ".join(f"%({column})s" for column in COLUMNS)
    + ", now()) "
    f"on conflict ({', '.join(KEY_COLUMNS)}) do update set "
    + ", ".join(
        f"{column} = excluded.{column}" for column in COLUMNS if column not in KEY_COLUMNS
    )
    + ", synced_at = now()"
)


def to_round_rows(detail: BidDetail) -> list[dict[str, Any]]:
    """입찰정보 한 건의 회차 이력을 DB 행으로 바꾼다.

    개찰일시나 회차가 없는 행은 **버린다** — 둘 다 PK 라 적재할 수 없다.

    Args:
        detail: `collect_bid_details` 가 수집한 입찰정보.

    Returns:
        `upsert_bid_rounds` 에 넣을 행들. 순서는 응답 그대로다 (정렬돼 있지 않다).
    """
    mng, cdtn = detail.key
    rows: list[dict[str, Any]] = []

    for entry in detail.rounds:
        opbd = parse_datetime(entry.get("cltrOpbdDt"))
        sequence = as_str(entry.get("pbctNsq"))
        if opbd.value is None or not sequence:
            continue

        amount = parse_amount(entry.get("lowstBidPrcIndctCont"))
        result_nm = as_str(entry.get("pbctStatNm"))
        status = status_from_name(result_nm)
        rows.append({
            "cltr_mng_no": mng,
            "pbct_cdtn_no": cdtn,
            "opbd_dt": opbd.value,
            "pbct_nsq": sequence,
            "pbct_sn": as_str(entry.get("pbctsn")),
            "result_nm": result_nm,
            "status": status.value if status else None,
            "min_bid_amt": amount.value,
            "min_bid_amt_text": amount.text,
            # 유찰 회차는 null 이다. 0 으로 채우면 낙찰가율 표본이 오염된다.
            "winning_amt": as_int(entry.get("scfbAmt")),
        })

    return rows


async def upsert_bid_rounds(
    conn: psycopg.AsyncConnection[Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """회차 이력을 배치로 적재한다.

    낙찰가는 나중에 붙으므로 기존 행도 갱신한다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        rows: `to_round_rows` 가 만든 행들.
        chunk_size: 한 번에 보낼 행 수.

    Returns:
        처리한 행 수.
    """
    if not rows:
        return 0

    processed = 0
    async with conn.cursor() as cur:
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            await cur.executemany(_UPSERT, chunk)
            processed += len(chunk)

    logger.info("회차 이력 적재: %d건", processed)
    return processed


# ── 대상 선별·시도 기록 (F1.11·F1.16) ──────────────────────────────────

_SELECT_TARGETS: Final = """
    select cltr_mng_no, pbct_cdtn_no
      from onbid_cltr
     where fail_cnt >= %(min_fail_count)s
       and pvct_trgt_yn is not true
     order by bid_round_synced_at asc nulls first, fail_cnt desc, bid_end asc nulls last
     limit %(limit)s
"""

_MARK_ATTEMPT: Final = """
    update onbid_cltr
       set bid_round_synced_at = %(attempted_at)s
     where cltr_mng_no = %(cltr_mng_no)s
       and pbct_cdtn_no = %(pbct_cdtn_no)s
"""


async def select_round_targets(
    conn: psycopg.AsyncConnection[Any],
    *,
    limit: int,
    min_fail_count: int = 1,
) -> list[BidTarget]:
    """입찰정보를 조회할 물건을 예산만큼 고른다 (F1.11·F1.16).

    두 부류를 제외한다 — 둘 다 ``03 NODATA_ERROR`` 가 돌아와 쿼터만 태운다.

    - **유찰 0회**: ``prcnBidClgList`` 가 비어 있다 (서울 물건의 68%).
    - **수의계약가능**: 입찰이 아니라 입찰정보가 없다 (실측 18/18건 ``03``).

    정렬은 **마지막 시도가 오래된 순**이다. 재개 토큰을 들고 다니지 않아도 며칠에 걸쳐
    한 바퀴를 돈다. 새로 등장한 물건은 null 이라 자연히 맨 앞이다.

    Args:
        conn: 열린 연결.
        limit: 이번 회차의 호출 예산.
        min_fail_count: 최소 유찰횟수.

    Returns:
        우선순위대로 정렬된 대상.
    """
    async with conn.cursor() as cur:
        await cur.execute(_SELECT_TARGETS, {"limit": limit, "min_fail_count": min_fail_count})
        found = await cur.fetchall()

    return [BidTarget(cltr_mng_no=str(row[0]), pbct_cdtn_no=str(row[1])) for row in found]


async def mark_round_attempts(
    conn: psycopg.AsyncConnection[Any],
    targets: Sequence[BidTarget],
    *,
    attempted_at: datetime,
) -> int:
    """시도한 물건에 시각을 남긴다 (F1.16).

    **성공·이력없음·실패를 가리지 않고 기록한다.** 성공만 남기면 이력이 없는 물건을 매일
    다시 호출해 예산을 태우고, 실패를 빼면 고장난 한 건이 매일 예산을 선점한다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        targets: 이번 회차에 실제로 호출한 대상.
        attempted_at: 시도 시각.

    Returns:
        기록한 행 수.
    """
    if not targets:
        return 0

    rows = [
        {"cltr_mng_no": t.cltr_mng_no, "pbct_cdtn_no": t.pbct_cdtn_no,
         "attempted_at": attempted_at}
        for t in targets
    ]
    async with conn.cursor() as cur:
        await cur.executemany(_MARK_ATTEMPT, rows)

    return len(rows)
