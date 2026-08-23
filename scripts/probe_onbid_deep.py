"""온비드 심층 프로브 — 스모크에서 드러난 이상 징후를 대표 표본으로 재확인한다."""

import sys
from collections import Counter

sys.path.insert(0, "scripts")
import urllib.parse  # noqa: E402

from smoke_onbid import PRPT_DIV_ALL, body_of, call, header_of, items_of, load_env  # noqa: E402

OP = "getRlstCltrList2"
env = load_env()
KEY = urllib.parse.unquote(env["ONBID_SERVICE_KEY"])

base = {
    "serviceKey": KEY, "resultType": "json",
    "prptDivCd": PRPT_DIV_ALL, "dspsMthodCd": "0001", "lctnSdnm": "서울특별시",
}

print("=" * 72)
print("[A] pvctTrgtYn 별 totalCount")
counts = {}
for pvct in ("N", "Y"):
    _, pl = call(OP, dict(base, pvctTrgtYn=pvct, pageNo=1, numOfRows=1))
    counts[pvct] = body_of(pl).get("totalCount")
    print(f"  pvctTrgtYn={pvct}: totalCount={counts[pvct]}")

print()
print("=" * 72)
print("[B] numOfRows 상한 재탐색")
for n in (1000, 2000, 5000):
    _, pl = call(OP, dict(base, pvctTrgtYn="N", pageNo=1, numOfRows=n))
    code = header_of(pl).get("resultCode")
    print(f"  numOfRows={n:<5} resultCode={code} · 수신 {len(items_of(pl))}건")

print()
print("=" * 72)
print("[C] 전량 표본 수집 (numOfRows=1000, 전 페이지)")
sample = []
for pvct in ("N", "Y"):
    page = 1
    while True:
        _, pl = call(OP, dict(base, pvctTrgtYn=pvct, pageNo=page, numOfRows=1000))
        got = items_of(pl)
        sample.extend(got)
        if len(got) < 1000 or page >= 10:
            break
        page += 1
print(f"  총 {len(sample)}건 수집")

def pct(n: int) -> str:
    return f"{n * 100 / max(len(sample), 1):.1f}%"

print()
print("[D14] pbctStatCd 분포")
for c, n in Counter(f"{i.get('pbctStatCd')} {i.get('pbctStatNm')}" for i in sample).most_common():
    print(f"  {c:24} {n:6}건  {pct(n)}")

print()
print("[D13] 온비드 제공 비율 필드의 실제 채움률")
for f in ("apslPrcCtrsLowstBidRto", "frstCtrsLowstBidPrcRto", "feeRate", "frstBidPrc"):
    filled = [i[f] for i in sample if i.get(f) not in (None, "", 0, "0")]
    ex = f" · 예시 {filled[:5]}" if filled else ""
    print(f"  {f:26} 채움 {len(filled):5}건 ({pct(len(filled))}){ex}")

print()
print("[D12] lowstBidPrcIndctCont 비수치 표기")
non: Counter[str] = Counter()
for i in sample:
    v = str(i.get("lowstBidPrcIndctCont", "") or "")
    if v and not v.replace(",", "").isdigit():
        non[v[:30]] += 1
for v, n in non.most_common(10):
    print(f"  {n:6}건  {v!r}")
if not non:
    print("  없음")

print()
print("[E] 감정가·PNU·좌표 근거 필드 결측률")
for f in ("apslEvlAmt", "ltnoPnu", "rdnmPnu", "lctnEmdNm", "landSqms", "bldSqms"):
    miss = sum(1 for i in sample if not i.get(f))
    print(f"  {f:14} 결측 {miss:6}건 ({pct(miss)})")

print()
print("[F] 입찰일시 이상값 (2999년 = 일정 미정 추정)")
sent = sum(1 for i in sample if str(i.get("cltrBidBgngDt", "")).startswith("2999"))
print(f"  cltrBidBgngDt 가 2999로 시작: {sent}건 ({pct(sent)})")
end_dates = sorted({str(i.get("cltrBidEndDt")) for i in sample})
print(f"  cltrBidEndDt 값 범위: 최소 {end_dates[0]} / 최대 {end_dates[-1]}")

print()
print("[G] 코드표 대비 실제 코드명 (가이드 불일치 탐색)")
CODE_PAIRS = (("bidDivCd", "bidDivNm"), ("prptDivCd", "prptDivNm"),
              ("cltrUsgMclsCtgrId", "cltrUsgMclsCtgrNm"))
for f, nf in CODE_PAIRS:
    pairs = Counter(f"{i.get(f)}={i.get(nf)}" for i in sample)
    print(f"  {f}: " + ", ".join(f"{k}({v})" for k, v in pairs.most_common(6)))

print()
print("[H] 가이드에 없던 필드")
guide = {
    "onbidCltrno","cltrMngNo","pbctCdtnNo","onbidPbancNo","pbctNo","pbctNsq","pbctsn","prptDivCd",
    "prptDivNm","dspsMthodCd","dspsMthodNm","bidDivCd","bidDivNm","bidMthodCd","bidMthodNm",
    "cptnMthodCd","cptnMthodNm","totalamtUnpcDivCd","totalamtUnpcDivNm","cltrUsgLclsCtgrId",
    "cltrUsgMclsCtgrId","cltrUsgSclsCtgrId","cltrUsgLclsCtgrNm","cltrUsgMclsCtgrNm",
    "cltrUsgSclsCtgrNm","onbidCltrNm","usbdNft","bidPrgnNft","pvctTrgtYn","batcBidYn",
    "cltrBidBgngDt","cltrBidEndDt","apslEvlAmt","lowstBidPrcIndctCont","frstBidPrc",
    "apslPrcCtrsLowstBidRto","frstCtrsLowstBidPrcRto","ltnoPnu","rdnmPnu","lctnSdnm","lctnSggnm",
    "lctnEmdNm","rqstOrgNm","orgNm","eltrGrprUseYn","collbBidPsblYn","twtmGthrBidPsblYn",
    "subtBidPsblYn","evcRsbyTrgtCont","landSqms","bldSqms","alcYn","dtbtRqrEdtmCont",
    "rentMthodNm","rentPerdCont","thnlImgUrlAdr","mdfcnDt","crtnYn","feeRate","pbctStatCd","pbctStatNm",
}
actual = set()
for i in sample[:200]:
    actual |= set(i.keys())
print(f"  응답에만 있음: {sorted(actual - guide)}")
print(f"  가이드에만 있음: {sorted(guide - actual)}")

print()
print("[I] 최저가율 자체 계산 가능성 (lowstBidPrcIndctCont / apslEvlAmt)")
ok = 0
for i in sample:
    lo = str(i.get("lowstBidPrcIndctCont", "") or "").replace(",", "")
    ap = i.get("apslEvlAmt")
    if lo.isdigit() and ap:
        ok += 1
print(f"  계산 가능 {ok}건 ({pct(ok)})")
