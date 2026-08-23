"""물건 적재 테스트 (`pytest -m db`, F4.1·F4.3).

**롤백 트랜잭션 안에서 돌린다** — 이 Supabase 는 다른 프로젝트와 공유되는 실서비스라
테스트가 흔적을 남기면 안 된다.

복합키 upsert 가 핵심이다. 온비드 상세·입찰정보 조회가 `cltrMngNo` + `pbctCdtnNo`
두 값을 함께 요구하므로 PK 도 복합키다.
"""

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from core.onbid.collector import CollectedItem
from core.store.cltr import upsert_cltr
from core.store.mapping import to_cltr_row
from tests.conftest import Conn

pytestmark = pytest.mark.db


def item(mng: str, cdtn: str = "1", **overrides: Any) -> CollectedItem:
    raw: dict[str, Any] = {
        "cltrMngNo": mng, "pbctCdtnNo": cdtn,
        "onbidCltrNm": "테스트 물건", "lctnSdnm": "서울특별시",
        "lctnSggnm": "강남구", "lctnEmdNm": "개포동",
        "apslEvlAmt": 1000, "lowstBidPrcIndctCont": "500",
        "usbdNft": 3, "pbctStatCd": "0001", "pvctTrgtYn": "N",
        **overrides,
    }
    return CollectedItem(raw=raw, group="N")


def rows(*items: CollectedItem) -> list[dict[str, Any]]:
    mapped = [to_cltr_row(i) for i in items]
    return [m for m in mapped if m is not None]


async def count_of(conn: Conn, mng: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute("select count(*) from onbid_cltr where cltr_mng_no = %s", (mng,))
        found = await cur.fetchone()
        return int(found[0]) if found else 0


async def field_of(conn: Conn, mng: str, column: str) -> Any:
    async with conn.cursor() as cur:
        await cur.execute(f"select {column} from onbid_cltr where cltr_mng_no = %s", (mng,))
        found = await cur.fetchone()
        return found[0] if found else None


# ── 기본 적재 ───────────────────────────────────────────────────────────


async def test_upsert_inserts_new_rows(conn: Conn) -> None:
    inserted = await upsert_cltr(conn, rows(item("T-INS-1"), item("T-INS-2")))
    assert inserted == 2
    assert await count_of(conn, "T-INS-1") == 1


async def test_upsert_of_empty_list_is_a_noop(conn: Conn) -> None:
    assert await upsert_cltr(conn, []) == 0


async def test_upsert_stores_raw_payload_as_json(conn: Conn) -> None:
    """jsonb 라 SQL 로 질의할 수 있어야 한다 (F1.3)."""
    await upsert_cltr(conn, rows(item("T-JSON-1")))
    payload = await field_of(conn, "T-JSON-1", "raw_payload")
    assert payload["cltrMngNo"] == "T-JSON-1"


async def test_upsert_computes_rate(conn: Conn) -> None:
    await upsert_cltr(conn, rows(item("T-RATE-1")))
    assert float(await field_of(conn, "T-RATE-1", "min_bid_rate")) == pytest.approx(0.5)


# ── 복합키 (F4.1) ───────────────────────────────────────────────────────


async def test_upsert_treats_same_item_different_condition_as_separate(conn: Conn) -> None:
    """물건관리번호가 같아도 공매조건번호가 다르면 다른 행이다."""
    await upsert_cltr(conn, rows(item("T-PK-1", "100"), item("T-PK-1", "200")))
    assert await count_of(conn, "T-PK-1") == 2


async def test_upsert_updates_on_conflict(conn: Conn) -> None:
    await upsert_cltr(conn, rows(item("T-UPD-1", usbdNft=3)))
    await upsert_cltr(conn, rows(item("T-UPD-1", usbdNft=9)))

    assert await count_of(conn, "T-UPD-1") == 1
    assert await field_of(conn, "T-UPD-1", "fail_cnt") == 9


# ── 멱등성 (AC2) ────────────────────────────────────────────────────────


async def test_upsert_is_idempotent(conn: Conn) -> None:
    """같은 입력을 두 번 넣어도 상태가 같아야 한다."""
    payload = rows(item("T-IDEM-1"), item("T-IDEM-2"))
    await upsert_cltr(conn, payload)
    snapshot = await field_of(conn, "T-IDEM-1", "raw_payload")

    await upsert_cltr(conn, payload)

    assert await count_of(conn, "T-IDEM-1") == 1
    assert await field_of(conn, "T-IDEM-1", "raw_payload") == snapshot


# ── 시각 필드 (F4.3) ────────────────────────────────────────────────────


async def test_upsert_keeps_first_seen_at_on_update(conn: Conn) -> None:
    """`first_seen_at` 은 처음 본 시각이다. 갱신할 때 덮어쓰면 의미가 사라진다."""
    await upsert_cltr(conn, rows(item("T-SEEN-1")))
    first = await field_of(conn, "T-SEEN-1", "first_seen_at")

    await upsert_cltr(conn, rows(item("T-SEEN-1", usbdNft=5)))

    assert await field_of(conn, "T-SEEN-1", "first_seen_at") == first


async def test_upsert_advances_last_seen_at(conn: Conn) -> None:
    """`last_seen_at` 은 tombstone 판정의 기준이라 매 배치 갱신돼야 한다 (F4.2)."""
    await upsert_cltr(conn, rows(item("T-SEEN-2")))
    before = await field_of(conn, "T-SEEN-2", "last_seen_at")

    later = rows(item("T-SEEN-2"))
    later[0]["last_seen_at"] = datetime.now(UTC)
    await upsert_cltr(conn, later)

    assert await field_of(conn, "T-SEEN-2", "last_seen_at") > before


# ── 대량 처리 (F4.10) ───────────────────────────────────────────────────


async def test_upsert_handles_a_batch(conn: Conn) -> None:
    """서울 전량이 6,910건이다. 행 단위 왕복이면 배치가 끝나지 않는다."""
    batch = rows(*[item(f"T-BULK-{i:04d}") for i in range(200)])
    assert await upsert_cltr(conn, batch) == 200


async def test_upsert_survives_undisclosed_price(conn: Conn) -> None:
    """`"비공개"` 는 정수 컬럼에 넣을 수 없다. null + 원문으로 갈라져야 한다 (F4.7)."""
    await upsert_cltr(conn, rows(item("T-HIDE-1", lowstBidPrcIndctCont="비공개")))

    assert await field_of(conn, "T-HIDE-1", "min_bid_amt") is None
    assert await field_of(conn, "T-HIDE-1", "min_bid_amt_text") == "비공개"


async def test_upsert_survives_tbd_schedule(conn: Conn) -> None:
    """`2999` 는 null + 플래그로 들어가야 한다 (§7.1)."""
    await upsert_cltr(conn, rows(item("T-TBD-1", cltrBidEndDt="299901021600")))

    assert await field_of(conn, "T-TBD-1", "bid_end") is None
    assert await field_of(conn, "T-TBD-1", "bid_date_tbd") is True


async def test_upsert_rejects_rows_without_key(conn: Conn) -> None:
    """매핑 단계에서 걸러지므로 여기까지 오면 안 된다 — 방어적으로 확인한다."""
    with pytest.raises((KeyError, psycopg.Error)):
        await upsert_cltr(conn, [{"cltr_nm": "키 없음"}])
