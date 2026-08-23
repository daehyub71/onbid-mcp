"""지오코딩 캐시 테스트 (`pytest -m db`, F3.2).

**카카오 앱을 다른 프로젝트와 공유한다.** 같은 주소를 두 번 묻는 것은 남의 쿼터까지 태우는
일이라, 모든 시도 전에 캐시를 먼저 본다.

**실패도 캐시한다.** 안 그러면 좌표를 못 찾는 주소를 매일 다시 묻게 되고, 그 호출이 쿼터에서
가장 큰 몫을 차지한다 — 성공하는 주소는 한 번 캐시되면 다시 묻지 않기 때문이다.

배치가 6,902건이므로 **한 건씩 조회하지 않는다** (F4.10 과 같은 이유).
"""

from typing import Any

import pytest

from core.geocoder.cache import CachedPoint, lookup_cache, normalize_addr, store_cache
from tests.conftest import Conn

pytestmark = pytest.mark.db

SEOUL = "서울특별시 강남구 개포동 12-3"
OTHER = "서울특별시 서초구 반포동 45-6"


def hit(addr: str, **overrides: Any) -> CachedPoint:
    values: dict[str, Any] = {
        "addr": addr, "lat": 37.5, "lng": 127.05,
        "src": "kakao", "level": "jibun", **overrides,
    }
    return CachedPoint(**values)


# ── 조회 ───────────────────────────────────────────────────────────────


async def test_cache_returns_stored_point(conn: Conn) -> None:
    await store_cache(conn, [hit(SEOUL)])

    found = await lookup_cache(conn, [SEOUL])

    assert found[SEOUL].lat == pytest.approx(37.5)
    assert found[SEOUL].level == "jibun"


async def test_cache_misses_are_absent(conn: Conn) -> None:
    """없는 주소는 키 자체가 없어야 한다 — None 과 '캐시된 실패' 를 구분하기 위해."""
    assert await lookup_cache(conn, ["T-없는주소-1"]) == {}


async def test_cache_looks_up_a_batch_in_one_query(conn: Conn) -> None:
    """6,902건을 한 건씩 조회하면 배치가 끝나지 않는다."""
    addrs = [f"T-GC-BULK-{i:04d}" for i in range(200)]
    await store_cache(conn, [hit(a) for a in addrs])

    found = await lookup_cache(conn, addrs)

    assert len(found) == 200


async def test_cache_lookup_of_empty_list(conn: Conn) -> None:
    assert await lookup_cache(conn, []) == {}


# ── 실패 캐싱 ──────────────────────────────────────────────────────────


async def test_failure_is_cached(conn: Conn) -> None:
    """좌표를 못 찾은 주소를 매일 다시 물으면 쿼터가 그쪽으로 다 나간다."""
    await store_cache(conn, [hit("T-GC-FAIL", lat=None, lng=None, level=None)])

    found = await lookup_cache(conn, ["T-GC-FAIL"])

    assert "T-GC-FAIL" in found
    assert found["T-GC-FAIL"].lat is None
    assert found["T-GC-FAIL"].is_failure is True


async def test_success_is_not_a_failure(conn: Conn) -> None:
    await store_cache(conn, [hit(SEOUL)])
    assert (await lookup_cache(conn, [SEOUL]))[SEOUL].is_failure is False


# ── 정규화 ─────────────────────────────────────────────────────────────


def test_normalize_collapses_whitespace() -> None:
    """같은 주소가 공백 차이로 두 행이 되면 캐시 적중률이 떨어진다."""
    assert normalize_addr("  서울특별시   강남구  개포동 12-3 ") == "서울특별시 강남구 개포동 12-3"


def test_normalize_is_idempotent() -> None:
    once = normalize_addr(" 서울  강남 ")
    assert normalize_addr(once) == once


def test_normalize_handles_empty() -> None:
    assert normalize_addr("   ") == ""
    assert normalize_addr(None) == ""


async def test_cache_hits_regardless_of_spacing(conn: Conn) -> None:
    await store_cache(conn, [hit(SEOUL)])

    found = await lookup_cache(conn, ["서울특별시  강남구   개포동 12-3"])

    assert found  # 정규화된 키로 맞아야 한다


# ── 갱신 ───────────────────────────────────────────────────────────────


async def test_store_updates_an_existing_address(conn: Conn) -> None:
    """폴백 단계가 올라가면 더 정확한 좌표로 덮어써야 한다."""
    await store_cache(conn, [hit(SEOUL, lat=37.1, level="dong_center")])

    await store_cache(conn, [hit(SEOUL, lat=37.9, level="road")])

    found = await lookup_cache(conn, [SEOUL])
    assert found[SEOUL].lat == pytest.approx(37.9)
    assert found[SEOUL].level == "road"


async def test_store_is_idempotent(conn: Conn) -> None:
    await store_cache(conn, [hit(SEOUL), hit(OTHER)])
    await store_cache(conn, [hit(SEOUL), hit(OTHER)])

    assert len(await lookup_cache(conn, [SEOUL, OTHER])) == 2


async def test_store_of_empty_list(conn: Conn) -> None:
    assert await store_cache(conn, []) == 0


async def test_store_skips_blank_addresses(conn: Conn) -> None:
    """빈 주소를 캐시하면 모든 빈 주소가 서로의 결과를 물려받는다."""
    assert await store_cache(conn, [hit("   ", lat=1.0)]) == 0
