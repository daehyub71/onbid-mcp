"""MCP 서버 골격 테스트 (F6.1·N4.3).

**stdio 전용이다.** 네트워크를 열지 않는다 (N4.3) — 열면 조회형의 경계를 벗어나고(§2.4),
service_role 로 붙는 DB 가 그대로 노출된다.

stdio 에서는 **표준출력이 프로토콜 채널**이다. 로그가 stdout 으로 새면 JSON-RPC 프레임이
깨져 클라이언트가 서버를 죽은 것으로 본다 — 조용히 연결이 끊긴다.
"""

import logging
import sys

import pytest

from onbid_mcp.server import (
    INSTRUCTIONS,
    SERVER_NAME,
    build_server,
    configure_logging,
)

# ── 서버 정체성 ────────────────────────────────────────────────────────


def test_server_has_a_name() -> None:
    server = build_server()
    assert server.name == SERVER_NAME


def test_instructions_state_the_boundary() -> None:
    """LLM 이 이 서버를 무엇으로 여길지가 여기서 정해진다 (§1.5①·F6.9)."""
    assert "판단" in INSTRUCTIONS
    assert "온비드" in INSTRUCTIONS


def test_instructions_declare_batch_origin() -> None:
    """실시간으로 오인하면 '지금 이 값' 이라고 단정한다."""
    assert "배치" in INSTRUCTIONS or "수집" in INSTRUCTIONS


# ── stdout 보호 (F6.1) ─────────────────────────────────────────────────


def test_logging_never_writes_to_stdout() -> None:
    """stdout 은 JSON-RPC 채널이다. 로그가 섞이면 프레임이 깨진다."""
    configure_logging()

    streams = [
        handler.stream
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
    ]
    assert streams
    assert all(stream is not sys.stdout for stream in streams)


def test_logging_is_idempotent() -> None:
    """두 번 불러도 핸들러가 쌓이면 같은 줄이 여러 번 찍힌다."""
    configure_logging()
    first = len(logging.getLogger().handlers)

    configure_logging()

    assert len(logging.getLogger().handlers) == first


def test_httpx_logging_stays_suppressed() -> None:
    """요청 URL 에 카카오·온비드 키가 실린다 (N4.5)."""
    configure_logging()

    assert logging.getLogger("httpx").level >= logging.WARNING


# ── 등록 ───────────────────────────────────────────────────────────────


async def test_server_exposes_no_tools_yet() -> None:
    """골격 단계다. 툴은 다음 태스크에서 붙인다 — 빈 상태도 정상 기동해야 한다."""
    server = build_server()

    assert await server.list_tools() == []


def test_build_server_is_repeatable() -> None:
    """테스트마다 새 인스턴스를 만들 수 있어야 한다 — 전역 하나면 상태가 샌다."""
    assert build_server() is not build_server()


# ── 실행 진입점 ────────────────────────────────────────────────────────


def test_run_is_stdio_only() -> None:
    """HTTP·SSE 진입점을 노출하지 않는다 (N4.3)."""
    import onbid_mcp.server as module

    exported = {name for name in dir(module) if not name.startswith("_")}
    assert not {"run_http", "run_sse", "app"} & exported


@pytest.mark.parametrize("required", ["build_server", "configure_logging", "main"])
def test_entry_points_exist(required: str) -> None:
    import onbid_mcp.server as module

    assert hasattr(module, required)
