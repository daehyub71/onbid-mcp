"""배치 실행 메타 테스트 (`pytest -m db`, F4.6·N2.2).

배치가 성공했는지, 몇 건을 처리했는지, **어디서 끊겼는지** 를 남긴다.
쿼터(`22`)로 중단된 배치는 다음 실행이 `resume_token` 부터 이어받아야 한다 — 처음부터
다시 돌면 남은 쿼터를 이미 처리한 구간에 다 쓴다.
"""

from typing import Any

import pytest

from core.store.batch_run import (
    BatchCounts,
    finish_run,
    latest_resume_token,
    start_run,
)
from tests.conftest import Conn

pytestmark = pytest.mark.db


async def run_row(conn: Conn, run_id: int) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute("select * from onbid_batch_run where run_id = %s", (run_id,))
        found = await cur.fetchone()
        assert found is not None
        assert cur.description is not None
        return dict(zip([c.name for c in cur.description], found, strict=True))


# ── 시작·종료 ───────────────────────────────────────────────────────────


async def test_start_run_opens_an_unfinished_row(conn: Conn) -> None:
    """시작 시점에 열어둬야 배치가 죽어도 흔적이 남는다."""
    run_id = await start_run(conn, mode="full")

    row = await run_row(conn, run_id)
    assert row["mode"] == "full"
    assert row["started_at"] is not None
    assert row["finished_at"] is None
    assert row["status"] is None


async def test_finish_run_records_counts(conn: Conn) -> None:
    run_id = await start_run(conn, mode="full")

    await finish_run(
        conn, run_id, status="ok",
        counts=BatchCounts(collected=6910, upserted=6910, tombstoned=12,
                           geocode_ok=6500, geocode_approx=300, geocode_failed=110),
    )

    row = await run_row(conn, run_id)
    assert row["status"] == "ok"
    assert row["finished_at"] is not None
    assert (row["collected"], row["upserted"], row["tombstoned"]) == (6910, 6910, 12)
    assert (row["geocode_ok"], row["geocode_approx"], row["geocode_failed"]) == (6500, 300, 110)


async def test_finish_run_without_counts(conn: Conn) -> None:
    """수집 전에 죽은 배치도 실패로 닫을 수 있어야 한다."""
    run_id = await start_run(conn, mode="delta")

    await finish_run(conn, run_id, status="failed", note="인증키 오류")

    row = await run_row(conn, run_id)
    assert row["status"] == "failed"
    assert row["note"] == "인증키 오류"
    assert row["collected"] is None


async def test_finish_run_rejects_unknown_run(conn: Conn) -> None:
    with pytest.raises(LookupError):
        await finish_run(conn, -1, status="ok")


# ── 값 검증 ─────────────────────────────────────────────────────────────


async def test_start_run_rejects_unknown_mode(conn: Conn) -> None:
    """`full` 과 `delta` 는 tombstone 판정 여부를 가른다 — 오타가 조용히 통과하면 안 된다."""
    with pytest.raises(ValueError, match="mode"):
        await start_run(conn, mode="fulll")


async def test_finish_run_rejects_unknown_status(conn: Conn) -> None:
    run_id = await start_run(conn, mode="full")
    with pytest.raises(ValueError, match="status"):
        await finish_run(conn, run_id, status="done")


# ── 재개 (N2.2) ─────────────────────────────────────────────────────────


async def test_partial_run_keeps_resume_token(conn: Conn) -> None:
    run_id = await start_run(conn, mode="full")

    await finish_run(conn, run_id, status="partial", resume_token="N:37")

    assert (await run_row(conn, run_id))["resume_token"] == "N:37"


async def test_latest_resume_token_returns_the_stopping_point(conn: Conn) -> None:
    await finish_run(conn, await start_run(conn, mode="full"), status="ok")
    await finish_run(conn, await start_run(conn, mode="full"), status="partial",
                     resume_token="N:37")

    assert await latest_resume_token(conn, mode="full") == "N:37"


async def test_latest_resume_token_is_none_after_a_clean_run(conn: Conn) -> None:
    """완주한 다음 실행이 이전 재개 지점으로 되돌아가면 안 된다."""
    await finish_run(conn, await start_run(conn, mode="full"), status="partial",
                     resume_token="N:37")
    await finish_run(conn, await start_run(conn, mode="full"), status="ok")

    assert await latest_resume_token(conn, mode="full") is None


async def test_ok_run_clears_the_resume_token(conn: Conn) -> None:
    """완주했는데 토큰이 남아 있으면 다음 실행이 중간부터 시작한다."""
    run_id = await start_run(conn, mode="full")

    await finish_run(conn, run_id, status="ok", resume_token="N:37")

    assert (await run_row(conn, run_id))["resume_token"] is None


async def test_resume_token_does_not_cross_modes(conn: Conn) -> None:
    """전량과 증분은 순회 방식이 달라 재개 지점을 공유할 수 없다."""
    await finish_run(conn, await start_run(conn, mode="full"), status="partial",
                     resume_token="N:37")

    assert await latest_resume_token(conn, mode="delta") is None


async def test_latest_resume_token_ignores_running_batch(conn: Conn) -> None:
    """아직 도는 배치는 재개 지점이 아니다 — 방금 연 행을 자기가 읽으면 안 된다."""
    await finish_run(conn, await start_run(conn, mode="full"), status="partial",
                     resume_token="N:37")
    await start_run(conn, mode="full")

    assert await latest_resume_token(conn, mode="full") == "N:37"
