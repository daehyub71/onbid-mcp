"""MCP 서버 연결 점검 — stdio 로 실제 호출해 본다.

Claude Desktop 에 붙이기 전에 **서버 자체가 도는지** 먼저 확인한다. 클라이언트에서 실패하면
설정 문제인지 서버 문제인지 가려내기 어렵다.

실행::

    python scripts/mcp_smoke.py
    python scripts/mcp_smoke.py --region 서초구

stdout 은 JSON-RPC 채널이므로 서버의 로그는 stderr 로 흘려보낸다 — 여기서는 조용히 버린다.
"""

import argparse
import asyncio
import json
import sys
from typing import Any

TIMEOUT = 30.0


async def _rpc(proc: Any, method: str, params: dict[str, Any], rid: int) -> dict[str, Any]:
    """요청 하나를 보내고 응답 한 줄을 읽는다."""
    payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    proc.stdin.write((json.dumps(payload) + "\n").encode())
    await proc.stdin.drain()
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=TIMEOUT)
    if not line:
        raise SystemExit("서버가 응답하지 않는다 — stderr 를 확인한다")
    body: dict[str, Any] = json.loads(line)
    if "error" in body:
        raise SystemExit(f"{method} 실패: {body['error']}")
    return body


async def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 서버를 stdio 로 점검한다")
    parser.add_argument("--region", default="강남구")
    parser.add_argument("--limit", type=int, default=2)
    args = parser.parse_args()

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "onbid_mcp.server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    try:
        await _rpc(proc, "initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"}}, 1)
        assert proc.stdin is not None
        proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
        await proc.stdin.drain()

        tools = await _rpc(proc, "tools/list", {}, 2)
        print("툴     ", ", ".join(t["name"] for t in tools["result"]["tools"]))

        found = await _rpc(proc, "resources/list", {}, 3)
        print("리소스 ", ", ".join(r["uri"] for r in found["result"]["resources"]))

        call = await _rpc(proc, "tools/call", {
            "name": "search_auction_items",
            "arguments": {"region": args.region, "limit": args.limit}}, 4)
        body = json.loads(call["result"]["content"][0]["text"])

        if "error" in body:
            print(f"\n검색  {body['error']['code']}: {body['error']['message']}")
        else:
            print(f"\n검색  {args.region} → {len(body['items'])}건 "
                  f"(전체 {body['total_count']:,}건)")
            for item in body["items"]:
                amount = item["min_bid_amt"]
                print(f"  {(item['cltr_nm'] or '')[:34]:36} "
                      f"{amount:>13,}원" if amount else f"  {item['cltr_nm']}")

        status = await _rpc(proc, "resources/read",
                            {"uri": "onbid://dataset/status"}, 5)
        dataset = json.loads(status["result"]["contents"][0]["text"])
        print(f"\n데이터 {dataset['total_count']:,}건 · "
              f"좌표율 {dataset['geocode_ok_rate']:.1%} · 기준 {str(dataset['synced_at'])[:19]}")
        print("\n✅ 서버 정상")
    finally:
        proc.terminate()
        await proc.wait()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
