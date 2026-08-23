"""물건상세 입찰정보(getCltrBidInf2) 프로브 — SPEC §14 D8 해소용.

일일 트래픽 1,000건 제약이 있으므로 표본 수를 작게 유지한다.
"""

import sys
import urllib.parse
from collections import Counter

sys.path.insert(0, "scripts")
from smoke_onbid import PRPT_DIV_ALL, body_of, call, items_of, load_env  # noqa: E402

SVC = "OnbidCltrBidDtlSrvc2"
OP = "getCltrBidInf2"
KEY = urllib.parse.unquote(load_env()["ONBID_SERVICE_KEY"])
SAMPLE_N = 15  # 트래픽 보호

# 1) 유찰이 많은 물건을 목록에서 고른다
base = {"serviceKey": KEY, "resultType": "json", "prptDivCd": PRPT_DIV_ALL,
        "dspsMthodCd": "0001", "lctnSdnm": "서울특별시", "pvctTrgtYn": "N",
        "pageNo": 1, "numOfRows": 1000, "usbdNftStart": 3}
_, pl = call("getRlstCltrList2", base)
cands = items_of(pl)
print(f"유찰 3회 이상 후보 {len(cands)}건 (totalCount={body_of(pl).get('totalCount')})")
targets = cands[:SAMPLE_N]

rounds: list[int] = []
statuses: Counter[str] = Counter()
scfb_filled = rate_filled = 0
for t in targets:
    _, d = call(OP, {"serviceKey": KEY, "resultType": "json", "pageNo": 1, "numOfRows": 10,
                     "cltrMngNo": t["cltrMngNo"], "pbctCdtnNo": t["pbctCdtnNo"]}, service=SVC)
    it = items_of(d)
    if not it:
        continue
    hist = it[0].get("prcnBidClgList") or []
    rounds.append(len(hist))
    for h in hist:
        statuses[h.get("pbctStatNm")] += 1
        if h.get("scfbAmt") not in (None, "", 0, "0"):
            scfb_filled += 1
        if h.get("apslPrcCtrsLowstBidRto") not in (None, "", 0, "0"):
            rate_filled += 1

total_rounds = sum(rounds)
avg = total_rounds / max(len(rounds), 1)
print(f"\n조회 {len(rounds)}건 · 회차 이력 총 {total_rounds}행 (물건당 평균 {avg:.1f})")
print("\n[prcnBidClgList.pbctStatNm 분포]")
for k, v in statuses.most_common():
    print(f"  {str(k):10} {v:4}행")
print(f"\n[낙찰가(scfbAmt) 채움] {scfb_filled}/{total_rounds}행")
print(f"[비율(apslPrcCtrsLowstBidRto) 채움] {rate_filled}/{total_rounds}행")

# 2) 저감 패턴 — 한 물건의 회차별 최저가 추이
t0 = targets[0]
_, d = call(OP, {"serviceKey": KEY, "resultType": "json", "pageNo": 1, "numOfRows": 10,
                 "cltrMngNo": t0["cltrMngNo"], "pbctCdtnNo": t0["pbctCdtnNo"]}, service=SVC)
sample_item = items_of(d)[0]
print(f"\n[저감 패턴 예시] {sample_item.get('onbidCltrNm')} "
      f"· 재산유형={targets[0].get('prptDivNm')}")
prev = None
for h in (sample_item.get("prcnBidClgList") or []):
    amt = h.get("lowstBidPrcIndctCont")
    try:
        a: int | None = int(str(amt))
    except (TypeError, ValueError):
        a = None
    drop = f"{(1 - a / prev) * 100:5.2f}% 하락" if (a and prev) else ""
    nsq, dt = h.get("pbctNsq"), h.get("cltrOpbdDt")
    print(f"  {nsq:>3}회차 {dt} {str(h.get('pbctStatNm')):6} {str(amt):>14} {drop}")
    if a:
        prev = a
