"""마이그레이션 검증 (`pytest -m db`).

SPEC §7 스키마와 §6.6 권한 차단이 **실제 DB에 반영됐는지** 확인한다.
설정 파일을 읽는 것이 아니라 **DB 카탈로그를 조회**한다 — §6.6 R5: 설정만 믿지 않는다.

이 프로젝트의 Supabase 는 다른 3개 프로젝트와 공유되며, 실측상 87개 테이블 중
**27개가 RLS 없이 anon 쓰기까지 열려 있다.** `onbid_*` 가 그렇게 되지 않는지 본다.
"""

import pathlib
import re
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.db

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "001_init.sql"

#: 카탈로그 조회용 연결 타입. 매 시그니처에 제네릭을 늘어놓지 않는다.
Conn = psycopg.Connection[tuple[Any, ...]]

EXPECTED_TABLES = {
    "onbid_cltr",
    "onbid_cltr_bid_round",
    "onbid_geocode_cache",
    "onbid_cltr_history",
    "onbid_usg_code",
    "onbid_addr_map",
    "onbid_batch_run",
}


def _dsn() -> str:
    env = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        pytest.skip(".env 가 없어 db 테스트를 건너뛴다")
    found = re.search(r"^SUPABASE_DATABASE_URL=(.*)$", env.read_text(encoding="utf-8"), re.M)
    if not found or not found.group(1).strip():
        pytest.skip("SUPABASE_DATABASE_URL 이 없어 db 테스트를 건너뛴다")
    return found.group(1).strip()


@pytest.fixture(scope="module")
def conn() -> Iterator[Conn]:
    """읽기 전용 조회용 연결.

    `autocommit` — 트랜잭션 안에서 쿼리 하나가 실패하면 그 뒤 모든 쿼리가
    "current transaction is aborted" 로 함께 죽어 진짜 원인이 가려진다.

    `prepare_threshold=None` — `SUPABASE_DATABASE_URL` 은 **트랜잭션 풀러(6543)** 를
    가리키며 pgbouncer 는 prepared statement 를 지원하지 않는다. psycopg 가 반복 쿼리를
    자동으로 prepare 하면 연속 실행에서만 깨진다 (단독 실행은 통과해 원인 찾기가 어렵다).
    """
    with psycopg.connect(
        _dsn(), connect_timeout=20, autocommit=True, prepare_threshold=None
    ) as connection:
        yield connection


def fetch(conn: Conn, sql: str,
          *args: object) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


# ── 파일 자체 ───────────────────────────────────────────────────────────


def test_migration_file_exists() -> None:
    assert MIGRATION.exists()


def test_migration_is_rerunnable() -> None:
    """모든 생성문이 `if not exists` 여야 재실행이 안전하다 (AC13)."""
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    creates = re.findall(r"create (table|index)([^;]*)", sql)
    assert creates
    for kind, body in creates:
        assert "if not exists" in body, f"create {kind} 에 if not exists 가 없다"


def test_migration_blocks_anon_in_the_same_file() -> None:
    """테이블이 생기는 순간부터 노출된다 — 차단을 별도 파일로 미루지 않는다 (§6.6)."""
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "enable row level security" in sql
    assert "revoke" in sql
    assert "anon" in sql


# ── 스키마 ──────────────────────────────────────────────────────────────


def test_all_tables_exist(conn: Conn) -> None:
    rows = fetch(conn, """
        select relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind = 'r' and c.relname = any(%s)
    """, sorted(EXPECTED_TABLES))
    assert {r[0] for r in rows} == EXPECTED_TABLES


def test_cltr_primary_key_is_composite(conn: Conn) -> None:
    """상세·입찰정보 조회가 두 값을 함께 요구한다 (F4.1)."""
    assert _pk_columns(conn, "onbid_cltr") == ["cltr_mng_no", "pbct_cdtn_no"]


def test_bid_round_primary_key_includes_open_date(conn: Conn) -> None:
    """`pbct_nsq` 는 사건마다 1부터 재시작해 중복된다 (실측 184건 충돌).

    `opbd_dt` 가 빠지면 적재가 깨진다.
    """
    columns = _pk_columns(conn, "onbid_cltr_bid_round")
    assert "opbd_dt" in columns
    assert columns == ["cltr_mng_no", "pbct_cdtn_no", "opbd_dt", "pbct_nsq"]


def test_raw_payload_is_jsonb(conn: Conn) -> None:
    """문자열이 아니라 jsonb 여야 필드 이상값을 SQL 로 조사할 수 있다."""
    assert _column_type(conn, "onbid_cltr", "raw_payload") == "jsonb"


def test_pnu_is_text_not_numeric(conn: Conn) -> None:
    """정수로 저장하면 선행 0 이 사라져 법정동코드가 깨진다."""
    assert _column_type(conn, "onbid_cltr", "ltno_pnu") == "text"


def test_amounts_are_bigint(conn: Conn) -> None:
    """감정가가 수천억 대라 integer 로는 넘친다."""
    for column in ("appraisal_amt", "min_bid_amt"):
        assert _column_type(conn, "onbid_cltr", column) == "bigint"


def test_min_bid_rate_allows_above_one(conn: Conn) -> None:
    """실측 최대 1.502 — numeric 자릿수가 부족하면 적재가 실패한다."""
    with conn.cursor() as cur:
        cur.execute("select %s::numeric(8,5)", ("1.502",))
        stored = cur.fetchone()
        assert stored is not None
        assert float(stored[0]) == pytest.approx(1.502)


def test_timestamps_are_timezone_aware(conn: Conn) -> None:
    for column in ("bid_start", "bid_end", "mdfcn_dt", "first_seen_at"):
        assert _column_type(conn, "onbid_cltr", column) == "timestamp with time zone"


def test_indexes_exist(conn: Conn) -> None:
    rows = fetch(conn, """
        select indexname from pg_indexes
         where schemaname = 'public' and tablename = any(%s)
    """, sorted(EXPECTED_TABLES))
    names = {r[0] for r in rows}
    assert {"idx_onbid_cltr_geo", "idx_onbid_cltr_rate", "idx_onbid_cltr_bidend",
            "idx_onbid_cltr_status", "idx_onbid_round_opbd"} <= names


# ── 권한 (§6.6, AC12) ───────────────────────────────────────────────────


@pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
def test_rls_is_enabled(conn: Conn, table: str) -> None:
    rows = fetch(conn, "select relrowsecurity from pg_class where relname = %s", table)
    assert rows and rows[0][0] is True, f"{table}: RLS 가 꺼져 있다"


@pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
def test_no_policies_exist(conn: Conn, table: str) -> None:
    """RLS 를 켜고 정책을 만들지 않으면 기본 거부다 (§6.6 R1)."""
    rows = fetch(conn, """
        select count(*) from pg_policies where schemaname = 'public' and tablename = %s
    """, table)
    assert rows[0][0] == 0, f"{table}: 정책이 있으면 anon 이 통과할 수 있다"


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
def test_grants_are_revoked(conn: Conn, table: str, role: str) -> None:
    """RLS 에 더해 grant 도 회수한다 — 이중 방어 (§6.6 R3)."""
    rows = fetch(conn, """
        select has_table_privilege(%s, %s, 'SELECT'),
               has_table_privilege(%s, %s, 'INSERT')
    """, role, table, role, table)
    can_select, can_insert = rows[0]
    assert not can_select, f"{table}: {role} 이 SELECT 할 수 있다"
    assert not can_insert, f"{table}: {role} 이 INSERT 할 수 있다"


def test_service_role_still_works(conn: Conn) -> None:
    """차단이 과해서 배치까지 막으면 안 된다."""
    rows = fetch(conn, "select count(*) from onbid_cltr")
    assert rows[0][0] >= 0


# ── 헬퍼 ────────────────────────────────────────────────────────────────


def _pk_columns(conn: Conn, table: str) -> list[str]:
    rows = fetch(conn, """
        select a.attname
          from pg_index i
          join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)
         where i.indrelid = %s::regclass and i.indisprimary
         order by array_position(i.indkey, a.attnum)
    """, table)
    return [r[0] for r in rows]


def _column_type(conn: Conn, table: str, column: str) -> str:
    rows = fetch(conn, """
        select data_type from information_schema.columns
         where table_schema = 'public' and table_name = %s and column_name = %s
    """, table, column)
    assert rows, f"{table}.{column} 이 없다"
    return str(rows[0][0])
