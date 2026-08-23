"""서비스키 로그 노출 차단 테스트 (N4.3·AC11).

온비드는 인증키를 **쿼리 파라미터**로 요구한다. 그런데 httpx 는 INFO 레벨에서 요청 URL을
통째로 기록하므로, 애플리케이션이 로깅을 켜는 순간 키가 로그 파일에 남는다.

    INFO:httpx:HTTP Request: GET https://...&serviceKey=gPqn... "HTTP/1.1 200 OK"

**조용하다는 점이 위험하다** — 예외도, 경고도 없다. 실제로 첫 실적재(2026-08-23)에서
터미널에 찍혔고 보안 점검에서 발견했다. 클라이언트를 import 하는 것만으로 막힌다.
"""

import logging

import pytest


def test_httpx_request_logging_is_suppressed() -> None:
    """`core.onbid.client` 를 쓰면 httpx 의 요청 로그가 켜지지 않는다."""
    import core.onbid.client  # noqa: F401

    assert logging.getLogger("httpx").level >= logging.WARNING


def test_our_own_logging_still_works(caplog: pytest.LogCaptureFixture) -> None:
    """남의 로거를 막았다고 우리 로그까지 죽이면 배치 상황을 알 수 없다."""
    with caplog.at_level(logging.INFO, logger="core.onbid.client"):
        logging.getLogger("core.onbid.client").info("배치 진행 중")

    assert "배치 진행 중" in caplog.text


def test_suppression_can_be_overridden() -> None:
    """디버깅 시 호출자가 되돌릴 수 있어야 한다 — 잠가버리지 않는다."""
    import core.onbid.client  # noqa: F401

    logger = logging.getLogger("httpx")
    original = logger.level
    try:
        logger.setLevel(logging.INFO)
        assert logger.level == logging.INFO
    finally:
        logger.setLevel(original)
