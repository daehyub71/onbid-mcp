"""물건 배치 오케스트레이션 테스트 (`pytest -m db`, F4.16·F4.17).

**커밋 경계가 이 모듈의 존재 이유다.** 적재 계층은 전부 "커밋은 호출자가 한다" 로 만들었고,
그 호출자가 여기다. 경계를 틀리면 두 가지가 깨진다.

1. 메타를 데이터와 같은 트랜잭션에 묶으면 → 배치가 죽었을 때 메타까지 사라진다 (F4.6 무의미)
2. 이력·적재·tombstone 을 나눠 커밋하면 → 이력만 남고 적재가 실패한 상태가 생긴다

그리고 tombstone 을 판정해도 되는 조건이 셋으로 늘었다: 전량 모드(F4.2) · 범위 일치(F4.13) ·
**수집 완주(F4.17)**. 셋 중 하나라도 어기면 멀쩡한 물건이 종료 처리된다.

**이 테스트는 커밋을 무력화한 채 실제 SQL 을 돌린다** — 공유 Supabase 라 흔적을 남길 수 없다.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from core.onbid.client import OnbidClient
from core.onbid.collector import CollectedItem, CollectResult, ListingFilter, PageFailure
from core.pipeline.batch import run_listing_batch
from core.store.batch_run import latest_resume_token
from core.store.cltr import upsert_cltr
from core.store.mapping import to_cltr_row
from tests.conftest import Conn

pytestmark = pytest.mark.db

CLIENT = cast(OnbidClient, object())


@pytest.fixture
def calls(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """커밋·롤백을 기록하고 **실제로는 수행하지 않는다.**

    monkeypatch 는 `conn` fixture 보다 나중에 정리되므로, teardown 시점에는 원래 `rollback`
    이 복구돼 트랜잭션이 정상적으로 되돌아간다.
    """
    recorded: list[str] = []

    async def fake_commit() -> None:
        recorded.append("commit")

    async def fake_rollback() -> None:
        recorded.append("rollback")

    monkeypatch.setattr(conn, "commit", fake_commit)
    monkeypatch.setattr(conn, "rollback", fake_rollback)
    return recorded


def item(mng: str, *, sgg: str = "강남구", **overrides: Any) -> CollectedItem:
    raw: dict[str, Any] = {
        "cltrMngNo": mng, "pbctCdtnNo": "1", "onbidCltrNm": "테스트",
        "lctnSdnm": "서울특별시", "lctnSggnm": sgg, "lctnEmdNm": "개포동",
        "apslEvlAmt": 1000, "lowstBidPrcIndctCont": "500",
        "usbdNft": 3, "pbctStatCd": "0001", "pvctTrgtYn": "N", **overrides,
    }
    return CollectedItem(raw=raw, group="N")


def collector(
    result: CollectResult, *, seen: list[dict[str, Any]] | None = None
) -> Callable[..., Any]:
    """`collect_listings` 자리에 끼울 대역.

    `seen` 을 주면 넘어온 인자를 기록한다 — `CollectResult` 는 slots 이라 속성을 못 붙인다.
    """

    async def fake_collect(client: OnbidClient, **kwargs: Any) -> CollectResult:
        if seen is not None:
            seen.append(kwargs)
        return result

    return fake_collect


def complete(*items: CollectedItem) -> CollectResult:
    return CollectResult(items=list(items), total_by_group={"N": len(items)}, pages_fetched=1)


async def status_of(conn: Conn, mng: str) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("select status from onbid_cltr where cltr_mng_no = %s", (mng,))
        found = await cur.fetchone()
        return found[0] if found else None


async def run_row(conn: Conn, run_id: int) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute("select * from onbid_batch_run where run_id = %s", (run_id,))
        found = await cur.fetchone()
        assert found is not None
        assert cur.description is not None
        return dict(zip([c.name for c in cur.description], found, strict=True))


# ── 커밋 경계 (F4.16) ───────────────────────────────────────────────────


async def test_batch_commits_meta_before_collecting(conn: Conn, calls: list[str]) -> None:
    """메타 행은 수집을 시작하기 전에 이미 커밋돼 있어야 한다.

    수집 중 프로세스가 죽어도 '시작했다가 끝나지 않은 배치' 가 남는다.
    """
    seen: list[int] = []

    async def watching_collect(client: OnbidClient, **kwargs: Any) -> CollectResult:
        seen.append(len(calls))
        return complete(item("T-PB-META"))

    await run_listing_batch(conn, CLIENT, collect=watching_collect)

    assert seen == [1]


async def test_batch_commits_data_once(conn: Conn, calls: list[str]) -> None:
    """이력·적재·tombstone 은 한 트랜잭션이다 — 중간 커밋이 있으면 부분 적재가 남는다."""
    await run_listing_batch(conn, CLIENT, collect=collector(complete(item("T-PB-TX"))))

    # 메타 개설 · 데이터 · 메타 종료
    assert calls == ["commit", "commit", "commit"]


async def test_batch_rolls_back_data_on_failure(
    conn: Conn, calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """적재가 실패하면 되돌린다 — 이력만 남은 상태를 만들지 않는다."""

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("적재 실패")

    monkeypatch.setattr("core.pipeline.batch.upsert_with_history", boom)

    with pytest.raises(RuntimeError, match="적재 실패"):
        await run_listing_batch(conn, CLIENT, collect=collector(complete(item("T-PB-FAIL"))))

    assert "rollback" in calls


# ── 모드 파생 (F4.2·F1.8) ───────────────────────────────────────────────


async def test_batch_derives_full_mode_from_the_filter(conn: Conn, calls: list[str]) -> None:
    outcome = await run_listing_batch(
        conn, CLIENT, collect=collector(complete(item("T-PB-FULL"))))

    assert outcome.mode == "full"
    assert (await run_row(conn, outcome.run_id))["mode"] == "full"


async def test_batch_derives_delta_mode_from_the_filter(conn: Conn, calls: list[str]) -> None:
    """증분 여부는 `ListingFilter` 하나에서 나온다 — 따로 넘기면 어긋날 수 있다 (F1.8)."""
    outcome = await run_listing_batch(
        conn, CLIENT,
        listing_filter=ListingFilter(modified_from="20260820"),
        collect=collector(complete(item("T-PB-DELTA"))),
    )

    assert outcome.mode == "delta"
    assert (await run_row(conn, outcome.run_id))["mode"] == "delta"


async def test_batch_passes_the_filter_to_the_collector(conn: Conn, calls: list[str]) -> None:
    """수집 조건과 tombstone 범위가 같은 필터에서 나와야 한다."""
    seen: list[dict[str, Any]] = []
    listing_filter = ListingFilter(region_sgg="강남구")

    await run_listing_batch(conn, CLIENT, listing_filter=listing_filter,
                            collect=collector(complete(item("T-PB-FLT")), seen=seen))

    assert seen[0]["listing_filter"] is listing_filter


# ── 적재 ────────────────────────────────────────────────────────────────


async def test_batch_loads_collected_items(conn: Conn, calls: list[str]) -> None:
    outcome = await run_listing_batch(
        conn, CLIENT, collect=collector(complete(item("T-PB-L1"), item("T-PB-L2"))))

    assert (outcome.collected, outcome.upserted) == (2, 2)
    assert await status_of(conn, "T-PB-L1") == "진행"


async def test_batch_records_history_on_the_second_run(conn: Conn, calls: list[str]) -> None:
    """이력은 적재보다 먼저 판정돼야 한다 (F4.14) — 파이프라인을 거쳐도 지켜지는지 본다."""
    await run_listing_batch(conn, CLIENT, collect=collector(complete(item("T-PB-H"))))

    outcome = await run_listing_batch(
        conn, CLIENT, collect=collector(complete(item("T-PB-H", usbdNft=9))))

    assert outcome.changes == 1


async def test_batch_counts_unmappable_items(conn: Conn, calls: list[str]) -> None:
    """복합키가 없어 버린 행을 조용히 삼키지 않는다."""
    broken = CollectedItem(raw={"onbidCltrNm": "키 없음"}, group="N")
    result = CollectResult(items=[item("T-PB-OK"), broken], pages_fetched=1)

    outcome = await run_listing_batch(conn, CLIENT, collect=collector(result))

    assert (outcome.collected, outcome.upserted, outcome.skipped) == (2, 1, 1)


async def test_batch_records_counts_in_meta(conn: Conn, calls: list[str]) -> None:
    outcome = await run_listing_batch(
        conn, CLIENT, collect=collector(complete(item("T-PB-C1"), item("T-PB-C2"))))

    row = await run_row(conn, outcome.run_id)
    assert (row["collected"], row["upserted"]) == (2, 2)
    assert row["status"] == "ok"
    assert row["finished_at"] is not None


# ── tombstone 판정 조건 (F4.2·F4.13·F4.17) ──────────────────────────────


async def test_batch_marks_tombstones_after_a_complete_full_run(
    conn: Conn, calls: list[str]
) -> None:
    rows = [r for r in [to_cltr_row(item("T-PB-GONE"))] if r]
    rows[0]["last_seen_at"] = datetime.now(UTC) - timedelta(days=2)
    await upsert_cltr(conn, rows)

    outcome = await run_listing_batch(
        conn, CLIENT, collect=collector(complete(item("T-PB-ALIVE"))))

    assert outcome.tombstoned >= 1
    assert await status_of(conn, "T-PB-GONE") == "종료추정"
    assert await status_of(conn, "T-PB-ALIVE") == "진행"


async def test_batch_skips_tombstone_when_collection_is_incomplete(
    conn: Conn, calls: list[str]
) -> None:
    """**F4.17** — 페이지가 실패한 회차에 판정하면 그 페이지의 물건이 통째로 종료 처리된다."""
    rows = [r for r in [to_cltr_row(item("T-PB-PARTIAL"))] if r]
    rows[0]["last_seen_at"] = datetime.now(UTC) - timedelta(days=2)
    await upsert_cltr(conn, rows)

    result = complete(item("T-PB-SEEN"))
    result.failed_pages.append(PageFailure(group="N", page=7, reason="[04] 서버 오류"))

    outcome = await run_listing_batch(conn, CLIENT, collect=collector(result))

    assert outcome.tombstoned == 0
    assert await status_of(conn, "T-PB-PARTIAL") == "진행"
    assert outcome.status == "partial"


async def test_batch_skips_tombstone_when_truncated(conn: Conn, calls: list[str]) -> None:
    """페이지 상한에 걸린 것도 '전량을 봤다' 가 아니다."""
    result = complete(item("T-PB-TRUNC"))
    result.truncated = True

    outcome = await run_listing_batch(conn, CLIENT, collect=collector(result))

    assert outcome.tombstoned == 0
    assert outcome.status == "partial"


async def test_batch_skips_tombstone_in_delta_mode(conn: Conn, calls: list[str]) -> None:
    """**F4.2** — 증분의 '응답에 없음' 은 '변경 없음' 이다."""
    rows = [r for r in [to_cltr_row(item("T-PB-DKEEP"))] if r]
    rows[0]["last_seen_at"] = datetime.now(UTC) - timedelta(days=2)
    await upsert_cltr(conn, rows)

    outcome = await run_listing_batch(
        conn, CLIENT,
        listing_filter=ListingFilter(modified_from="20260820"),
        collect=collector(complete(item("T-PB-DSEEN"))),
    )

    assert outcome.tombstoned == 0
    assert await status_of(conn, "T-PB-DKEEP") == "진행"
    assert outcome.status == "ok"


async def test_batch_limits_tombstone_to_the_collected_district(
    conn: Conn, calls: list[str]
) -> None:
    """**F4.13** — 강남구만 수집했으면 서초구는 건드리지 않는다."""
    stale = datetime.now(UTC) - timedelta(days=2)
    rows = [r for r in (to_cltr_row(i) for i in
                        (item("T-PB-GN", sgg="강남구"), item("T-PB-SC", sgg="서초구"))) if r]
    for row in rows:
        row["last_seen_at"] = stale
    await upsert_cltr(conn, rows)

    await run_listing_batch(
        conn, CLIENT,
        listing_filter=ListingFilter(region_sgg="강남구"),
        collect=collector(complete(item("T-PB-GNSEEN", sgg="강남구"))),
    )

    assert await status_of(conn, "T-PB-GN") == "종료추정"
    assert await status_of(conn, "T-PB-SC") == "진행"


# ── 재개 지점 (N2.2) ────────────────────────────────────────────────────


async def test_batch_records_the_resume_point_on_abort(conn: Conn, calls: list[str]) -> None:
    """쿼터로 끊기면 다음 실행이 그 지점부터 이어받아야 한다."""
    result = complete(item("T-PB-AB"))
    result.aborted_reason = "[22] 일일 트래픽 초과"
    result.stopped_at = ("N", 37)

    outcome = await run_listing_batch(conn, CLIENT, collect=collector(result))

    assert outcome.status == "partial"
    assert outcome.resume_token == "N:37"
    assert (await run_row(conn, outcome.run_id))["resume_token"] == "N:37"


async def test_batch_keeps_collected_rows_even_when_aborted(
    conn: Conn, calls: list[str]
) -> None:
    """중단돼도 이미 받은 것은 버리지 않는다."""
    result = complete(item("T-PB-ABKEEP"))
    result.aborted_reason = "[22] 일일 트래픽 초과"
    result.stopped_at = ("N", 3)

    outcome = await run_listing_batch(conn, CLIENT, collect=collector(result))

    assert outcome.upserted == 1
    assert await status_of(conn, "T-PB-ABKEEP") == "진행"


async def test_batch_clears_the_resume_point_after_completing(
    conn: Conn, calls: list[str]
) -> None:
    aborted = complete(item("T-PB-RS"))
    aborted.aborted_reason = "[22] 일일 트래픽 초과"
    aborted.stopped_at = ("N", 37)
    await run_listing_batch(conn, CLIENT, collect=collector(aborted))

    await run_listing_batch(conn, CLIENT, collect=collector(complete(item("T-PB-RS"))))

    assert await latest_resume_token(conn, mode="full") is None


async def test_batch_reports_the_stopping_reason(conn: Conn, calls: list[str]) -> None:
    """요약을 남겨야 다음 날 왜 멈췄는지 알 수 있다 (F1.5)."""
    result = complete(item("T-PB-NOTE"))
    result.aborted_reason = "[22] 일일 트래픽 초과"
    result.stopped_at = ("N", 37)

    outcome = await run_listing_batch(conn, CLIENT, collect=collector(result))

    note = (await run_row(conn, outcome.run_id))["note"]
    assert note is not None
    assert "22" in note
