"""지오코딩 패스 테스트 (`pytest -m db`, F3.3·F3.5·PLAN §4.1①).

**수집과 분리된 패스다.** 붙여 두면 카카오 장애 하나가 온비드 수집까지 멈춰 세운다.

이 패스가 지켜야 할 것 셋:

1. **호출 상한** — 카카오 앱을 다른 프로젝트와 공유하므로 예산을 넘기면 남의 서비스가 막힌다
2. **쿼터 소진 시 즉시 중단** — 남은 대상은 다음 실행으로 넘긴다 (F3.3)
3. **캐시 먼저** — 실측상 6,902건의 고유 주소가 801개뿐이라 절약이 크다 (F3.2)
"""

from typing import Any, cast

import pytest

from core.geocoder.kakao import KakaoClient, KakaoPoint, KakaoQuotaExceededError
from core.onbid.collector import CollectedItem
from core.pipeline.geocode import run_geocode_batch
from core.store.cltr import upsert_cltr
from core.store.mapping import to_cltr_row
from tests.conftest import Conn

pytestmark = pytest.mark.db

REGION = "테스트특별시"


@pytest.fixture
def calls(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []

    async def fake_commit() -> None:
        recorded.append("commit")

    async def fake_rollback() -> None:
        recorded.append("rollback")

    monkeypatch.setattr(conn, "commit", fake_commit)
    monkeypatch.setattr(conn, "rollback", fake_rollback)
    return recorded


class FakeKakao:
    """카카오 대역. 호출 수를 세고 예산을 흉내 낸다."""

    def __init__(self, *, quota_after: int | None = None, found: bool = True) -> None:
        self.call_count = 0
        self.queries: list[str] = []
        self._quota_after = quota_after
        self._found = found

    async def search(self, address: str) -> KakaoPoint | None:
        self.call_count += 1
        self.queries.append(address)
        if self._quota_after is not None and self.call_count > self._quota_after:
            raise KakaoQuotaExceededError("쿼터 소진")
        if not self._found:
            return None
        return KakaoPoint(lat=37.5, lng=127.05, address_name=address,
                          address_type="REGION_ADDR")


async def seed(conn: Conn, mng: str, *, emd: str = "개포동", **overrides: Any) -> None:
    raw: dict[str, Any] = {
        "cltrMngNo": mng, "pbctCdtnNo": "1", "onbidCltrNm": "테스트",
        "lctnSdnm": REGION, "lctnSggnm": "강남구", "lctnEmdNm": emd,
        "apslEvlAmt": 1000, "lowstBidPrcIndctCont": "500",
        "usbdNft": 1, "pbctStatCd": "0001", "pvctTrgtYn": "N", **overrides,
    }
    row = to_cltr_row(CollectedItem(raw=raw, group="N"))
    assert row is not None
    await upsert_cltr(conn, [row])


async def located(conn: Conn, mng: str) -> tuple[Any, ...]:
    async with conn.cursor() as cur:
        await cur.execute(
            "select lat, geocode_status, geocode_level from onbid_cltr "
            "where cltr_mng_no = %s", (mng,))
        found = await cur.fetchone()
        assert found is not None
        return found


async def run_row(conn: Conn, run_id: int) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute("select * from onbid_batch_run where run_id = %s", (run_id,))
        found = await cur.fetchone()
        assert found is not None
        assert cur.description is not None
        return dict(zip([c.name for c in cur.description], found, strict=True))


def run(conn: Conn, kakao: FakeKakao, **kwargs: Any) -> Any:
    return run_geocode_batch(conn, cast(KakaoClient, kakao), sd_nm=REGION, **kwargs)


# ── 기본 동작 ──────────────────────────────────────────────────────────


async def test_pass_fills_coordinates(conn: Conn, calls: list[str]) -> None:
    await seed(conn, "T-GP-1")

    outcome = await run(conn, FakeKakao(), budget=10)

    assert outcome.located >= 1
    lat, status, level = await located(conn, "T-GP-1")
    assert lat is not None and status == "ok" and level == "jibun"


async def test_pass_records_meta(conn: Conn, calls: list[str]) -> None:
    await seed(conn, "T-GP-META")

    outcome = await run(conn, FakeKakao(), budget=10)

    row = await run_row(conn, outcome.run_id)
    assert row["mode"] == "geocode"
    assert row["status"] == "ok"
    assert row["geocode_ok"] == outcome.located


async def test_pass_does_nothing_without_targets(conn: Conn, calls: list[str]) -> None:
    kakao = FakeKakao()

    outcome = await run(conn, kakao, budget=10, sgg_nm="없는구")

    assert outcome.targets == 0
    assert kakao.call_count == 0


# ── 호출 상한 (F3.5) ───────────────────────────────────────────────────


async def test_budget_caps_the_number_of_targets(conn: Conn, calls: list[str]) -> None:
    """공유 앱이라 예산을 넘기면 남의 프로젝트가 막힌다."""
    for i in range(5):
        await seed(conn, f"T-GP-CAP{i}", emd=f"동{i}")
    kakao = FakeKakao()

    outcome = await run(conn, kakao, budget=2)

    assert outcome.targets == 2
    assert kakao.call_count <= 2


async def test_budget_of_zero_makes_no_call(conn: Conn, calls: list[str]) -> None:
    await seed(conn, "T-GP-ZERO")
    kakao = FakeKakao()

    outcome = await run(conn, kakao, budget=0)

    assert kakao.call_count == 0
    assert outcome.targets == 0


async def test_usage_is_reported(conn: Conn, calls: list[str]) -> None:
    """얼마나 썼는지 남지 않으면 공유 앱의 사용량을 알 수 없다 (F3.5)."""
    await seed(conn, "T-GP-USAGE")
    kakao = FakeKakao()

    outcome = await run(conn, kakao, budget=10)

    assert outcome.api_calls == kakao.call_count


# ── 캐시 (F3.2) ────────────────────────────────────────────────────────


async def test_same_address_is_asked_once(conn: Conn, calls: list[str]) -> None:
    """한 지번에 여러 물건이 걸려 있다 — 실측 6,902건의 고유 주소가 801개뿐이다."""
    for i in range(3):
        await seed(conn, f"T-GP-SAME{i}")   # 같은 읍면동·같은 주소
    kakao = FakeKakao()

    await run(conn, kakao, budget=10)

    assert kakao.call_count == 1


async def test_results_are_cached_for_later_runs(conn: Conn, calls: list[str]) -> None:
    await seed(conn, "T-GP-CACHE")
    await run(conn, FakeKakao(), budget=10)

    async with conn.cursor() as cur:
        await cur.execute(
            "select count(*) from onbid_geocode_cache where addr like %s",
            (f"{REGION}%",))
        found = await cur.fetchone()
    assert found is not None and found[0] >= 1


# ── 쿼터 중단 (F3.3) ───────────────────────────────────────────────────


async def test_quota_exhaustion_stops_the_pass(conn: Conn, calls: list[str]) -> None:
    """계속 두드리면 남의 프로젝트까지 막는다."""
    for i in range(4):
        await seed(conn, f"T-GP-Q{i}", emd=f"쿼터동{i}")

    outcome = await run(conn, FakeKakao(quota_after=2), budget=10)

    assert outcome.status == "partial"
    assert outcome.carried_over >= 1


async def test_results_before_the_quota_are_kept(conn: Conn, calls: list[str]) -> None:
    """중단됐다고 이미 얻은 좌표를 버리면 그만큼 쿼터를 버리는 셈이다."""
    for i in range(4):
        await seed(conn, f"T-GP-KEEP{i}", emd=f"보존동{i}")

    outcome = await run(conn, FakeKakao(quota_after=2), budget=10)

    assert outcome.located >= 1


# ── 실패 기록 ──────────────────────────────────────────────────────────


async def test_unresolved_targets_are_marked_failed(conn: Conn, calls: list[str]) -> None:
    await seed(conn, "T-GP-FAIL")

    outcome = await run(conn, FakeKakao(found=False), budget=10)

    _lat, status, _level = await located(conn, "T-GP-FAIL")
    assert status == "failed"
    assert outcome.failed >= 1


# ── 커밋 경계 (F4.16) ──────────────────────────────────────────────────


async def test_pass_commits_meta_before_calling(conn: Conn, calls: list[str]) -> None:
    await seed(conn, "T-GP-COMMIT")
    kakao = FakeKakao()

    await run(conn, kakao, budget=10)

    assert calls == ["commit", "commit", "commit"]
