"""리포트용 요약 데이터 생성. 개별 물건 나열이 아니라 분포·표본 중심이다 (SPEC §2.4)."""

import asyncio
import json
import pathlib
import sys
from collections import Counter
from typing import Any

sys.path.insert(0, ".")

from core.codes.address import fetch_address_list  # noqa: E402
from core.codes.usage import fetch_usage_tree  # noqa: E402
from core.onbid.bidinfo import collect_bid_details, select_bid_targets  # noqa: E402
from core.onbid.client import OnbidClient  # noqa: E402
from core.onbid.collector import collect_listings  # noqa: E402
from core.onbid.parser import as_int, as_str  # noqa: E402

OUT = pathlib.Path("data/report.json")


def rate(row: dict[str, Any]) -> float | None:
    low, apr = as_int(row.get("lowstBidPrcIndctCont")), as_int(row.get("apslEvlAmt"))
    return round(low / apr, 4) if low and apr else None


async def main() -> None:
    from core.config import Settings  # noqa: PLC0415

    key = Settings.load().require("onbid_service_key")
    async with OnbidClient(service_key=key) as client:
        listing = await collect_listings(client, page_size=5000)
        rows = [dict(i.raw) for i in listing.items]
        for row in rows:
            row["_rate"] = rate(row)

        targets = select_bid_targets(listing.items)
        bids = await collect_bid_details(client, targets, budget=40)
        tree = await fetch_usage_tree(client, max_depth=2)
        addrs = await fetch_address_list(client, sd_nm="서울특별시")

    rates = sorted(r["_rate"] for r in rows if r["_rate"])
    buckets = Counter(min(int(r * 10), 10) for r in rates)
    fails = Counter(as_int(r.get("usbdNft")) or 0 for r in rows)
    rounds = [r for d in bids.details for r in d.rounds]

    def top(field: str, n: int = 30) -> list[list[Any]]:
        return [[k, v] for k, v in Counter(r.get(field) for r in rows).most_common(n)]

    # 표본은 개별 물건 나열이 아니라 "이런 모양의 데이터가 들어온다"를 보이는 용도다.
    sample = [
        {
            "sgg": as_str(r.get("lctnSggnm")), "emd": as_str(r.get("lctnEmdNm")),
            "usage": as_str(r.get("cltrUsgSclsCtgrNm")), "prpt": as_str(r.get("prptDivNm")),
            "appraisal": as_int(r.get("apslEvlAmt")), "low": as_int(r.get("lowstBidPrcIndctCont")),
            "rate": r["_rate"], "fail": as_int(r.get("usbdNft")),
            "pnu": bool(as_str(r.get("ltnoPnu"))), "status": as_str(r.get("pbctStatNm")),
        }
        for r in sorted((r for r in rows if r["_rate"]), key=lambda r: r["_rate"])[:60]
    ]

    payload = {
        "collectedAt": "2026-08-22",
        "listing": {
            "total": listing.collected, "pages": listing.pages_fetched,
            "elapsed": round(listing.elapsed_sec, 1),
            "byGroup": listing.total_by_group,
        },
        "rate": {
            "count": len(rates), "coverage": round(len(rates) / len(rows), 4),
            "min": rates[0], "median": rates[len(rates) // 2], "max": rates[-1],
            "buckets": [[b * 10, buckets.get(b, 0)] for b in range(11)],
        },
        "fails": [[k, fails[k]] for k in sorted(fails)][:16],
        "zeroFailShare": round(fails[0] / len(rows), 4),
        "prptDiv": top("prptDivNm"),
        "usageMcls": top("cltrUsgMclsCtgrNm"),
        "sgg": top("lctnSggnm", 25),
        "status": top("pbctStatNm"),
        "fill": {
            f: round(sum(1 for r in rows if r.get(f) not in (None, "", "-")) / len(rows), 4)
            for f in ("apslEvlAmt", "ltnoPnu", "rdnmPnu", "lctnEmdNm", "landSqms", "bldSqms",
                      "apslPrcCtrsLowstBidRto", "feeRate")
        },
        "bid": {
            "targets": len(targets), "sampled": bids.collected, "rounds": bids.rounds_collected,
            "noData": bids.no_data,
            "roundResults": [[k, v] for k, v in
                             Counter(r.get("pbctStatNm") for r in rounds).most_common()],
            "won": [
                {"nsq": as_str(r.get("pbctNsq")), "low": as_int(r.get("lowstBidPrcIndctCont")),
                 "win": as_int(r.get("scfbAmt"))}
                for r in rounds if as_int(r.get("scfbAmt"))
            ][:8],
        },
        "codes": {"usageMid": len([n for n in tree if n.depth == 2]),
                  "addrCombos": len(addrs), "districts": len({a.sgg_nm for a in addrs})},
        "sample": sample,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    size = OUT.stat().st_size
    print(f"{OUT} ({size:,} bytes) · 물건 {listing.collected}건 · 표본 {len(sample)}행")


if __name__ == "__main__":
    asyncio.run(main())
