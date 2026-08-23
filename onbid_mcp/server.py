"""MCP 서버 (stdio) — F6.1·N4.3.

**stdio 전용이다.** HTTP·SSE 진입점을 노출하지 않는다. 네트워크를 열면 조회형의 경계를
벗어나고(§2.4), `service_role` 로 붙는 DB 가 그대로 노출된다.

**stdout 은 프로토콜 채널이다.** stdio 트랜스포트에서 표준출력으로 나가는 모든 바이트는
JSON-RPC 프레임으로 해석된다. 로그 한 줄이 섞이면 프레임이 깨지고 클라이언트는 서버가
죽은 것으로 본다 — 오류 메시지도 없이 연결만 끊긴다. 그래서 로깅을 **stderr 로 못박는다.**

실행::

    python -m onbid_mcp.server

Claude Code·Desktop 에는 `.mcp.json` 으로 등록한다 (M6 마지막 태스크).
"""

import logging
import sys
from typing import Final

from mcp.server import MCPServer

# 클라이언트가 이 서버를 무엇으로 여길지가 여기서 정해진다.
import core.onbid.client  # noqa: F401  # httpx 요청 로그 차단 (N4.5)

logger = logging.getLogger(__name__)

SERVER_NAME: Final = "onbid"

VERSION: Final = "0.1.0"

INSTRUCTIONS: Final = """온비드(한국자산관리공사) 공매 물건을 조회하는 데이터 레이어입니다.

- **판단하지 않습니다.** 물건을 추천·평가·순위화하지 않으며, 조건에 맞는 공공데이터를 그대로
  돌려줍니다. 투자 판단은 사용자 몫입니다.
- **배치로 수집한 데이터**입니다. 실시간이 아니며 기준 시각은 각 응답의 `meta.synced_at` 에
  있습니다. 입찰 전에는 반드시 온비드 원문을 확인하세요.
- 결과가 0건이면 `no_result` 오류로 알립니다. 빈 목록과 조회 실패를 구분하세요.
"""


def configure_logging(level: int = logging.INFO) -> None:
    """**stderr 로만** 로그를 낸다 (F6.1).

    stdout 은 JSON-RPC 채널이라 한 줄이라도 섞이면 프레임이 깨진다. 두 번 불러도 핸들러가
    쌓이지 않게 한다 — 쌓이면 같은 줄이 여러 번 찍힌다.

    Args:
        level: 로깅 레벨.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)

    # 요청 URL 에 온비드·카카오 키가 실린다 (N4.5). 재확인해 둔다.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_server() -> MCPServer:
    """서버 인스턴스를 만든다.

    **전역 하나를 쓰지 않는다** — 테스트마다 깨끗한 인스턴스가 필요하고, 전역이면 등록
    상태가 테스트 사이로 샌다.

    Returns:
        툴이 아직 붙지 않은 서버. 툴·Resource 는 다음 태스크에서 등록한다.
    """
    return MCPServer(
        name=SERVER_NAME,
        version=VERSION,
        instructions=INSTRUCTIONS,
    )


def main() -> None:
    """stdio 로 서버를 띄운다."""
    configure_logging()
    server = build_server()
    logger.info("MCP 서버 시작 (stdio): %s v%s", SERVER_NAME, VERSION)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
