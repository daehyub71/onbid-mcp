"""tombstone 테스트 (`pytest -m db`, F4.2).

목록에서 사라진 물건을 삭제하지 않고 `종료추정` 으로 표시한다. 삭제하면 변경 이력·통계·
알림이 성립하지 않는다.

**두 가지를 틀리면 멀쩡한 데이터가 뒤집힌다.**

1. **증분 모드에서 판정** — 증분의 "응답에 없음" 은 "변경 없음" 이지 "사라짐" 이 아니다.
   매일 전체가 `종료추정` 이 된다.
2. **수집 범위를 무시** — 강남구만 수집하고 서울 전체를 대상으로 판정하면
   나머지 24개 구가 통째로 종료 처리된다.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.onbid.collector import CollectedItem, ListingFilter
from core.store.cltr import upsert_cltr
from core.store.mapping import to_cltr_row
from core.store.tombstone import TombstoneScope, mark_tombstones
from tests.conftest import Conn

pytestmark = pytest.mark.db

SEOUL = TombstoneScope(sd_nm="서울특별시")


def item(mng: str, *, sgg: str = "강남구", **overrides: Any) -> CollectedItem:
    raw: dict[str, Any] = {
        "cltrMngNo": mng, "pbctCdtnNo": "1", "onbidCltrNm": "테스트",
        "lctnSdnm": "서울특별시", "lctnSggnm": sgg, "lctnEmdNm": "개포동",
        "apslEvlAmt": 1000, "lowstBidPrcIndctCont": "500",
        "pbctStatCd": "0001", "pvctTrgtYn": "N", **overrides,
    }
    return CollectedItem(raw=raw, group="N")


async def seed(conn: Conn, *items: CollectedItem, seen_at: datetime | None = None) -> None:
    rows = [r for r in (to_cltr_row(i) for i in items) if r]
    if seen_at is not None:
        for row in rows:
            row["last_seen_at"] = seen_at
    await upsert_cltr(conn, rows)


async def status_of(conn: Conn, mng: str) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("select status from onbid_cltr where cltr_mng_no = %s", (mng,))
        found = await cur.fetchone()
        return found[0] if found else None


# ── 기본 동작 ───────────────────────────────────────────────────────────


async def test_tombstone_marks_rows_not_seen(conn: Conn) -> None:
    old = datetime.now(UTC) - timedelta(days=2)
    await seed(conn, item("T-TS-GONE"), seen_at=old)
    await seed(conn, item("T-TS-ALIVE"))

    marked = await mark_tombstones(
        conn, seen_before=datetime.now(UTC) - timedelta(hours=1), scope=SEOUL
    )

    assert marked >= 1
    assert await status_of(conn, "T-TS-GONE") == "종료추정"
    assert await status_of(conn, "T-TS-ALIVE") == "진행"


async def test_tombstone_does_not_delete(conn: Conn) -> None:
    """삭제하면 이력·통계·알림이 성립하지 않는다."""
    old = datetime.now(UTC) - timedelta(days=2)
    await seed(conn, item("T-TS-KEEP"), seen_at=old)

    await mark_tombstones(conn, seen_before=datetime.now(UTC), scope=SEOUL)

    async with conn.cursor() as cur:
        await cur.execute(
            "select count(*) from onbid_cltr where cltr_mng_no = %s", ("T-TS-KEEP",))
        found = await cur.fetchone()
    assert found is not None
    assert found[0] == 1


async def test_tombstone_is_idempotent(conn: Conn) -> None:
    """이미 표시된 행을 다시 세지 않는다 — 배치 요약이 부풀지 않아야 한다."""
    old = datetime.now(UTC) - timedelta(days=2)
    await seed(conn, item("T-TS-IDEM"), seen_at=old)
    cutoff = datetime.now(UTC)

    first = await mark_tombstones(conn, seen_before=cutoff, scope=SEOUL)
    second = await mark_tombstones(conn, seen_before=cutoff, scope=SEOUL)

    assert first >= 1
    assert second == 0


async def test_tombstone_revives_on_reappearance(conn: Conn) -> None:
    """다시 나타나면 응답의 상태로 되돌아간다 — upsert 가 status 를 덮어쓴다."""
    old = datetime.now(UTC) - timedelta(days=2)
    await seed(conn, item("T-TS-BACK"), seen_at=old)
    await mark_tombstones(conn, seen_before=datetime.now(UTC), scope=SEOUL)
    assert await status_of(conn, "T-TS-BACK") == "종료추정"

    await seed(conn, item("T-TS-BACK"))

    assert await status_of(conn, "T-TS-BACK") == "진행"


# ── 수집 범위 (핵심 함정) ───────────────────────────────────────────────


async def test_tombstone_respects_district_scope(conn: Conn) -> None:
    """강남구만 수집했으면 강남구만 판정한다.

    범위를 무시하면 나머지 24개 구가 통째로 종료 처리된다.
    """
    old = datetime.now(UTC) - timedelta(days=2)
    await seed(conn, item("T-TS-GN", sgg="강남구"), seen_at=old)
    await seed(conn, item("T-TS-SC", sgg="서초구"), seen_at=old)

    await mark_tombstones(
        conn,
        seen_before=datetime.now(UTC),
        scope=TombstoneScope(sd_nm="서울특별시", sgg_nm="강남구"),
    )

    assert await status_of(conn, "T-TS-GN") == "종료추정"
    assert await status_of(conn, "T-TS-SC") == "진행"


async def test_tombstone_scope_comes_from_the_collection_filter() -> None:
    """수집 조건과 판정 범위가 어긋나지 않도록 필터에서 직접 만든다."""
    scope = TombstoneScope.from_filter(ListingFilter(region_sgg="강남구"))
    assert scope.sd_nm == "서울특별시"
    assert scope.sgg_nm == "강남구"


async def test_tombstone_scope_without_district() -> None:
    scope = TombstoneScope.from_filter(ListingFilter())
    assert scope.sd_nm == "서울특별시"
    assert scope.sgg_nm is None


# ── 증분 모드 차단 (핵심 함정) ──────────────────────────────────────────


async def test_tombstone_refuses_incremental_filter() -> None:
    """증분 모드의 '응답에 없음' 은 '변경 없음' 이다. 판정하면 전체가 뒤집힌다."""
    incremental = ListingFilter(modified_from="20260820")

    with pytest.raises(ValueError, match="증분"):
        TombstoneScope.from_filter(incremental)


async def test_tombstone_requires_a_scope(conn: Conn) -> None:
    """빈 범위로 전체를 쓸어버리는 실수를 막는다."""
    with pytest.raises(ValueError, match="범위"):
        await mark_tombstones(
            conn, seen_before=datetime.now(UTC), scope=TombstoneScope(sd_nm=None)
        )
