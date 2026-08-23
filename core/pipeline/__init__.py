"""배치 오케스트레이션 (F4.16).

`core/store` 의 함수들은 전부 "커밋은 호출자가 한다" 로 만들었다. **그 호출자가 이 패키지고,
커밋 지점은 여기뿐이다.**

그래서 연결이 autocommit 이면 안 된다. 문장마다 커밋되면 "데이터는 한 트랜잭션" 이라는
전제가 무너지고, 실패 경로의 `rollback()` 이 아무 일도 하지 않아 부분 적재가 남는다.
예외도 나지 않아 조용히 어긋난다 — 그래서 시작하자마자 막는다.
"""

from typing import Any

import psycopg


def require_transactional(conn: psycopg.AsyncConnection[Any]) -> None:
    """연결이 명시적 트랜잭션 모드인지 확인한다.

    Args:
        conn: 검사할 연결.

    Raises:
        ValueError: autocommit 연결일 때. `Database(autocommit=False)` 로 열어야 한다.
    """
    if conn.autocommit:
        raise ValueError(
            "배치는 autocommit 연결에서 실행할 수 없다 — 데이터를 한 트랜잭션으로 "
            "커밋할 수 없고 실패해도 되돌릴 수 없다. Database(autocommit=False) 로 연다 (F4.16)"
        )
