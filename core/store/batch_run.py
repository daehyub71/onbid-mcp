"""배치 실행 메타 (F4.6·N2.2).

배치가 성공했는지, 몇 건을 처리했는지, **어디서 끊겼는지** 를 남긴다.

끊긴 지점(`resume_token`)이 특히 중요하다. 온비드 일일 트래픽은 유한하고 쿼터 소진(`22`)은
전체 중단 사유다 (F1.13). 다음 실행이 처음부터 다시 돌면 남은 쿼터를 **이미 처리한 구간**에
다 쓰고 또 같은 자리에서 멈춘다 — 영원히 진도가 나가지 않는다.

행은 **시작할 때** 연다. 끝날 때 한 번에 쓰면 배치가 죽었을 때 아무 흔적도 남지 않는다.
"""

import logging
from dataclasses import asdict, dataclass
from typing import Any, Final

import psycopg

logger = logging.getLogger(__name__)

TABLE: Final = "onbid_batch_run"

MODES: Final = frozenset({"full", "delta", "rounds", "codes", "geocode"})
"""전량·증분·회차.

`full`/`delta` 는 tombstone 판정 여부가 갈리므로 오타를 통과시키지 않는다 (F4.2).
`rounds`(회차 이력)·`codes`(코드표)·`geocode`(좌표)는 별개 배치다 — 물건 배치와
**재개 지점을 공유하지 않도록** 모드를 나눈다.
"""

STATUSES: Final = frozenset({"ok", "partial", "failed"})
"""``partial`` 은 재개 지점이 있는 중단, ``failed`` 는 수집조차 못 한 실패다."""

RESUMABLE_STATUSES: Final = frozenset({"partial", "failed"})
"""완주(``ok``)한 실행은 재개 지점을 남기지 않는다."""


@dataclass(frozen=True, slots=True)
class BatchCounts:
    """배치 처리 건수.

    Attributes:
        collected: 수집한 물건 수.
        upserted: 적재한 물건 수.
        tombstoned: `종료추정` 으로 표시한 수.
        geocode_ok: 정확 좌표를 얻은 수.
        geocode_approx: 근사 좌표로 대체한 수.
        geocode_failed: 좌표를 얻지 못한 수.
    """

    collected: int | None = None
    upserted: int | None = None
    tombstoned: int | None = None
    geocode_ok: int | None = None
    geocode_approx: int | None = None
    geocode_failed: int | None = None


async def start_run(conn: psycopg.AsyncConnection[Any], *, mode: str) -> int:
    """배치 행을 열고 식별자를 돌려준다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        mode: ``full`` 또는 ``delta``.

    Returns:
        `run_id`.

    Raises:
        ValueError: 알 수 없는 mode 일 때.
    """
    if mode not in MODES:
        raise ValueError(f"알 수 없는 배치 mode: {mode!r} (가능: {sorted(MODES)})")

    async with conn.cursor() as cur:
        await cur.execute(
            f"insert into {TABLE} (mode) values (%s) returning run_id", (mode,)
        )
        found = await cur.fetchone()

    assert found is not None  # returning 절은 항상 한 행을 준다
    run_id = int(found[0])
    logger.info("배치 시작: run_id=%d mode=%s", run_id, mode)
    return run_id


async def finish_run(
    conn: psycopg.AsyncConnection[Any],
    run_id: int,
    *,
    status: str,
    counts: BatchCounts | None = None,
    resume_token: str | None = None,
    note: str | None = None,
) -> None:
    """배치 행을 닫는다.

    완주(``ok``)했다면 재개 지점을 **지운다** — 남겨두면 다음 실행이 이유 없이 중간부터
    시작한다.

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        run_id: `start_run` 이 돌려준 식별자.
        status: ``ok`` | ``partial`` | ``failed``.
        counts: 처리 건수. 수집 전에 죽었다면 생략한다.
        resume_token: 중단 지점. ``ok`` 일 때는 무시된다.
        note: 사람이 읽을 메모 — 중단 사유 등.

    Raises:
        ValueError: 알 수 없는 status 일 때.
        LookupError: 해당 배치 행이 없을 때.
    """
    if status not in STATUSES:
        raise ValueError(f"알 수 없는 배치 status: {status!r} (가능: {sorted(STATUSES)})")

    params: dict[str, Any] = {
        **asdict(counts if counts is not None else BatchCounts()),
        "run_id": run_id,
        "status": status,
        "note": note,
        "resume_token": resume_token if status in RESUMABLE_STATUSES else None,
    }
    assignments = ", ".join(
        f"{column} = %({column})s" for column in params if column != "run_id"
    )

    async with conn.cursor() as cur:
        await cur.execute(
            f"update {TABLE} set finished_at = now(), {assignments} where run_id = %(run_id)s",
            params,
        )
        if cur.rowcount == 0:
            raise LookupError(f"배치 행이 없다: run_id={run_id}")

    logger.info("배치 종료: run_id=%d status=%s", run_id, status)


async def latest_resume_token(
    conn: psycopg.AsyncConnection[Any], *, mode: str
) -> str | None:
    """이 모드에서 **마지막으로 끝난** 배치의 재개 지점.

    아직 도는 배치는 보지 않는다 — 자기가 방금 연 행을 읽으면 안 된다.
    직전 실행이 완주했다면 None 이다.

    Args:
        conn: 열린 연결.
        mode: ``full`` 또는 ``delta``.

    Returns:
        재개 지점. 없으면 None.

    Raises:
        ValueError: 알 수 없는 mode 일 때.
    """
    if mode not in MODES:
        raise ValueError(f"알 수 없는 배치 mode: {mode!r} (가능: {sorted(MODES)})")

    async with conn.cursor() as cur:
        await cur.execute(
            f"select resume_token from {TABLE} "
            "where mode = %s and finished_at is not null "
            "order by finished_at desc, run_id desc limit 1",
            (mode,),
        )
        found = await cur.fetchone()

    token = found[0] if found else None
    return str(token) if token else None
