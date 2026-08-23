"""지오코딩 패스 (F3.1~F3.6·PLAN §4.1①).

**수집과 분리된 패스다.** 붙여 두면 카카오 장애 하나가 온비드 수집까지 멈춰 세우고, 좌표만
다시 채우고 싶을 때 전량을 다시 받아야 한다. 대상은 ``lat is null`` 인 행이므로 이 패스는
몇 번을 돌려도 이미 채운 행을 건드리지 않는다.

세 가지를 지킨다.

- **호출 상한** (F3.5) — 카카오 앱을 다른 프로젝트와 공유한다. 예산을 넘기면 남의 서비스가
  막히므로 대상 수를 예산으로 자른다.
- **쿼터 소진 시 즉시 중단** (F3.3) — 남은 대상을 다음 실행으로 넘기고 `partial` 로 닫는다.
  **이미 얻은 좌표는 적재한다** — 버리면 그만큼 쿼터를 버리는 셈이다.
- **캐시 먼저** (F3.2) — 실측상 6,902건의 고유 주소가 801개뿐이라 절약이 크다.

커밋 경계는 다른 배치와 같다 (F4.16): 메타는 즉시, 데이터는 한 트랜잭션.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import psycopg

from core.geocoder.cache import CachedPoint, lookup_cache, normalize_addr, store_cache
from core.geocoder.kakao import KakaoClient, KakaoQuotaExceededError
from core.geocoder.resolver import (
    CachedTuple,
    GeocodeResult,
    GeocodeTarget,
    resolve_one,
)
from core.pipeline import require_transactional
from core.store.batch_run import BatchCounts, finish_run, start_run
from core.store.geocode import select_geocode_targets, update_geocode

logger = logging.getLogger(__name__)

MODE: Final = "geocode"
"""수집 배치와 재개 지점을 공유하지 않도록 모드를 분리한다."""

NOTE_LIMIT: Final = 500


@dataclass(frozen=True, slots=True)
class GeocodeBatchOutcome:
    """지오코딩 패스 결과.

    Attributes:
        run_id: 배치 식별자.
        targets: 이번 패스가 고른 대상 수.
        located: 좌표를 얻은 수 (근사 포함).
        approx: 읍면동 중심으로 근사한 수.
        failed: 좌표를 얻지 못한 수.
        carried_over: 쿼터 소진으로 처리하지 못해 다음 실행으로 넘긴 수.
        api_calls: 실제 외부 호출 수. **재시도를 포함한다** (F3.5).
        status: ``ok`` | ``partial``.
    """

    run_id: int
    targets: int
    located: int
    approx: int
    failed: int
    carried_over: int
    api_calls: int
    status: str


def _to_cached(result: GeocodeResult) -> CachedPoint | None:
    """결과를 캐시 항목으로. 주소가 없으면 캐시할 것이 없다."""
    if not result.addr:
        return None
    return CachedPoint(addr=result.addr, lat=result.lat, lng=result.lng,
                       src=result.src, level=result.level)


async def run_geocode_batch(
    conn: psycopg.AsyncConnection[Any],
    kakao: KakaoClient,
    *,
    budget: int,
    sd_nm: str | None = None,
    sgg_nm: str | None = None,
    usage_fn: Callable[[], int] | None = None,
) -> GeocodeBatchOutcome:
    """좌표가 없는 물건에 좌표를 붙인다.

    Args:
        conn: 열린 연결. **이 함수가 커밋한다.**
        kakao: 카카오 클라이언트.
        budget: 이번 패스의 대상 상한. 카카오 호출 예산과 맞춘다.
        sd_nm: 시도명으로 범위를 좁힌다.
        sgg_nm: 시군구명으로 범위를 좁힌다.
        usage_fn: 호출 수를 읽는 함수. 기본은 클라이언트의 `call_count`.

    Returns:
        패스 결과.

    Raises:
        Exception: 적재 중 발생한 예외를 그대로 올린다. 되돌리고 ``failed`` 로 닫는다.
    """
    require_transactional(conn)
    usage = usage_fn or (lambda: kakao.call_count)

    run_id = await start_run(conn, mode=MODE)
    await conn.commit()  # 메타는 즉시 커밋 (F4.16)

    try:
        targets = (
            await select_geocode_targets(conn, limit=budget, sd_nm=sd_nm, sgg_nm=sgg_nm)
            if budget > 0
            else []
        )
        results, aborted = await _resolve(conn, kakao, targets)

        located = await update_geocode(conn, results)
        cached = [point for point in (_to_cached(r) for r in results) if point]
        await store_cache(conn, cached)
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        await finish_run(conn, run_id, status="failed", note=str(exc)[:NOTE_LIMIT])
        await conn.commit()
        logger.exception("지오코딩 패스 실패: run_id=%d", run_id)
        raise

    outcome = GeocodeBatchOutcome(
        run_id=run_id,
        targets=len(targets),
        located=sum(1 for r in results if r.is_located),
        approx=sum(1 for r in results if r.status == "approx"),
        failed=sum(1 for r in results if r.status == "failed"),
        carried_over=len(targets) - len(results),
        api_calls=usage(),
        status="partial" if aborted else "ok",
    )

    await finish_run(
        conn, run_id,
        status=outcome.status,
        counts=BatchCounts(collected=outcome.targets, upserted=located,
                           geocode_ok=outcome.located - outcome.approx,
                           geocode_approx=outcome.approx,
                           geocode_failed=outcome.failed),
        note=f"호출 {outcome.api_calls}회 · 이월 {outcome.carried_over}"[:NOTE_LIMIT],
    )
    await conn.commit()

    logger.info(
        "지오코딩 패스 완료: run_id=%d %s · 대상 %d · 좌표 %d (근사 %d) · 실패 %d · 호출 %d",
        run_id, outcome.status, outcome.targets, outcome.located,
        outcome.approx, outcome.failed, outcome.api_calls,
    )
    return outcome


async def _resolve(
    conn: psycopg.AsyncConnection[Any],
    kakao: KakaoClient,
    targets: list[GeocodeTarget],
) -> tuple[list[GeocodeResult], bool]:
    """대상을 사다리에 태운다. 쿼터가 끊기면 거기까지의 결과를 돌려준다.

    Returns:
        ``(결과, 쿼터로 중단됐는지)``.
    """
    if not targets:
        return [], False

    # 이번 패스가 볼 주소를 한 번에 조회한다 (F3.2·F4.10).
    wanted = {
        normalize_addr(addr)
        for target in targets
        for addr in (target.road_addr, target.jibun_addr, target.district_query())
        if normalize_addr(addr)
    }
    stored = await lookup_cache(conn, sorted(wanted))
    running: dict[str, CachedTuple] = {
        addr: (point.lat, point.lng, point.src, point.level)
        for addr, point in stored.items()
    }

    results: list[GeocodeResult] = []
    for target in targets:
        try:
            result = await resolve_one(target, kakao=kakao, cached=running)
        except KakaoQuotaExceededError:
            # 남은 대상은 다음 실행으로 넘긴다. 여기까지의 결과는 살린다 (F3.3).
            logger.warning("카카오 쿼터 소진 — %d건을 다음 실행으로 넘긴다",
                           len(targets) - len(results))
            return results, True

        results.append(result)
        if result.addr and not result.from_cache:
            running[result.addr] = (result.lat, result.lng, result.src, result.level)

    return results, False
