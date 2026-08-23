"""회차 이력 적재 테스트 (F1.7·§7).

`prcnBidClgList` 는 회차 이력을 통째로 준다 — 유찰 물건은 첫 배치부터 과거 전체를 얻는다.

**`pbct_nsq` 는 유일하지 않다.** 한 물건의 이력에 여러 공매 사건이 섞여 있어 회차 번호가
사건마다 1부터 다시 매겨진다 (실측 25건 표본에서 184건 충돌). 개찰일시까지 키에 넣어야
이력이 서로를 덮어쓰지 않는다.
"""

from datetime import datetime
from typing import Any, Final

import pytest

from core.normalizer.datetimes import KST
from core.onbid.bidinfo import BidDetail, BidTarget
from core.onbid.collector import CollectedItem
from core.store.bid_round import (
    mark_round_attempts,
    select_round_targets,
    to_round_rows,
    upsert_bid_rounds,
)
from core.store.cltr import upsert_cltr
from core.store.mapping import to_cltr_row
from tests.conftest import Conn

TARGET = BidTarget(cltr_mng_no="T-BR-1", pbct_cdtn_no="1")

ROUND = {
    "cltrOpbdDt": "202606181100",
    "pbctNsq": "021",
    "pbctsn": "001",
    "pbctStatNm": "유찰",
    "lowstBidPrcIndctCont": "581800000",
    "scfbAmt": None,
}


def detail(*rounds: dict[str, Any], target: BidTarget = TARGET) -> BidDetail:
    return BidDetail(target=target, raw={"prcnBidClgList": list(rounds)})


def one(**overrides: Any) -> dict[str, Any]:
    return {**ROUND, **overrides}


# ── 매핑 ────────────────────────────────────────────────────────────────


def test_round_mapping_carries_the_parent_key() -> None:
    row = to_round_rows(detail(one()))[0]
    assert (row["cltr_mng_no"], row["pbct_cdtn_no"]) == ("T-BR-1", "1")


def test_round_mapping_parses_opening_datetime() -> None:
    """개찰일시가 사건을 가르는 실질 키다."""
    row = to_round_rows(detail(one()))[0]
    assert row["opbd_dt"] == datetime(2026, 6, 18, 11, 0, tzinfo=KST)


def test_round_mapping_keeps_sequence_as_text() -> None:
    """`"021"` 을 정수로 바꾸면 선행 0 이 사라져 원본과 대조할 수 없다."""
    row = to_round_rows(detail(one()))[0]
    assert row["pbct_nsq"] == "021"
    assert row["pbct_sn"] == "001"


def test_round_mapping_derives_status_from_name() -> None:
    """회차 이력에는 `pbctStatCd` 가 없다 — 이름으로 파생한다 (§7.1)."""
    row = to_round_rows(detail(one()))[0]
    assert row["result_nm"] == "유찰"
    assert row["status"] == "유찰"


def test_round_mapping_parses_amounts() -> None:
    row = to_round_rows(detail(one(scfbAmt=700000000)))[0]
    assert row["min_bid_amt"] == 581800000
    assert row["min_bid_amt_text"] is None
    assert row["winning_amt"] == 700000000


def test_round_mapping_leaves_winning_amount_empty_when_not_sold() -> None:
    """유찰 회차의 낙찰가는 null 이다 — 0 으로 채우면 통계가 무너진다 (§8.3)."""
    assert to_round_rows(detail(one()))[0]["winning_amt"] is None


def test_round_mapping_preserves_undisclosed_price() -> None:
    row = to_round_rows(detail(one(lowstBidPrcIndctCont="비공개")))[0]
    assert row["min_bid_amt"] is None
    assert row["min_bid_amt_text"] == "비공개"


def test_round_mapping_skips_rows_without_opening_datetime() -> None:
    """개찰일시는 PK 다 — 없으면 적재할 수 없다."""
    assert to_round_rows(detail(one(cltrOpbdDt=None))) == []


def test_round_mapping_skips_rows_without_sequence() -> None:
    assert to_round_rows(detail(one(pbctNsq=None))) == []


def test_round_mapping_handles_empty_history() -> None:
    assert to_round_rows(detail()) == []


def test_round_mapping_keeps_every_round() -> None:
    """한 물건 최대 75행이 실측됐다 — 잘라내면 하락 곡선이 끊긴다."""
    rounds = [one(pbctNsq=f"{i:03d}", cltrOpbdDt=f"2026061811{i:02d}") for i in range(30)]
    assert len(to_round_rows(detail(*rounds))) == 30


def test_round_mapping_on_real_response() -> None:
    from tests.conftest import load_fixture
    raw = load_fixture("bid_detail_usbd2")["body"]["items"]["item"]
    item = raw[0] if isinstance(raw, list) else raw

    rows = to_round_rows(BidDetail(target=TARGET, raw=item))

    assert rows
    for row in rows:
        assert row["opbd_dt"] is not None
        assert row["pbct_nsq"]


# ── 적재 ────────────────────────────────────────────────────────────────


@pytest.mark.db
async def test_round_upsert_inserts_rows(conn: Conn) -> None:
    assert await upsert_bid_rounds(conn, to_round_rows(detail(one()))) == 1


@pytest.mark.db
async def test_round_upsert_separates_events_with_the_same_sequence(conn: Conn) -> None:
    """회차 번호가 같아도 개찰일시가 다르면 다른 사건이다 — 덮어쓰면 이력이 사라진다."""
    rows = to_round_rows(detail(
        one(cltrOpbdDt="202406181100"),
        one(cltrOpbdDt="202606181100"),
    ))

    await upsert_bid_rounds(conn, rows)

    async with conn.cursor() as cur:
        await cur.execute(
            "select count(*) from onbid_cltr_bid_round where cltr_mng_no = %s", ("T-BR-1",))
        found = await cur.fetchone()
    assert found is not None and found[0] == 2


@pytest.mark.db
async def test_round_upsert_is_idempotent(conn: Conn) -> None:
    """주기적 재수집이 회차를 부풀리면 낙찰가율 표본이 왜곡된다 (AC2)."""
    rows = to_round_rows(detail(one(), one(cltrOpbdDt="202406181100")))
    await upsert_bid_rounds(conn, rows)
    await upsert_bid_rounds(conn, rows)

    async with conn.cursor() as cur:
        await cur.execute(
            "select count(*) from onbid_cltr_bid_round where cltr_mng_no = %s", ("T-BR-1",))
        found = await cur.fetchone()
    assert found is not None and found[0] == 2


@pytest.mark.db
async def test_round_upsert_fills_winning_amount_later(conn: Conn) -> None:
    """낙찰가는 나중에 붙는다 — 재수집이 값을 갱신해야 한다."""
    await upsert_bid_rounds(conn, to_round_rows(detail(one())))
    await upsert_bid_rounds(conn, to_round_rows(
        detail(one(pbctStatNm="낙찰", scfbAmt=700000000))))

    async with conn.cursor() as cur:
        await cur.execute(
            "select winning_amt, status from onbid_cltr_bid_round "
            "where cltr_mng_no = %s", ("T-BR-1",))
        found = await cur.fetchone()
    assert found is not None
    assert (found[0], found[1]) == (700000000, "낙찰")


@pytest.mark.db
async def test_round_upsert_of_empty_list(conn: Conn) -> None:
    assert await upsert_bid_rounds(conn, []) == 0


# ── 대상 선별·시도 기록 (F1.11·F1.16) ──────────────────────────────────


#: 실데이터와 섞이지 않게 하는 장치.
#: 첫 실적재(2026-08-23) 이후 이 테이블에는 실제 물건 6,900여 건이 들어 있고, 선별 질의는
#: 그것들을 정상적으로 반환한다. 테스트 행을 정렬로 앞세우려 하면 실데이터 분포에 의존하게
#: 되므로, **`min_fail_count` 로 후보 자체를 갈라낸다** — 프로덕션에 테스트용 인자를
#: 추가하지 않고, 실 테이블을 지우지도 않는다.
TEST_FAIL_CNT: Final = 9999
TEST_MIN_FAIL: Final = 9000


def cltr(mng: str, **overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "cltrMngNo": mng, "pbctCdtnNo": "1", "onbidCltrNm": "테스트",
        "lctnSdnm": "서울특별시", "lctnSggnm": "강남구", "lctnEmdNm": "개포동",
        "apslEvlAmt": 1000, "lowstBidPrcIndctCont": "500",
        "usbdNft": TEST_FAIL_CNT, "pbctStatCd": "0001", "pvctTrgtYn": "N", **overrides,
    }
    row = to_cltr_row(CollectedItem(raw=raw, group=raw["pvctTrgtYn"]))
    assert row is not None
    return row


async def seed_cltr(conn: Conn, *rows: dict[str, Any]) -> None:
    """행마다 따로 넣는다 — `upsert_cltr` 는 첫 행의 컬럼 구성을 배치 전체에 쓴다."""
    for row in rows:
        await upsert_cltr(conn, [row])


def names(targets: list[BidTarget]) -> list[str]:
    return [t.cltr_mng_no for t in targets]


@pytest.mark.db
async def test_targets_exclude_never_failed_items(conn: Conn) -> None:
    """유찰 0회는 이력이 비어 있다 — 호출하면 `03` 만 돌아와 예산을 태운다 (F1.11)."""
    await seed_cltr(conn, cltr("T-TG-FAIL"), cltr("T-TG-NONE", usbdNft=0))

    picked = names(await select_round_targets(conn, limit=50, min_fail_count=TEST_MIN_FAIL))

    assert "T-TG-FAIL" in picked
    assert "T-TG-NONE" not in picked


@pytest.mark.db
async def test_targets_exclude_private_contract_items(conn: Conn) -> None:
    """수의계약은 입찰이 아니라 입찰정보가 없다 — 실측 18/18건 `03` (F1.11)."""
    await seed_cltr(conn, cltr("T-TG-PVCT", pvctTrgtYn="Y"))

    picked = await select_round_targets(conn, limit=50, min_fail_count=TEST_MIN_FAIL)
    assert "T-TG-PVCT" not in names(picked)


@pytest.mark.db
async def test_targets_put_never_attempted_first(conn: Conn) -> None:
    """새로 등장한 물건은 시도 시각이 null 이라 자연히 최우선이다 (F1.16)."""
    done = cltr("T-TG-DONE")
    done["bid_round_synced_at"] = datetime.now(KST)
    await seed_cltr(conn, done, cltr("T-TG-NEW"))

    picked = names(await select_round_targets(conn, limit=50, min_fail_count=TEST_MIN_FAIL))

    assert picked.index("T-TG-NEW") < picked.index("T-TG-DONE")


@pytest.mark.db
async def test_targets_are_ordered_by_least_recently_attempted(conn: Conn) -> None:
    """오래 안 본 것부터 — 며칠에 걸쳐 한 바퀴를 돈다."""
    old, recent = cltr("T-TG-OLD"), cltr("T-TG-RECENT")
    old["bid_round_synced_at"] = datetime(2026, 1, 1, tzinfo=KST)
    recent["bid_round_synced_at"] = datetime(2026, 8, 1, tzinfo=KST)
    await seed_cltr(conn, recent, old)

    picked = names(await select_round_targets(conn, limit=50, min_fail_count=TEST_MIN_FAIL))

    assert picked.index("T-TG-OLD") < picked.index("T-TG-RECENT")


@pytest.mark.db
async def test_targets_respect_the_budget(conn: Conn) -> None:
    """일일 트래픽이 1,000건이다 — 상한을 넘겨 받으면 안 된다."""
    await seed_cltr(conn, *[cltr(f"T-TG-B{i:02d}") for i in range(5)])

    assert len(await select_round_targets(conn, limit=3, min_fail_count=TEST_MIN_FAIL)) == 3


@pytest.mark.db
async def test_targets_carry_the_composite_key(conn: Conn) -> None:
    """입찰정보 조회가 두 값을 함께 요구한다."""
    await seed_cltr(conn, cltr("T-TG-KEY", pbctCdtnNo="777"))

    picked = [t for t in await select_round_targets(conn, limit=50, min_fail_count=TEST_MIN_FAIL)
              if t.cltr_mng_no == "T-TG-KEY"]

    assert picked[0].pbct_cdtn_no == "777"


@pytest.mark.db
async def test_marking_attempts_moves_items_to_the_back(conn: Conn) -> None:
    """시도를 기록해야 다음 회차가 다른 물건을 본다."""
    await seed_cltr(conn, cltr("T-TG-MARK"))
    target = [t for t in await select_round_targets(conn, limit=50, min_fail_count=TEST_MIN_FAIL)
              if t.cltr_mng_no == "T-TG-MARK"][0]

    marked = await mark_round_attempts(conn, [target], attempted_at=datetime.now(KST))

    assert marked == 1
    async with conn.cursor() as cur:
        await cur.execute(
            "select bid_round_synced_at from onbid_cltr where cltr_mng_no = %s", ("T-TG-MARK",))
        found = await cur.fetchone()
    assert found is not None and found[0] is not None


@pytest.mark.db
async def test_marking_an_empty_list_is_a_noop(conn: Conn) -> None:
    assert await mark_round_attempts(conn, [], attempted_at=datetime.now(KST)) == 0
