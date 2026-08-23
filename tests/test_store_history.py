"""변경 이력 적재 테스트 (`pytest -m db`, F4.4).

`min_bid_amt`·`fail_cnt`·`status` 가 바뀐 순간을 남긴다. 온비드 응답은 **현재 상태만**
주므로, 이 순간을 놓치면 "언제 얼마였는지" 를 다시는 알 수 없다.

**호출 순서가 곧 정확성이다.** upsert 뒤에 비교하면 DB 값이 이미 새 값이라 차이가 0이 된다
— 조용히 아무것도 기록되지 않는다. `upsert_with_history` 가 순서를 대신 지킨다.
"""

from typing import Any

import pytest

from core.onbid.collector import CollectedItem
from core.store.cltr import upsert_cltr
from core.store.history import record_changes, upsert_with_history
from core.store.mapping import to_cltr_row
from tests.conftest import Conn

pytestmark = pytest.mark.db


def item(mng: str, **overrides: Any) -> CollectedItem:
    raw: dict[str, Any] = {
        "cltrMngNo": mng, "pbctCdtnNo": "1", "onbidCltrNm": "테스트",
        "lctnSdnm": "서울특별시", "lctnSggnm": "강남구", "lctnEmdNm": "개포동",
        "apslEvlAmt": 1000, "lowstBidPrcIndctCont": "500",
        "usbdNft": 3, "pbctStatCd": "0001", "pvctTrgtYn": "N", **overrides,
    }
    return CollectedItem(raw=raw, group="N")


def rows(*items: CollectedItem) -> list[dict[str, Any]]:
    return [r for r in (to_cltr_row(i) for i in items) if r is not None]


async def history_of(conn: Conn, mng: str) -> list[tuple[Any, ...]]:
    async with conn.cursor() as cur:
        await cur.execute(
            "select field, old_value, new_value from onbid_cltr_history "
            "where cltr_mng_no = %s order by changed_at, field",
            (mng,),
        )
        return list(await cur.fetchall())


# ── 변경 감지 ───────────────────────────────────────────────────────────


async def test_history_records_price_drop(conn: Conn) -> None:
    """유찰마다 최저가가 내려간다 — 이 하락 곡선이 이 서비스의 핵심 데이터다."""
    await upsert_cltr(conn, rows(item("T-H-DROP")))

    changed = await record_changes(conn, rows(item("T-H-DROP", lowstBidPrcIndctCont="400")))

    assert changed == 1
    assert await history_of(conn, "T-H-DROP") == [("min_bid_amt", "500", "400")]


async def test_history_records_fail_count_and_status(conn: Conn) -> None:
    await upsert_cltr(conn, rows(item("T-H-MULTI")))

    changed = await record_changes(
        conn, rows(item("T-H-MULTI", usbdNft=4, pbctStatCd="0011"))
    )

    assert changed == 2
    assert await history_of(conn, "T-H-MULTI") == [
        ("fail_cnt", "3", "4"),
        ("status", "진행", "유찰"),
    ]


async def test_history_ignores_untracked_fields(conn: Conn) -> None:
    """이름·썸네일까지 남기면 이력이 잡음으로 덮인다."""
    await upsert_cltr(conn, rows(item("T-H-NOISE")))

    changed = await record_changes(conn, rows(item("T-H-NOISE", onbidCltrNm="이름 변경")))

    assert changed == 0


async def test_history_skips_unchanged_rows(conn: Conn) -> None:
    await upsert_cltr(conn, rows(item("T-H-SAME")))
    assert await record_changes(conn, rows(item("T-H-SAME"))) == 0


async def test_history_ignores_first_sighting(conn: Conn) -> None:
    """처음 본 물건은 '변경' 이 아니다 — null→값 을 이력으로 남기지 않는다."""
    assert await record_changes(conn, rows(item("T-H-NEW"))) == 0
    assert await history_of(conn, "T-H-NEW") == []


async def test_history_records_transition_to_null(conn: Conn) -> None:
    """`"비공개"` 로 가려지는 것도 변경이다 (F4.7)."""
    await upsert_cltr(conn, rows(item("T-H-HIDE")))

    changed = await record_changes(
        conn, rows(item("T-H-HIDE", lowstBidPrcIndctCont="비공개"))
    )

    assert changed == 1
    assert await history_of(conn, "T-H-HIDE") == [("min_bid_amt", "500", None)]


async def test_history_of_empty_batch(conn: Conn) -> None:
    assert await record_changes(conn, []) == 0


# ── 호출 순서 (핵심 함정) ───────────────────────────────────────────────


async def test_history_wrapper_records_then_upserts(conn: Conn) -> None:
    """`upsert_with_history` 가 순서를 대신 지킨다 — 뒤집으면 이력이 조용히 사라진다."""
    await upsert_cltr(conn, rows(item("T-H-WRAP")))

    result = await upsert_with_history(conn, rows(item("T-H-WRAP", usbdNft=9)))

    assert result.upserted == 1
    assert result.changes == 1
    assert await history_of(conn, "T-H-WRAP") == [("fail_cnt", "3", "9")]


async def test_history_wrapper_leaves_new_row_loaded(conn: Conn) -> None:
    result = await upsert_with_history(conn, rows(item("T-H-WNEW")))

    assert (result.upserted, result.changes) == (1, 0)
    async with conn.cursor() as cur:
        await cur.execute(
            "select fail_cnt from onbid_cltr where cltr_mng_no = %s", ("T-H-WNEW",))
        found = await cur.fetchone()
    assert found is not None and found[0] == 3


async def test_history_does_not_duplicate_on_replay(conn: Conn) -> None:
    """같은 배치를 두 번 돌려도 이력은 한 번만 쌓인다 (AC2)."""
    await upsert_cltr(conn, rows(item("T-H-REPLAY")))
    payload = rows(item("T-H-REPLAY", usbdNft=7))

    await upsert_with_history(conn, payload)
    await upsert_with_history(conn, payload)

    assert len(await history_of(conn, "T-H-REPLAY")) == 1


# ── 대량 처리 ───────────────────────────────────────────────────────────


async def test_history_compares_a_batch_in_bulk(conn: Conn) -> None:
    """행마다 조회하면 6,910건 배치가 끝나지 않는다 (F4.10)."""
    before = rows(*[item(f"T-H-BULK-{i:03d}") for i in range(100)])
    await upsert_cltr(conn, before)
    after = rows(*[item(f"T-H-BULK-{i:03d}", usbdNft=4) for i in range(100)])

    assert await record_changes(conn, after) == 100


async def test_history_matches_the_full_key(conn: Conn) -> None:
    """물건관리번호가 같아도 공매조건번호가 다르면 다른 행이다 — 섞이면 안 된다."""
    await upsert_cltr(conn, rows(item("T-H-KEY"), item("T-H-KEY", pbctCdtnNo="2")))

    changed = await record_changes(conn, rows(item("T-H-KEY", usbdNft=8)))

    assert changed == 1
