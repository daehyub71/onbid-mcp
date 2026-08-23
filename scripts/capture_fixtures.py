"""실응답을 테스트 fixture로 저장한다 (PLAN §5.2).

서비스키는 요청 URL에만 실리고 응답 본문에는 없으므로, **본문만** 저장하면 안전하다.
저장 후 키 문자열이 섞여 들어가지 않았는지 한 번 더 검사한다.
"""

import asyncio
import json
import pathlib
import re
import sys
from typing import Any

sys.path.insert(0, ".")

from core.onbid.client import OnbidClient  # noqa: E402

OUT = pathlib.Path("tests/fixtures/onbid")
ROOT = pathlib.Path(__file__).resolve().parents[1]


def service_key() -> str:
    text = (ROOT / ".env").read_text(encoding="utf-8")
    match = re.search(r"^ONBID_SERVICE_KEY=(.*)$", text, re.M)
    assert match, ".env 에 ONBID_SERVICE_KEY 가 없다"
    return match.group(1).strip()


def save(name: str, payload: Any, key: str) -> None:
    """본문을 저장하되 서비스키가 섞였는지 검사한다."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    import urllib.parse

    for variant in {key, urllib.parse.unquote(key), urllib.parse.quote(key, safe="")}:
        if variant and variant in text:
            raise SystemExit(f"{name}: 응답 본문에 서비스키가 포함됐다 — 저장 중단")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(text + "\n", encoding="utf-8")
    print(f"  저장 {name}.json ({len(text):,} bytes)")


async def main() -> None:
    key = service_key()
    seoul = {"dspsMthodCd": "0001", "lctnSdnm": "서울특별시"}
    async with OnbidClient(service_key=key) as client:
        cases: list[tuple[str, str, dict[str, Any]]] = [
            ("list_single", "realestate_list",
             {"pageNo": 1, "numOfRows": 1, "prptDivCd": "0007", "pvctTrgtYn": "N", **seoul}),
            ("list_many", "realestate_list",
             {"pageNo": 1, "numOfRows": 3, "prptDivCd": "0007", "pvctTrgtYn": "N", **seoul}),
            ("list_empty", "realestate_list",
             {"pageNo": 1, "numOfRows": 1, "prptDivCd": "0007", "pvctTrgtYn": "N",
              "lctnSggnm": "존재하지않는구", **seoul}),
            ("usage_code_root", "usage_code", {"pageNo": 1, "numOfRows": 10,
                                               "upCtgrId": "10000"}),
            ("address_seoul", "address", {"pageNo": 1, "numOfRows": 5, "sdnm": "서울특별시",
                                          "sggnm": "강남구"}),
        ]
        for name, endpoint, params in cases:
            resp = await client.call(endpoint, **params)
            save(name, resp.payload, key)

        # 회차 이력이 1건뿐인 물건 — 중첩 배열의 단건 모양 확인용
        for lo, hi in ((1, 1), (2, 2)):
            listing = await client.call(
                "realestate_list", pageNo=1, numOfRows=1, prptDivCd="0007",
                pvctTrgtYn="N", usbdNftStart=lo, usbdNftEnd=hi, **seoul)
            if listing.is_no_data:
                continue
            item = listing.payload["body"]["items"]["item"][0]
            detail = await client.call(
                "bid_detail", pageNo=1, numOfRows=10,
                cltrMngNo=item["cltrMngNo"], pbctCdtnNo=item["pbctCdtnNo"])
            rounds = detail.payload["body"]["items"]["item"][0]["prcnBidClgList"]
            print(f"  유찰 {lo}회 물건: prcnBidClgList 타입={type(rounds).__name__} "
                  f"길이={len(rounds) if isinstance(rounds, list) else 'N/A'}")
            save(f"bid_detail_usbd{lo}", detail.payload, key)


if __name__ == "__main__":
    asyncio.run(main())
