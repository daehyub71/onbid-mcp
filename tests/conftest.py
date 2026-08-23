"""테스트 공통 설정."""

import json
import pathlib
import re
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "onbid"
ROOT = pathlib.Path(__file__).resolve().parents[1]

Conn = psycopg.AsyncConnection[tuple[Any, ...]]


def load_fixture(name: str) -> dict[str, Any]:
    """캡처해 둔 실응답을 읽는다 (`scripts/capture_fixtures.py` 산출물)."""
    payload: dict[str, Any] = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return payload


@pytest.fixture
def fixture() -> Any:
    """이름으로 실응답 fixture를 불러오는 헬퍼."""
    return load_fixture


def database_url() -> str:
    """`.env` 의 접속 문자열. 없으면 테스트를 건너뛴다."""
    env = ROOT / ".env"
    if not env.exists():
        pytest.skip(".env 가 없어 db 테스트를 건너뛴다")
    found = re.search(r"^SUPABASE_DATABASE_URL=(.*)$", env.read_text(encoding="utf-8"), re.M)
    if not found or not found.group(1).strip():
        pytest.skip("SUPABASE_DATABASE_URL 이 없어 db 테스트를 건너뛴다")
    return found.group(1).strip()


@pytest.fixture
async def conn() -> AsyncIterator[Conn]:
    """**롤백되는** 연결.

    이 Supabase 는 다른 3개 프로젝트와 공유되는 실서비스다. 적재 테스트가 흔적을 남기면
    안 되므로 teardown 에서 반드시 되돌린다.

    `prepare_threshold=None` 은 트랜잭션 풀러 제약이다 (SPEC C10).
    """
    connection = await psycopg.AsyncConnection.connect(
        database_url(), connect_timeout=20, autocommit=False, prepare_threshold=None
    )
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()
