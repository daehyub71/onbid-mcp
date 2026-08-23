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

import json
import logging
import sys
from typing import Any, Final

from mcp.server import MCPServer

import core.onbid.client  # noqa: F401  # httpx 요청 로그 차단 (N4.5)
from core.config import Settings
from core.geocoder.kakao import KakaoClient
from core.store.connection import Database
from onbid_mcp import resources as resource_readers
from onbid_mcp.common import ToolError, error_response
from onbid_mcp.errors import to_tool_error
from onbid_mcp.tools.detail import get_auction_detail
from onbid_mcp.tools.geocode import get_address_geocode
from onbid_mcp.tools.search import search_auction_items
from onbid_mcp.tools.stats import get_auction_stats

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


TOOL_NOTICE: Final = (
    "\n\n[공통] 이 툴은 판단하지 않습니다 — 추천·평가·순위를 매기지 않고 공공데이터를 그대로 "
    "돌려줍니다. 실시간이 아니라 배치로 수집한 값이며 기준 시각은 `meta.synced_at` 에 있습니다. "
    "입찰 전에는 반드시 온비드 원문을 확인하세요."
)
"""모든 툴 설명 끝에 붙는다 (F6.9). 하나라도 빠지면 그 툴에서 단정적인 답이 새어 나간다."""

_database = Database()


def _describe(summary: str) -> str:
    """툴 설명에 공통 고지를 덧붙인다 (F6.9)."""
    return summary + TOOL_NOTICE


async def _run_tool(handler: Any, **kwargs: Any) -> str:
    """툴을 실행하고 **오류까지 규약대로** 돌려준다 (§8.7).

    예외를 그대로 올리면 MCP 가 일반 오류로 감싸 `code` 가 사라진다 — LLM 이 어떻게 행동할지
    알 수 없게 된다.
    """
    echo = {k: v for k, v in kwargs.items() if v is not None}
    try:
        body = await handler(**kwargs)
    except ToolError as exc:
        body = error_response(exc, query_echo=echo)
    except Exception as exc:  # noqa: BLE001 - 어떤 예외도 규약 안으로 넣는다
        body = error_response(to_tool_error(exc), query_echo=echo)
    return json.dumps(body, ensure_ascii=False, default=str)


def _register_tools(server: MCPServer) -> None:
    """툴 4종을 등록한다 (F6.2). 설명에는 세 취지를 반드시 담는다 (F6.9)."""

    @server.tool(
        name="search_auction_items",
        description=_describe(
            "온비드 공매 물건을 조건으로 검색합니다. 지역·용도·재산유형·수의계약 여부·"
            "가격·최저가율·유찰횟수·마감일로 좁힐 수 있습니다. 지역과 용도는 한글 명칭을 "
            "그대로 쓰면 되고, 용도를 중분류로 주면 하위 소분류까지 확장해 조회합니다. "
            "결과가 없으면 no_result 오류로 알립니다(빈 목록이 아닙니다)."),
    )
    async def search(  # noqa: PLR0913
        region: str | None = None, usage: str | None = None,
        prpt_div: str | None = None, pvct_trgt: str | None = None,
        min_price: int | None = None, max_price: int | None = None,
        min_rate: float | None = None, max_rate: float | None = None,
        min_fail_cnt: int | None = None,
        bid_end_after: str | None = None, bid_end_before: str | None = None,
        status: str | None = None, sort: str | None = None,
        limit: int = 20, cursor: str | None = None,
    ) -> str:
        conn = await _database.connect()
        return await _run_tool(
            search_auction_items, conn=conn, region=region, usage=usage,
            prpt_div=prpt_div, pvct_trgt=pvct_trgt, min_price=min_price,
            max_price=max_price, min_rate=min_rate, max_rate=max_rate,
            min_fail_cnt=min_fail_cnt, bid_end_after=bid_end_after,
            bid_end_before=bid_end_before, status=status, sort=sort,
            limit=limit, cursor=cursor)

    @server.tool(
        name="get_auction_detail",
        description=_describe(
            "물건관리번호로 단건 상세를 조회합니다. 한 물건관리번호에 공매조건번호가 여러 개 "
            "붙을 수 있어, 조건을 지정하지 않으면 최신 회차를 주고 형제 조건번호를 "
            "meta.sibling_conditions 에 함께 알립니다."),
    )
    async def detail(cltr_mng_no: str, pbct_cdtn_no: str | None = None) -> str:
        conn = await _database.connect()
        return await _run_tool(get_auction_detail, conn=conn,
                               cltr_mng_no=cltr_mng_no, pbct_cdtn_no=pbct_cdtn_no)

    @server.tool(
        name="get_auction_stats",
        description=_describe(
            "적재된 물건의 분포를 집계합니다. group_by 는 min_bid_rate_bucket / fail_cnt / "
            "usage / region / prpt_div / pvct_trgt / win_rate 중 하나입니다. 개별 물건은 "
            "돌려주지 않습니다. 재산유형을 지정하지 않으면 저감 체계가 다른 유형이 섞이므로 "
            "meta.caveat 을 함께 읽으세요."),
    )
    async def stats(group_by: str, region: str | None = None,
                    prpt_div: str | None = None, status: str | None = None) -> str:
        conn = await _database.connect()
        return await _run_tool(get_auction_stats, conn=conn, group_by=group_by,
                               region=region, prpt_div=prpt_div, status=status)

    @server.tool(
        name="get_address_geocode",
        description=_describe(
            "주소를 좌표로 변환합니다. 외부 지도 API 를 쓰므로 **일일 호출 상한**이 있으며 "
            "남은 횟수는 meta.daily_remaining 에 있습니다. 같은 주소는 캐시에서 답하므로 "
            "상한을 소모하지 않습니다."),
    )
    async def geocode(address: str) -> str:
        conn = await _database.connect()
        key = Settings.load().require("kakao_rest_api_key")
        async with KakaoClient(rest_api_key=key) as kakao:
            return await _run_tool(get_address_geocode, conn=conn,
                                   address=address, kakao=kakao)


def _register_resources(server: MCPServer) -> None:
    """Resource 4종을 등록한다 (F7). 툴로 만들면 검색마다 왕복이 늘어난다 (§8.5)."""

    @server.resource("onbid://codes/regions", name="지역 코드",
                     description="물건이 실제로 존재하는 서울 시군구·읍면동 조합",
                     mime_type="application/json")
    async def regions() -> str:
        return await resource_readers.read_regions(await _database.connect())

    @server.resource("onbid://codes/usages", name="용도 코드",
                     description="부동산 용도 3단 계층 트리 (대/중/소분류)",
                     mime_type="application/json")
    async def usages() -> str:
        return await resource_readers.read_usages(await _database.connect())

    @server.resource("onbid://codes/property-types", name="재산유형 코드",
                     description="재산유형 코드표", mime_type="application/json")
    async def property_types() -> str:
        return await resource_readers.read_property_types()

    @server.resource("onbid://dataset/status", name="데이터셋 상태",
                     description="최근 배치 시각·총 건수·상태별 건수·지오코딩 성공률",
                     mime_type="application/json")
    async def dataset_status() -> str:
        return await resource_readers.read_dataset_status(await _database.connect())


def build_server() -> MCPServer:
    """서버 인스턴스를 만든다.

    **전역 하나를 쓰지 않는다** — 테스트마다 깨끗한 인스턴스가 필요하고, 전역이면 등록
    상태가 테스트 사이로 샌다.

    Returns:
        툴 4종·Resource 4종이 등록된 서버.
    """
    server = MCPServer(
        name=SERVER_NAME,
        version=VERSION,
        instructions=INSTRUCTIONS,
    )
    _register_tools(server)
    _register_resources(server)
    return server


def main() -> None:
    """stdio 로 서버를 띄운다."""
    configure_logging()
    server = build_server()
    logger.info("MCP 서버 시작 (stdio): %s v%s", SERVER_NAME, VERSION)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
