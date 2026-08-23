"""좌표 적재 테스트 (`pytest -m db`, F3.6).

지오코딩은 **별도 패스**로 돈다 (PLAN §4.1①). 수집과 붙여 두면 카카오 장애 하나가 온비드
수집까지 멈춰 세우고, 좌표만 다시 채우고 싶을 때 전량을 다시 받아야 한다.

대상은 `lat is null` 인 행이다 — 부분 인덱스가 이 조건으로 걸려 있다.
"""

from typing import Any

import pytest

from core.geocoder.resolver import GeocodeResult
from core.onbid.collector import CollectedItem
from core.store.cltr import upsert_cltr
from core.store.geocode import select_geocode_targets, update_geocode
from core.store.mapping import to_cltr_row
from tests.conftest import Conn

pytestmark = pytest.mark.db

#: 실데이터(6,902건)와 섞이지 않게 하는 시도명.
#: 대상 선별이 실제 물건을 정상 반환하므로 범위 필터로 갈라낸다.
REGION = "테스트특별시"


async def seed(conn: Conn, mng: str, **overrides: Any) -> None:
    raw: dict[str, Any] = {
        "cltrMngNo": mng, "pbctCdtnNo": "1", "onbidCltrNm": "테스트",
        "lctnSdnm": REGION, "lctnSggnm": "강남구", "lctnEmdNm": "개포동",
        "apslEvlAmt": 1000, "lowstBidPrcIndctCont": "500",
        "usbdNft": 1, "pbctStatCd": "0001", "pvctTrgtYn": "N", **overrides,
    }
    row = to_cltr_row(CollectedItem(raw=raw, group="N"))
    assert row is not None
    await upsert_cltr(conn, [row])


def result(mng: str, **overrides: Any) -> GeocodeResult:
    values: dict[str, Any] = {
        "key": (mng, "1"), "addr": f"{REGION} 강남구 개포동 12-3",
        "lat": 37.5, "lng": 127.05, "status": "ok", "level": "jibun", "src": "kakao",
        **overrides,
    }
    return GeocodeResult(**values)


async def row_of(conn: Conn, mng: str) -> tuple[Any, ...]:
    async with conn.cursor() as cur:
        await cur.execute(
            "select lat, lng, geocode_status, geocode_level, geocode_src "
            "from onbid_cltr where cltr_mng_no = %s", (mng,))
        found = await cur.fetchone()
        assert found is not None
        return found


# ── 대상 선별 ──────────────────────────────────────────────────────────


async def test_targets_are_rows_without_coordinates(conn: Conn) -> None:
    await seed(conn, "T-GEO-NEED")
    await update_geocode(conn, [result("T-GEO-NEED")])
    await seed(conn, "T-GEO-WANT")

    picked = {t.key[0] for t in await select_geocode_targets(conn, limit=500, sd_nm=REGION)}

    assert "T-GEO-WANT" in picked
    assert "T-GEO-NEED" not in picked


async def test_targets_carry_every_fallback_candidate(conn: Conn) -> None:
    """사다리가 도로명·지번·읍면동을 모두 봐야 하므로 함께 실어 보낸다."""
    await seed(conn, "T-GEO-FIELDS")

    target = next(t for t in await select_geocode_targets(conn, limit=500, sd_nm=REGION)
                  if t.key[0] == "T-GEO-FIELDS")

    assert target.jibun_addr
    assert (target.sd_nm, target.sgg_nm, target.emd_nm) == (REGION, "강남구", "개포동")


async def test_targets_respect_the_limit(conn: Conn) -> None:
    """일일 호출 상한을 넘겨 뽑으면 예산 계산이 어긋난다."""
    for i in range(4):
        await seed(conn, f"T-GEO-LIM{i}")

    assert len(await select_geocode_targets(conn, limit=2, sd_nm=REGION)) == 2


# ── 적재 (F3.6) ────────────────────────────────────────────────────────


async def test_update_writes_point_and_provenance(conn: Conn) -> None:
    await seed(conn, "T-GEO-OK")

    assert await update_geocode(conn, [result("T-GEO-OK")]) == 1

    lat, lng, status, level, src = await row_of(conn, "T-GEO-OK")
    assert float(lat) == pytest.approx(37.5)
    assert (status, level, src) == ("ok", "jibun", "kakao")


async def test_update_records_approximation(conn: Conn) -> None:
    """근사를 정확으로 적으면 지도에서 동 중심점이 정확한 척 보인다."""
    await seed(conn, "T-GEO-APPROX")

    await update_geocode(conn, [result("T-GEO-APPROX", status="approx",
                                       level="dong_center")])

    _lat, _lng, status, level, _src = await row_of(conn, "T-GEO-APPROX")
    assert (status, level) == ("approx", "dong_center")


async def test_update_records_failure_without_coordinates(conn: Conn) -> None:
    """실패도 기록해야 다음 패스가 같은 행을 또 집지 않는지 판단할 수 있다."""
    await seed(conn, "T-GEO-FAIL")

    await update_geocode(conn, [result("T-GEO-FAIL", lat=None, lng=None,
                                       status="failed", level=None, src=None)])

    lat, lng, status, _level, _src = await row_of(conn, "T-GEO-FAIL")
    assert (lat, lng, status) == (None, None, "failed")


async def test_failed_rows_stay_in_the_target_pool(conn: Conn) -> None:
    """좌표가 없으니 다시 시도해야 한다 — 캐시가 재호출을 막아 주므로 안전하다."""
    await seed(conn, "T-GEO-RETRY")
    await update_geocode(conn, [result("T-GEO-RETRY", lat=None, lng=None,
                                       status="failed", level=None, src=None)])

    picked = {t.key[0] for t in await select_geocode_targets(conn, limit=500, sd_nm=REGION)}

    assert "T-GEO-RETRY" in picked


async def test_update_matches_the_full_key(conn: Conn) -> None:
    """물건관리번호가 같아도 공매조건번호가 다르면 다른 행이다."""
    await seed(conn, "T-GEO-KEY")
    await seed(conn, "T-GEO-KEY", pbctCdtnNo="2")

    await update_geocode(conn, [result("T-GEO-KEY")])

    async with conn.cursor() as cur:
        await cur.execute(
            "select count(*) from onbid_cltr where cltr_mng_no = %s and lat is null",
            ("T-GEO-KEY",))
        found = await cur.fetchone()
    assert found is not None and found[0] == 1


async def test_update_of_empty_list(conn: Conn) -> None:
    assert await update_geocode(conn, []) == 0


async def test_update_handles_a_batch(conn: Conn) -> None:
    for i in range(50):
        await seed(conn, f"T-GEO-BULK{i:02d}")

    assert await update_geocode(
        conn, [result(f"T-GEO-BULK{i:02d}") for i in range(50)]) == 50
