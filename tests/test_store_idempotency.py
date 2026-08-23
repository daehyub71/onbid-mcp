"""배치 멱등성 테스트 (`pytest -m db`, AC2).

**같은 배치를 두 번 돌려도 DB 상태가 같아야 한다.** 이 성질이 없으면 재개(N2.2)를 쓸 수
없다 — 중단된 배치를 다시 돌릴 때마다 이력이 부풀고 회차가 중복된다.

여기서는 모듈을 하나씩이 아니라 **배치가 실제로 하는 순서 그대로** 돌린다:
`start_run` → 이력+적재 → 회차 → 코드표 → tombstone → `finish_run`.
단위 테스트가 각자 통과해도 순서를 틀리면 멱등성은 깨진다.

실응답 fixture 를 쓰되 키에 접두어를 붙여 실데이터와 섞이지 않게 한다. 전체는 롤백된다.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from core.onbid.bidinfo import BidDetail, BidTarget
from core.onbid.collector import CollectedItem
from core.store.batch_run import BatchCounts, finish_run, latest_resume_token, start_run
from core.store.bid_round import to_round_rows, upsert_bid_rounds
from core.store.codes import upsert_address_map, upsert_usage_codes
from core.store.history import upsert_with_history
from core.store.mapping import to_cltr_row
from core.store.tombstone import TombstoneScope, mark_tombstones
from tests.conftest import Conn, load_fixture

pytestmark = pytest.mark.db

PREFIX = "T-AC2-"
SCOPE = TombstoneScope(sd_nm="서울특별시")

#: 적재 시각은 배치마다 당연히 움직인다 — 멱등성 비교에서 뺀다.
VOLATILE = ("last_seen_at", "synced_at")


def sample_items(limit: int = 40) -> list[CollectedItem]:
    """실응답에서 표본을 뜨되 키를 테스트용으로 바꾼다."""
    raw = load_fixture("list_many")["body"]["items"]["item"][:limit]
    return [
        CollectedItem(raw={**row, "cltrMngNo": f"{PREFIX}{row['cltrMngNo']}"}, group="N")
        for row in raw
    ]


def sample_detail(item: CollectedItem) -> BidDetail:
    detail = load_fixture("bid_detail_usbd2")["body"]["items"]["item"]
    raw = detail[0] if isinstance(detail, list) else detail
    return BidDetail(
        target=BidTarget(
            cltr_mng_no=str(item.raw["cltrMngNo"]),
            pbct_cdtn_no=str(item.raw["pbctCdtnNo"]),
        ),
        raw=raw,
    )


async def run_batch(conn: Conn, items: list[CollectedItem], *, started: datetime) -> int:
    """배치 한 회. 실제 파이프라인과 같은 순서로 부른다."""
    run_id = await start_run(conn, mode="full")

    rows = [r for r in (to_cltr_row(i) for i in items) if r is not None]
    loaded = await upsert_with_history(conn, rows)
    rounds = await upsert_bid_rounds(conn, to_round_rows(sample_detail(items[0])))
    await upsert_usage_codes(conn, [])
    await upsert_address_map(conn, [])
    tombstoned = await mark_tombstones(conn, seen_before=started, scope=SCOPE)

    await finish_run(
        conn, run_id, status="ok",
        counts=BatchCounts(collected=len(items), upserted=loaded.upserted,
                           tombstoned=tombstoned),
        note=f"회차 {rounds}",
    )
    return run_id


async def snapshot(conn: Conn) -> list[tuple[Any, ...]]:
    """비교용 스냅숏 — 적재 시각만 빼고 전부 본다."""
    async with conn.cursor() as cur:
        await cur.execute(
            "select column_name from information_schema.columns "
            "where table_name = 'onbid_cltr' order by ordinal_position")
        columns = [
            name for (name,) in await cur.fetchall() if name not in VOLATILE
        ]
        await cur.execute(
            f"select {', '.join(columns)} from onbid_cltr "
            "where cltr_mng_no like %s order by cltr_mng_no, pbct_cdtn_no",
            (f"{PREFIX}%",))
        return list(await cur.fetchall())


async def count_of(conn: Conn, table: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            f"select count(*) from {table} where cltr_mng_no like %s", (f"{PREFIX}%",))
        found = await cur.fetchone()
        return int(found[0]) if found else 0


async def test_rerunning_a_batch_leaves_the_same_state(conn: Conn) -> None:
    """AC2 — 두 번째 실행이 아무것도 바꾸지 않는다."""
    items = sample_items()
    started = datetime.now(UTC)

    await run_batch(conn, items, started=started)
    first = await snapshot(conn)

    await run_batch(conn, items, started=started)

    assert await snapshot(conn) == first


async def test_rerunning_a_batch_does_not_grow_tables(conn: Conn) -> None:
    """행이 늘면 통계·이력이 모두 부푼다."""
    items = sample_items()
    started = datetime.now(UTC)

    await run_batch(conn, items, started=started)
    before = (
        await count_of(conn, "onbid_cltr"),
        await count_of(conn, "onbid_cltr_history"),
        await count_of(conn, "onbid_cltr_bid_round"),
    )

    await run_batch(conn, items, started=started)

    assert (
        await count_of(conn, "onbid_cltr"),
        await count_of(conn, "onbid_cltr_history"),
        await count_of(conn, "onbid_cltr_bid_round"),
    ) == before


async def test_first_batch_records_no_history(conn: Conn) -> None:
    """처음 본 물건은 '변경' 이 아니다 — 첫 배치가 이력으로 가득 차면 안 된다."""
    await run_batch(conn, sample_items(), started=datetime.now(UTC))

    assert await count_of(conn, "onbid_cltr_history") == 0


async def test_real_change_is_recorded_once(conn: Conn) -> None:
    """멱등이라고 변경까지 놓치면 안 된다 — 바뀐 값은 정확히 한 번 남는다."""
    items = sample_items()
    started = datetime.now(UTC)
    await run_batch(conn, items, started=started)

    changed = [
        CollectedItem(raw={**item.raw, "usbdNft": (item.raw.get("usbdNft") or 0) + 1},
                      group=item.group)
        for item in items
    ]
    await run_batch(conn, changed, started=started)
    await run_batch(conn, changed, started=started)

    async with conn.cursor() as cur:
        await cur.execute(
            "select count(*) from onbid_cltr_history "
            "where cltr_mng_no like %s and field = 'fail_cnt'", (f"{PREFIX}%",))
        found = await cur.fetchone()
    assert found is not None
    assert found[0] == len(items)


async def test_completed_batch_leaves_no_resume_point(conn: Conn) -> None:
    """완주한 배치 뒤에 재개 지점이 남으면 다음 실행이 중간부터 시작한다 (N2.2)."""
    await run_batch(conn, sample_items(), started=datetime.now(UTC))

    assert await latest_resume_token(conn, mode="full") is None
