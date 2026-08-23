"""배치 실행기 — 코드표 → 물건 → 회차 순으로 돈다 (F4.16).

실행::

    python scripts/run_batch.py                      # 코드표 + 전량 + 회차 + 좌표
    python scripts/run_batch.py --mode delta --since 20260820
    python scripts/run_batch.py --sgg 강남구 --rounds-budget 0
    python scripts/run_batch.py --dry-run            # 무엇을 할지만 출력

**실제로 커밋한다.** 지금까지의 테스트는 전부 롤백 트랜잭션 안에서 돌았지만 이건 아니다.

순서에 이유가 있다. 회차 대상은 적재된 물건의 유찰횟수로 고르므로 **물건 적재 뒤**여야 하고,
좌표 패스도 `lat is null` 인 행을 보므로 마찬가지다. 코드표는 조회용이라 앞뒤 어디든 되지만
실패해도 나머지를 막지 않도록 맨 앞에 둔다.
"""

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, ".")

from core.config import Settings  # noqa: E402
from core.geocoder.kakao import KakaoClient  # noqa: E402
from core.onbid.client import OnbidClient  # noqa: E402
from core.onbid.collector import ListingFilter  # noqa: E402
from core.pipeline.batch import run_listing_batch  # noqa: E402
from core.pipeline.codes import run_code_batch  # noqa: E402
from core.pipeline.geocode import run_geocode_batch  # noqa: E402
from core.pipeline.rounds import run_round_batch  # noqa: E402
from core.store.connection import Database  # noqa: E402

DEFAULT_ROUNDS_BUDGET = 50
"""첫 실행 기본값. 일일 상한은 1,000이지만 처음부터 다 쓰지 않는다."""

DEFAULT_GEOCODE_BUDGET = 500
"""좌표 패스 기본 예산. 카카오 앱을 다른 프로젝트와 공유하므로 여유를 둔다 (F3.5)."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="온비드 배치를 실행한다")
    parser.add_argument("--mode", choices=("full", "delta"), default="full")
    parser.add_argument("--since", help="증분 기준일 (yyyyMMdd). --mode delta 와 함께 쓴다")
    parser.add_argument("--sgg", help="시군구로 범위를 좁힌다 (예: 강남구)")
    parser.add_argument("--rounds-budget", type=int, default=DEFAULT_ROUNDS_BUDGET,
                        help="회차 배치 호출 예산. 0이면 건너뛴다")
    parser.add_argument("--geocode-budget", type=int, default=DEFAULT_GEOCODE_BUDGET,
                        help="좌표 패스 호출 예산. 0이면 건너뛴다")
    parser.add_argument("--skip-codes", action="store_true", help="코드표 갱신을 건너뛴다")
    parser.add_argument("--page-size", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def build_filter(args: argparse.Namespace) -> ListingFilter:
    """인자에서 수집 조건을 만든다. **모드는 이 필터 하나에서 파생된다.**"""
    if args.mode == "delta" and not args.since:
        raise SystemExit("--mode delta 에는 --since 가 필요하다")
    return ListingFilter(region_sgg=args.sgg,
                         modified_from=args.since if args.mode == "delta" else None)


async def main() -> int:
    args = parse_args()
    listing_filter = build_filter(args)

    print(f"모드    {args.mode}")
    print(f"범위    {listing_filter.region_sd} {listing_filter.region_sgg or '전체'}")
    print(f"코드표  {'건너뜀' if args.skip_codes else '갱신'}")
    print(f"회차    예산 {args.rounds_budget}건" if args.rounds_budget else "회차    건너뜀")
    print(f"좌표    예산 {args.geocode_budget}건" if args.geocode_budget else "좌표    건너뜀")
    if args.dry_run:
        print("\n--dry-run 이라 실행하지 않는다")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    key = Settings.load().require("onbid_service_key")

    # 파이프라인이 커밋 경계를 직접 관리한다 — autocommit 이면 안 된다 (F4.16).
    database = Database(autocommit=False)
    conn = await database.connect()

    try:
        async with OnbidClient(service_key=key) as client:
            if not args.skip_codes:
                print("\n── 코드표 ──")
                codes = await run_code_batch(conn, client,
                                             region_sd=listing_filter.region_sd)
                print(f"  {codes.status} · 용도 {codes.usage.loaded} · 주소 {codes.address.loaded}")

            print("\n── 물건 ──")
            listings = await run_listing_batch(
                conn, client, listing_filter=listing_filter, page_size=args.page_size,
                on_page=lambda g, p, n: print(f"  pvctTrgtYn={g} page={p} → {n}건"),
            )
            print(f"  {listings.status} · 수집 {listings.collected} · 적재 {listings.upserted}"
                  f" · 이력 {listings.changes} · tombstone {listings.tombstoned}")
            if listings.resume_token:
                print(f"  ⚠️ 재개 지점 {listings.resume_token}")

            if args.rounds_budget:
                print("\n── 회차 ──")
                rounds = await run_round_batch(conn, client, budget=args.rounds_budget)
                print(f"  {rounds.status} · 대상 {rounds.targets} · 회차 {rounds.rounds}"
                      f" · 이력없음 {rounds.no_data} · 실패 {rounds.failed}")

        # 좌표는 온비드가 아니라 카카오를 부르므로 클라이언트가 다르다.
        if args.geocode_budget:
            print("\n── 좌표 ──")
            kakao_key = Settings.load().require("kakao_rest_api_key")
            async with KakaoClient(rest_api_key=kakao_key) as kakao:
                geo = await run_geocode_batch(
                    conn, kakao, budget=args.geocode_budget,
                    sd_nm=listing_filter.region_sd, sgg_nm=listing_filter.region_sgg)
            print(f"  {geo.status} · 대상 {geo.targets} · 좌표 {geo.located}"
                  f" (근사 {geo.approx}) · 실패 {geo.failed} · 호출 {geo.api_calls}")
    finally:
        await database.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
