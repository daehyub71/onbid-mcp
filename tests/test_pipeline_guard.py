"""파이프라인 트랜잭션 전제 테스트 (F4.16).

`core/store` 는 전부 "커밋은 호출자가 한다" 로 만들었고, 파이프라인이 그 호출자다.
그런데 **연결이 autocommit 이면 그 설계가 통째로 무너진다** — 문장마다 커밋되므로
"데이터는 한 트랜잭션" 이 성립하지 않고, 실패 경로의 `rollback()` 도 아무 일도 하지 않아
부분 적재가 그대로 남는다. 예외도 나지 않아 조용하다.

`Database` 의 기본값이 `autocommit=True` 라 실수하기 쉽다. 시작하자마자 막는다.
"""

from typing import Any, cast

import pytest

from core.onbid.client import OnbidClient
from core.pipeline import require_transactional
from core.pipeline.batch import run_listing_batch
from tests.conftest import Conn


class FakeConn:
    def __init__(self, *, autocommit: bool) -> None:
        self.autocommit = autocommit


def test_guard_rejects_an_autocommit_connection() -> None:
    with pytest.raises(ValueError, match="autocommit"):
        require_transactional(cast(Any, FakeConn(autocommit=True)))


def test_guard_accepts_a_transactional_connection() -> None:
    require_transactional(cast(Any, FakeConn(autocommit=False)))


@pytest.mark.db
async def test_listing_batch_refuses_an_autocommit_connection(conn: Conn) -> None:
    """배치가 한 줄도 쓰기 전에 멈춰야 한다."""
    await conn.set_autocommit(True)

    with pytest.raises(ValueError, match="autocommit"):
        await run_listing_batch(conn, cast(OnbidClient, object()))

    await conn.set_autocommit(False)
