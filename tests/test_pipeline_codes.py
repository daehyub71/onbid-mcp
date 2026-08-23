"""코드표 갱신 배치 테스트 (`pytest -m db`, F6.12·F7.2).

용도 3단 트리와 주소 조합을 DB 에 둔다. 조회마다 온비드에 물어보면 일일 트래픽을 검색에
쓰게 되고, 조회 지연이 외부 API 가용성에 묶인다.

**두 가지를 틀리면 조회가 조용히 반쪽이 된다.**

1. **한쪽 실패가 다른 쪽을 삼킴** — 용도와 주소는 서로 다른 엔드포인트다. 하나가 죽었다고
   나머지까지 버리면 멀쩡한 갱신을 잃는다.
2. **빈 응답을 정상 갱신으로 기록** — 장애로 0건이 와도 upsert 는 아무 일도 하지 않아
   성공처럼 보인다. `synced_at` 만 새것이 되어 "코드표는 최신" 이라 믿게 된다.
"""

from collections.abc import Callable
from typing import Any, cast

import pytest

from core.codes.address import AddressEntry
from core.codes.usage import UsageCode
from core.onbid.client import OnbidApiError, OnbidClient
from core.pipeline.codes import run_code_batch
from tests.conftest import Conn

pytestmark = pytest.mark.db

CLIENT = cast(OnbidClient, object())

USAGE = [
    UsageCode(ctgr_id="T-CP-10000", ctgr_nm="부동산", up_ctgr_id=None,
              up_ctgr_nm=None, depth=1),
    UsageCode(ctgr_id="T-CP-10300", ctgr_nm="상가용및업무용건물", up_ctgr_id="T-CP-10000",
              up_ctgr_nm="부동산", depth=2),
]
ADDRESS = [
    AddressEntry(sd_nm="테스트특별시", sgg_nm="강남구", emd_nm="개포동"),
    AddressEntry(sd_nm="테스트특별시", sgg_nm="서초구", emd_nm="반포동"),
]


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


def gives(value: Any, *, seen: list[Any] | None = None) -> Callable[..., Any]:
    async def fetch(client: OnbidClient, **kwargs: Any) -> Any:
        if seen is not None:
            seen.append(kwargs)
        return value

    return fetch


def fails(reason: str) -> Callable[..., Any]:
    async def fetch(client: OnbidClient, **kwargs: Any) -> Any:
        raise OnbidApiError(result_code="04", result_msg=reason)

    return fetch


async def usage_count(conn: Conn) -> int:
    async with conn.cursor() as cur:
        await cur.execute("select count(*) from onbid_usg_code where ctgr_id like 'T-CP-%'")
        found = await cur.fetchone()
        return int(found[0]) if found else 0


async def address_count(conn: Conn) -> int:
    async with conn.cursor() as cur:
        await cur.execute("select count(*) from onbid_addr_map where sd_nm = '테스트특별시'")
        found = await cur.fetchone()
        return int(found[0]) if found else 0


async def run_row(conn: Conn, run_id: int) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute("select * from onbid_batch_run where run_id = %s", (run_id,))
        found = await cur.fetchone()
        assert found is not None
        assert cur.description is not None
        return dict(zip([c.name for c in cur.description], found, strict=True))


# ── 정상 갱신 ───────────────────────────────────────────────────────────


async def test_code_batch_loads_both_sources(conn: Conn, calls: list[str]) -> None:
    outcome = await run_code_batch(
        conn, CLIENT, fetch_usage=gives(USAGE), fetch_address=gives(ADDRESS))

    assert outcome.status == "ok"
    assert (await usage_count(conn), await address_count(conn)) == (2, 2)


async def test_code_batch_records_counts_in_meta(conn: Conn, calls: list[str]) -> None:
    outcome = await run_code_batch(
        conn, CLIENT, fetch_usage=gives(USAGE), fetch_address=gives(ADDRESS))

    row = await run_row(conn, outcome.run_id)
    assert row["mode"] == "codes"
    assert row["collected"] == 4
    assert row["upserted"] == 4


async def test_code_batch_is_idempotent(conn: Conn, calls: list[str]) -> None:
    """주간 갱신이 코드표를 부풀리면 안 된다 (AC2)."""
    await run_code_batch(conn, CLIENT, fetch_usage=gives(USAGE), fetch_address=gives(ADDRESS))
    await run_code_batch(conn, CLIENT, fetch_usage=gives(USAGE), fetch_address=gives(ADDRESS))

    assert (await usage_count(conn), await address_count(conn)) == (2, 2)


async def test_code_batch_scopes_addresses_to_the_collection_region(
    conn: Conn, calls: list[str]
) -> None:
    """수집 범위 밖 지역을 노출하면 조회가 0건인 지역을 추천한다."""
    seen: list[Any] = []

    await run_code_batch(conn, CLIENT, region_sd="서울특별시",
                         fetch_usage=gives(USAGE),
                         fetch_address=gives(ADDRESS, seen=seen))

    assert seen[0]["sd_nm"] == "서울특별시"


# ── 실패 격리 (핵심 함정 ①) ────────────────────────────────────────────


async def test_code_batch_keeps_address_when_usage_fails(
    conn: Conn, calls: list[str]
) -> None:
    """용도가 죽었다고 멀쩡한 주소 갱신을 버리지 않는다."""
    outcome = await run_code_batch(
        conn, CLIENT, fetch_usage=fails("서버 오류"), fetch_address=gives(ADDRESS))

    assert outcome.status == "partial"
    assert outcome.usage.error is not None
    assert await address_count(conn) == 2


async def test_code_batch_keeps_usage_when_address_fails(
    conn: Conn, calls: list[str]
) -> None:
    outcome = await run_code_batch(
        conn, CLIENT, fetch_usage=gives(USAGE), fetch_address=fails("서버 오류"))

    assert outcome.status == "partial"
    assert await usage_count(conn) == 2


async def test_code_batch_fails_when_both_sources_fail(
    conn: Conn, calls: list[str]
) -> None:
    outcome = await run_code_batch(
        conn, CLIENT, fetch_usage=fails("서버 오류"), fetch_address=fails("서버 오류"))

    assert outcome.status == "failed"
    assert (await run_row(conn, outcome.run_id))["status"] == "failed"


async def test_code_batch_records_the_reason(conn: Conn, calls: list[str]) -> None:
    """무엇이 왜 실패했는지 남지 않으면 다음 주에 같은 일을 겪는다."""
    outcome = await run_code_batch(
        conn, CLIENT, fetch_usage=fails("쿼터 초과"), fetch_address=gives(ADDRESS))

    note = (await run_row(conn, outcome.run_id))["note"]
    assert note is not None
    assert "usage" in note


# ── 빈 응답 (핵심 함정 ②) ──────────────────────────────────────────────


async def test_code_batch_treats_an_empty_usage_tree_as_a_failure(
    conn: Conn, calls: list[str]
) -> None:
    """빈 응답에 upsert 는 아무 일도 하지 않아 성공처럼 보인다 — 장애를 성공으로 남기지 않는다."""
    outcome = await run_code_batch(
        conn, CLIENT, fetch_usage=gives([]), fetch_address=gives(ADDRESS))

    assert outcome.status == "partial"
    assert outcome.usage.error is not None


async def test_code_batch_treats_an_empty_address_list_as_a_failure(
    conn: Conn, calls: list[str]
) -> None:
    outcome = await run_code_batch(
        conn, CLIENT, fetch_usage=gives(USAGE), fetch_address=gives([]))

    assert outcome.status == "partial"
    assert outcome.address.error is not None


async def test_code_batch_does_not_delete_on_empty_response(
    conn: Conn, calls: list[str]
) -> None:
    """빈 응답이 와도 기존 코드표는 그대로다 — 갱신 중 조회가 빈 표를 보면 안 된다."""
    await run_code_batch(conn, CLIENT, fetch_usage=gives(USAGE), fetch_address=gives(ADDRESS))

    await run_code_batch(conn, CLIENT, fetch_usage=gives([]), fetch_address=gives([]))

    assert (await usage_count(conn), await address_count(conn)) == (2, 2)


# ── 커밋 경계 (F4.16) ───────────────────────────────────────────────────


async def test_code_batch_commits_meta_before_fetching(
    conn: Conn, calls: list[str]
) -> None:
    seen: list[int] = []

    async def watching(client: OnbidClient, **kwargs: Any) -> Any:
        seen.append(len(calls))
        return USAGE

    await run_code_batch(conn, CLIENT, fetch_usage=watching, fetch_address=gives(ADDRESS))

    assert seen == [1]


async def test_code_batch_uses_a_single_data_commit(conn: Conn, calls: list[str]) -> None:
    await run_code_batch(conn, CLIENT, fetch_usage=gives(USAGE), fetch_address=gives(ADDRESS))

    assert calls == ["commit", "commit", "commit"]


async def test_code_batch_propagates_database_errors(
    conn: Conn, calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 실패는 기록하고 넘어가지만, DB 실패는 감추지 않는다."""

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("적재 실패")

    monkeypatch.setattr("core.pipeline.codes.upsert_usage_codes", boom)

    with pytest.raises(RuntimeError, match="적재 실패"):
        await run_code_batch(conn, CLIENT, fetch_usage=gives(USAGE),
                             fetch_address=gives(ADDRESS))

    assert "rollback" in calls
