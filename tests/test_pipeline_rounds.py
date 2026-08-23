"""회차 이력 배치 테스트 (`pytest -m db`, F1.7·F1.11·F1.16).

입찰정보 API 는 **일일 트래픽 1,000건**인데 대상은 1,100여 건이다. 한 번에 다 돌 수 없으므로
**오래 안 본 것부터** 예산만큼 처리하고 며칠에 걸쳐 한 바퀴를 돈다.

**시도 시각을 남기는 것이 이 배치의 핵심이다.** 남기지 않으면 매일 같은 앞머리 1,000건만
호출하고 뒤쪽은 영원히 갱신되지 않는다. 성공한 것만 남겨도 안 된다 — 이력이 없는 물건(`03`)을
매일 다시 부르게 된다.

`test_pipeline_batch` 와 같은 이유로 커밋을 기록·무력화한 채 실 SQL 을 돌린다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final, cast

import pytest

from core.onbid.bidinfo import BidCollectResult, BidDetail, BidFailure, BidTarget
from core.onbid.client import OnbidClient
from core.onbid.collector import CollectedItem
from core.pipeline.rounds import run_round_batch
from core.store.batch_run import latest_resume_token
from core.store.cltr import upsert_cltr
from core.store.mapping import to_cltr_row
from tests.conftest import Conn, load_fixture

pytestmark = pytest.mark.db

CLIENT = cast(OnbidClient, object())

#: 실데이터 격리. 첫 실적재 이후 대상 선별이 실제 물건 1,000여 건을 반환하므로
#: `min_fail_count` 로 후보를 갈라낸다 (`test_store_bid_round` 와 같은 이유).
TEST_FAIL_CNT: Final = 9999
TEST_MIN_FAIL: Final = 9000


@pytest.fixture
def calls(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """커밋·롤백을 기록만 하고 수행하지 않는다 (공유 Supabase)."""
    recorded: list[str] = []

    async def fake_commit() -> None:
        recorded.append("commit")

    async def fake_rollback() -> None:
        recorded.append("rollback")

    monkeypatch.setattr(conn, "commit", fake_commit)
    monkeypatch.setattr(conn, "rollback", fake_rollback)
    return recorded


async def seed(conn: Conn, mng: str, **overrides: Any) -> None:
    raw: dict[str, Any] = {
        "cltrMngNo": mng, "pbctCdtnNo": "1", "onbidCltrNm": "테스트",
        "lctnSdnm": "서울특별시", "lctnSggnm": "강남구", "lctnEmdNm": "개포동",
        "apslEvlAmt": 1000, "lowstBidPrcIndctCont": "500",
        "usbdNft": TEST_FAIL_CNT, "pbctStatCd": "0001", "pvctTrgtYn": "N", **overrides,
    }
    row = to_cltr_row(CollectedItem(raw=raw, group="N"))
    assert row is not None
    await upsert_cltr(conn, [row])


def real_rounds() -> dict[str, Any]:
    detail = load_fixture("bid_detail_usbd2")["body"]["items"]["item"]
    payload: dict[str, Any] = detail[0] if isinstance(detail, list) else detail
    return payload


def collector(result: BidCollectResult, *, seen: list[Any] | None = None) -> Callable[..., Any]:
    """`collect_bid_details` 자리에 끼울 대역."""

    async def fake_collect(
        client: OnbidClient, targets: list[BidTarget], **kwargs: Any
    ) -> BidCollectResult:
        if seen is not None:
            seen.append((targets, kwargs))
        result.details = [BidDetail(target=t, raw=real_rounds()) for t in targets]
        return result

    return fake_collect


def empty_collector() -> Callable[..., Any]:
    async def fake_collect(
        client: OnbidClient, targets: list[BidTarget], **kwargs: Any
    ) -> BidCollectResult:
        return BidCollectResult(no_data_targets=list(targets))

    return fake_collect


async def attempt_of(conn: Conn, mng: str) -> datetime | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "select bid_round_synced_at from onbid_cltr where cltr_mng_no = %s", (mng,))
        found = await cur.fetchone()
        return found[0] if found else None


async def round_count(conn: Conn, mng: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "select count(*) from onbid_cltr_bid_round where cltr_mng_no = %s", (mng,))
        found = await cur.fetchone()
        return int(found[0]) if found else 0


async def run_row(conn: Conn, run_id: int) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute("select * from onbid_batch_run where run_id = %s", (run_id,))
        found = await cur.fetchone()
        assert found is not None
        assert cur.description is not None
        return dict(zip([c.name for c in cur.description], found, strict=True))


# ── 적재 ────────────────────────────────────────────────────────────────


async def test_round_batch_loads_history(conn: Conn, calls: list[str]) -> None:
    await seed(conn, "T-RB-LOAD")

    outcome = await run_round_batch(
        conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5,
        collect=collector(BidCollectResult(budget=5)))

    assert outcome.rounds > 0
    assert await round_count(conn, "T-RB-LOAD") > 0


async def test_round_batch_records_counts_in_meta(conn: Conn, calls: list[str]) -> None:
    await seed(conn, "T-RB-META")

    outcome = await run_round_batch(
        conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5,
        collect=collector(BidCollectResult(budget=5)))

    row = await run_row(conn, outcome.run_id)
    assert row["mode"] == "rounds"
    assert row["status"] == "ok"
    assert row["collected"] == outcome.collected


async def test_round_batch_does_nothing_without_targets(
    conn: Conn, calls: list[str]
) -> None:
    """대상이 없으면 호출도 하지 않는다 — 빈 호출로 쿼터를 태우지 않는다."""
    called: list[Any] = []

    outcome = await run_round_batch(
        conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=0,
        collect=collector(BidCollectResult(), seen=called))

    assert outcome.targets == 0
    assert called == []


# ── 예산 롤링 (F1.16) ───────────────────────────────────────────────────


async def test_round_batch_marks_attempts(conn: Conn, calls: list[str]) -> None:
    """시도를 남기지 않으면 매일 같은 앞머리만 호출한다."""
    await seed(conn, "T-RB-MARK")

    await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5,
                          collect=collector(BidCollectResult(budget=5)))

    assert await attempt_of(conn, "T-RB-MARK") is not None


async def test_round_batch_marks_items_without_history(
    conn: Conn, calls: list[str]
) -> None:
    """이력이 없어 `03` 이 온 물건도 시도로 친다 — 아니면 매일 다시 부른다."""
    await seed(conn, "T-RB-NODATA")

    outcome = await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL,
                                    budget=5, collect=empty_collector())

    assert outcome.rounds == 0
    assert await attempt_of(conn, "T-RB-NODATA") is not None


async def test_round_batch_marks_failed_items(conn: Conn, calls: list[str]) -> None:
    """실패한 물건도 시도로 친다 — 고장난 한 건이 매일 예산을 선점하면 안 된다."""
    await seed(conn, "T-RB-FAILED")

    async def failing(
        client: OnbidClient, targets: list[BidTarget], **kwargs: Any
    ) -> BidCollectResult:
        return BidCollectResult(
            failed=[BidFailure(target=t, reason="[04] 서버 오류") for t in targets])

    outcome = await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL,
                                    budget=5, collect=failing)

    assert outcome.failed == 1
    assert await attempt_of(conn, "T-RB-FAILED") is not None
    assert outcome.status == "partial"


async def test_round_batch_does_not_mark_unattempted_items(
    conn: Conn, calls: list[str]
) -> None:
    """예산이 끊겨 못 부른 물건은 시도가 아니다 — 표시하면 순번이 뒤로 밀린다."""
    await seed(conn, "T-RB-SKIP")

    async def aborting(
        client: OnbidClient, targets: list[BidTarget], **kwargs: Any
    ) -> BidCollectResult:
        return BidCollectResult(not_attempted=list(targets),
                                aborted_reason="[22] 일일 트래픽 초과")

    await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5, collect=aborting)

    assert await attempt_of(conn, "T-RB-SKIP") is None


async def test_round_batch_passes_the_budget_to_the_collector(
    conn: Conn, calls: list[str]
) -> None:
    await seed(conn, "T-RB-BUDGET")
    seen: list[Any] = []

    await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=7,
                          collect=collector(BidCollectResult(budget=7), seen=seen))

    assert seen[0][1]["budget"] == 7


async def test_round_batch_limits_targets_to_the_budget(
    conn: Conn, calls: list[str]
) -> None:
    """예산보다 많이 뽑으면 대역폭 계산이 어긋난다."""
    for i in range(4):
        await seed(conn, f"T-RB-LIM{i}")
    seen: list[Any] = []

    await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=2,
                          collect=collector(BidCollectResult(budget=2), seen=seen))

    assert len(seen[0][0]) == 2


# ── 중단·상태 ───────────────────────────────────────────────────────────


async def test_round_batch_is_partial_when_aborted(conn: Conn, calls: list[str]) -> None:
    await seed(conn, "T-RB-AB")

    async def aborting(
        client: OnbidClient, targets: list[BidTarget], **kwargs: Any
    ) -> BidCollectResult:
        return BidCollectResult(not_attempted=list(targets),
                                aborted_reason="[22] 일일 트래픽 초과")

    outcome = await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL,
                                    budget=5, collect=aborting)

    assert outcome.status == "partial"
    assert outcome.carried_over == 1


async def test_round_batch_leaves_no_resume_token(conn: Conn, calls: list[str]) -> None:
    """회차 배치는 토큰으로 재개하지 않는다 — 시도 시각이 그 역할을 한다 (F1.16)."""
    await seed(conn, "T-RB-NOTOK")

    async def aborting(
        client: OnbidClient, targets: list[BidTarget], **kwargs: Any
    ) -> BidCollectResult:
        return BidCollectResult(not_attempted=list(targets),
                                aborted_reason="[22] 일일 트래픽 초과")

    await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5, collect=aborting)

    assert await latest_resume_token(conn, mode="rounds") is None


async def test_round_batch_does_not_touch_listing_resume_token(
    conn: Conn, calls: list[str]
) -> None:
    """물건 배치의 재개 지점과 섞이면 전량 수집이 엉뚱한 데서 재개된다."""
    await seed(conn, "T-RB-SEP")

    await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5,
                          collect=collector(BidCollectResult(budget=5)))

    assert await latest_resume_token(conn, mode="full") is None


# ── 커밋 경계 (F4.16) ───────────────────────────────────────────────────


async def test_round_batch_commits_meta_before_calling(
    conn: Conn, calls: list[str]
) -> None:
    await seed(conn, "T-RB-CM")
    seen: list[int] = []

    async def watching(
        client: OnbidClient, targets: list[BidTarget], **kwargs: Any
    ) -> BidCollectResult:
        seen.append(len(calls))
        return BidCollectResult(no_data_targets=list(targets))

    await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5, collect=watching)

    assert seen == [1]


async def test_round_batch_rolls_back_on_failure(
    conn: Conn, calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """적재가 실패하면 시도 기록도 함께 되돌린다 — 부르지 않은 것처럼 남아야 한다."""
    await seed(conn, "T-RB-BOOM")

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("적재 실패")

    monkeypatch.setattr("core.pipeline.rounds.upsert_bid_rounds", boom)

    with pytest.raises(RuntimeError, match="적재 실패"):
        await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5,
                              collect=collector(BidCollectResult(budget=5)))

    assert "rollback" in calls


async def test_round_batch_uses_a_single_data_commit(
    conn: Conn, calls: list[str]
) -> None:
    await seed(conn, "T-RB-ONE")

    await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5,
                          collect=collector(BidCollectResult(budget=5)))

    assert calls == ["commit", "commit", "commit"]


async def test_round_batch_stamps_attempts_with_the_batch_time(
    conn: Conn, calls: list[str]
) -> None:
    """호출마다 now() 를 부르면 같은 배치 안에서 순번이 갈린다."""
    await seed(conn, "T-RB-TIME1")
    await seed(conn, "T-RB-TIME2")
    fixed = datetime(2026, 8, 23, 4, 0, tzinfo=UTC)

    await run_round_batch(conn, CLIENT, min_fail_count=TEST_MIN_FAIL, budget=5,
                          now_fn=lambda: fixed,
                          collect=collector(BidCollectResult(budget=5)))

    assert await attempt_of(conn, "T-RB-TIME1") == await attempt_of(conn, "T-RB-TIME2")
