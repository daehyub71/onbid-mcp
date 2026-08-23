"""마이그레이션 적용기 (F4.11).

``migrations/NNN_*.sql`` 을 번호순으로 실행한다. 모든 마이그레이션은 재실행 가능하게
작성하므로(AC13) 적용 이력을 따로 관리하지 않고 **매번 전부 실행**한다 —
스키마가 적고 DDL 이 멱등이라 이 편이 단순하고 안전하다.

실행::

    python scripts/migrate.py           # 적용
    python scripts/migrate.py --dry-run # 실행할 파일만 보여준다
"""

import argparse
import pathlib
import re
import sys

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations"


def database_url() -> str:
    """`.env` 에서 접속 문자열을 읽는다.

    Raises:
        SystemExit: `SUPABASE_DATABASE_URL` 이 없을 때.
    """
    env = ROOT / ".env"
    if not env.exists():
        raise SystemExit(".env 가 없다")
    found = re.search(r"^SUPABASE_DATABASE_URL=(.*)$", env.read_text(encoding="utf-8"), re.M)
    if not found or not found.group(1).strip():
        raise SystemExit(".env 에 SUPABASE_DATABASE_URL 이 없다")
    return found.group(1).strip()


def migration_files() -> list[pathlib.Path]:
    """번호순으로 정렬된 마이그레이션 파일."""
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def main() -> int:
    parser = argparse.ArgumentParser(description="마이그레이션을 적용한다")
    parser.add_argument("--dry-run", action="store_true", help="실행하지 않고 목록만 보여준다")
    args = parser.parse_args()

    files = migration_files()
    if not files:
        print("적용할 마이그레이션이 없다")
        return 0

    for path in files:
        print(f"  {path.name} ({path.stat().st_size:,} bytes)")
    if args.dry_run:
        return 0

    with psycopg.connect(database_url(), connect_timeout=30) as conn:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            print(f"✅ {path.name} 적용 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
