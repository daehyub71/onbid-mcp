"""서울 전량을 수집해 검토용 파일로 남긴다.

산출물은 `data/` 에 쓰며 `.gitignore` 대상이다 — 실물 데이터는 커밋하지 않는다.
"""

import asyncio
import csv
import json
import pathlib
import sys
from collections import Counter
from typing import Any

sys.path.insert(0, ".")

from core.onbid.client import OnbidClient  # noqa: E402
from core.onbid.collector import collect_listings  # noqa: E402
from core.onbid.parser import as_bool_yn, as_float, as_int, as_str  # noqa: E402

OUT = pathlib.Path("data")
STAMP = "20260822"

COLUMNS = [
    ("cltrMngNo", "물건관리번호"), ("pbctCdtnNo", "공매조건번호"),
    ("pvctTrgtYn", "수의계약가능"), ("prptDivNm", "재산유형"),
    ("cltrUsgMclsCtgrNm", "용도중분류"), ("cltrUsgSclsCtgrNm", "용도소분류"),
    ("lctnSggnm", "시군구"), ("lctnEmdNm", "읍면동"), ("onbidCltrNm", "물건명"),
    ("apslEvlAmt", "감정가"), ("lowstBidPrcIndctCont", "최저입찰가"),
    ("_minBidRate", "최저가율"), ("usbdNft", "유찰횟수"),
    ("cltrBidEndDt", "입찰종료"), ("pbctStatNm", "입찰상태"),
    ("landSqms", "토지면적"), ("bldSqms", "건물면적"),
    ("alcYn", "지분물건"), ("ltnoPnu", "지번PNU"), ("orgNm", "공고기관"),
]


def min_bid_rate(row: dict[str, Any]) -> float | None:
    """최저가율 = 최저입찰가 / 감정가. 온비드가 주는 비율 필드는 채움률 0%라 직접 계산한다."""
    low, appraisal = as_int(row.get("lowstBidPrcIndctCont")), as_int(row.get("apslEvlAmt"))
    if not low or not appraisal:
        return None
    return round(low / appraisal, 4)


async def main() -> None:
    from core.config import Settings  # noqa: PLC0415

    key = Settings.load().require("onbid_service_key")
    OUT.mkdir(exist_ok=True)

    async with OnbidClient(service_key=key) as client:
        result = await collect_listings(
            client, page_size=5000,
            on_page=lambda g, p, n: print(f"  pvctTrgtYn={g} page={p} → {n}건"),
        )

    # 원본은 손대지 않는다 (F1.3). 파생 값은 사본에 얹는다.
    rows = [{**item.raw, "pvctTrgtYnGroup": item.group} for item in result.items]
    for row in rows:
        row["_minBidRate"] = min_bid_rate(row)

    # 1) 원본 JSON — 필드 하나도 빠뜨리지 않는다
    raw_path = OUT / f"onbid_seoul_raw_{STAMP}.json"
    raw_path.write_text(json.dumps([dict(i.raw) for i in result.items],
                               ensure_ascii=False, indent=1), encoding="utf-8")

    # 2) 검토용 CSV — 엑셀에서 열리도록 BOM 포함
    csv_path = OUT / f"onbid_seoul_{STAMP}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow([label for _, label in COLUMNS])
        for row in sorted(rows, key=lambda r: (r.get("_minBidRate") or 9, )):
            writer.writerow([row.get(field) for field, _ in COLUMNS])

    print(f"\n{result.summary()}")
    print(f"  {raw_path} ({raw_path.stat().st_size:,} bytes)")
    print(f"  {csv_path} ({csv_path.stat().st_size:,} bytes)")

    # 요약 통계
    rates = [r["_minBidRate"] for r in rows if r["_minBidRate"]]
    print(f"\n[최저가율] 산출 {len(rates)}/{result.collected}건 "
          f"({len(rates) * 100 // result.collected}%)")
    if rates:
        rates.sort()
        for label, value in (("최저", rates[0]), ("중앙", rates[len(rates) // 2]),
                             ("최고", rates[-1])):
            print(f"  {label} {value:.1%}")
        buckets = Counter(min(int(r * 10), 10) for r in rates)
        print("  구간별:", " ".join(f"{b*10}~{b*10+9}%:{c}" for b, c in sorted(buckets.items())))

    print("\n[재산유형]", dict(Counter(r.get("prptDivNm") for r in rows).most_common()))
    print("[입찰상태]", dict(Counter(r.get("pbctStatNm") for r in rows).most_common()))
    print("[유찰횟수]", dict(sorted(Counter(as_int(r.get("usbdNft")) or 0
                                          for r in rows).items())[:12]))
    print("[시군구 상위]", dict(Counter(r.get("lctnSggnm") for r in rows).most_common(8)))
    unused = (as_str, as_float, as_bool_yn)  # noqa: F841 — 열 확장 시 사용


if __name__ == "__main__":
    asyncio.run(main())
