"""Supabase 연결 관리 (N1.3·C10).

두 가지 제약이 설계를 결정한다.

1. **커넥션 재사용** — 툴 호출마다 새 연결을 열면 무료 티어 연결 수 제한에 걸리고
   지연도 커진다. 인스턴스를 하나 만들어 오래 쓴다 (N1.3).
2. **prepared statement 금지** — ``SUPABASE_DATABASE_URL`` 은 pgbouncer
   **트랜잭션 풀러(6543)** 를 가리킨다. psycopg 는 같은 쿼리가 반복되면 자동으로
   prepared statement 로 전환하는데 pgbouncer 가 이를 지원하지 않아,
   **반복 호출에서만 깨진다** — 단발 실행은 통과해 원인 찾기가 어렵다 (C10).

MCP 서버가 async 이므로 `psycopg.AsyncConnection` 을 쓴다. 동기 드라이버를 쓰면
툴 호출이 이벤트 루프를 막는다.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Final, Self

import psycopg

from core.config import Settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT: Final = 30
"""연결 타임아웃(초)."""

Row = tuple[Any, ...]


class Database:
    """Supabase 연결 하나를 재사용하는 관리자.

    Args:
        dsn: 접속 문자열. 생략하면 `.env` 의 ``SUPABASE_DATABASE_URL``.
        autocommit: 자동 커밋 여부. 배치 적재는 명시적으로 커밋하려고 기본 참이다.
    """

    def __init__(self, dsn: str | None = None, *, autocommit: bool = True) -> None:
        self._dsn = dsn or Settings.load().require("database_url")
        self._autocommit = autocommit
        self._conn: psycopg.AsyncConnection[Row] | None = None

    async def connect(self) -> psycopg.AsyncConnection[Row]:
        """연결을 얻는다. 이미 열려 있으면 그대로 재사용한다.

        Returns:
            열린 연결.
        """
        if self._conn is not None and not self._conn.closed:
            return self._conn

        self._conn = await psycopg.AsyncConnection.connect(
            self._dsn,
            connect_timeout=CONNECT_TIMEOUT,
            autocommit=self._autocommit,
            # 트랜잭션 풀러에서는 prepared statement 를 쓸 수 없다 (C10).
            prepare_threshold=None,
        )
        logger.debug("Supabase 연결 수립")
        return self._conn

    async def close(self) -> None:
        """연결을 닫는다. 열려 있지 않으면 아무것도 하지 않는다."""
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = None

    @property
    def is_connected(self) -> bool:
        """연결이 살아 있는지 여부."""
        return self._conn is not None and not self._conn.closed

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[psycopg.AsyncCursor[Row]]:
        """커서를 연다. 연결은 닫지 않고 유지한다."""
        conn = await self.connect()
        async with conn.cursor() as cur:
            yield cur

    async def fetch(self, sql: str, params: Any = None) -> list[Row]:
        """조회 결과를 전부 가져온다."""
        async with self.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def fetch_one(self, sql: str, params: Any = None) -> Row | None:
        """첫 행만 가져온다."""
        async with self.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def execute(self, sql: str, params: Any = None) -> int:
        """문장을 실행하고 영향받은 행 수를 돌려준다."""
        async with self.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
