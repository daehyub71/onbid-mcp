"""배치 건강 점검 (AC14).

최근 며칠 동안 기대한 배치가 모두 돌았는지 본다. **실행 자체가 없었던 날**은 실패 알림이
오지 않으므로 날짜를 세어야만 드러난다.

실행::

    python scripts/batch_health.py           # 최근 7일
    python scripts/batch_health.py --days 14
"""

import argparse
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from core.ops.health import check_window  # noqa: E402
from core.store.connection import Database  # noqa: E402

QUERY = """
    select to_char(started_at at time zone 'Asia/Seoul', 'YYYY-MM-DD') as kst_date,
           mode, status
      from onbid_batch_run
     where started_at >= (now() at time zone 'Asia/Seoul')::date - %(days)s
"""


async def main() -> int:
    parser = argparse.ArgumentParser(description="최근 배치 실행을 점검한다")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    database = Database()
    try:
        rows = await database.fetch(QUERY, {"days": args.days + 1})
    finally:
        await database.close()

    runs = [{"kst_date": r[0], "mode": r[1], "status": r[2]} for r in rows]
    # KST 기준 오늘. 배치가 04:00 에 도므로 당일도 포함해 본다.
    today = date.today() + timedelta(0)
    checks = check_window(runs, end=today, days=args.days)

    for check in checks:
        print(f"  {check.label()}")

    broken = [c for c in checks if not c.ok]
    print()
    if not broken:
        print(f"✅ 최근 {args.days}일 모두 정상 (AC14)")
        return 0
    missing = sum(1 for c in broken if c.missing)
    print(f"⚠️ {len(broken)}일에 문제 — 물건 배치 누락 {missing}일")
    return 1


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
