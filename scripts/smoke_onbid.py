"""온비드 API 실호출 스모크 — SPEC §14 미확정 항목(D11~D15) 해소용.

의존성 없이 표준 라이브러리만 사용한다(패키지 설치 전에도 실행 가능).
실행: python3 scripts/smoke_onbid.py
"""

import json
import pathlib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any

BASE = "https://apis.data.go.kr/B010003"
PRPT_DIV_ALL = "0002,0003,0004,0005,0006,0007,0008,0010,0011,0013"


def load_env(path: str = ".env") -> dict[str, str]:
    """.env를 파싱해 dict로 반환한다 (python-dotenv 없이)."""
    env: dict[str, str] = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _ssl_context() -> ssl.SSLContext:
    """macOS 시스템 파이썬의 인증서 체인 미설정 문제를 certifi로 우회한다."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def call(op: str, params: dict[str, Any], service: str = "OnbidRlstListSrvc2") -> tuple[int, Any]:
    """온비드 API를 호출해 (HTTP 상태, 파싱된 본문)을 반환한다."""
    url = f"{BASE}/{service}/{op}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    ctx = _ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600]
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body[:600]


def items_of(payload: Any) -> list[dict[str, Any]]:
    """items.item이 단건일 때 배열이 아닌 케이스를 흡수한다 (SPEC §6.4)."""
    if not isinstance(payload, dict):
        return []
    body = payload.get("body") or payload.get("response", {}).get("body") or {}
    items = body.get("items") or {}
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    return items if isinstance(items, list) else []


def header_of(payload: Any) -> dict[str, Any]:
    """정상 응답과 포털 게이트웨이 오류 봉투를 모두 처리한다.

    정상: {"header": {"resultCode": "00", ...}}
    게이트웨이 오류: {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": "12", ...}}}
    """
    if not isinstance(payload, dict):
        return {}
    gw = payload.get("OpenAPI_ServiceResponse")
    if isinstance(gw, dict):
        h = gw.get("cmmMsgHeader", {})
        return {"resultCode": h.get("returnReasonCode"), "resultMsg": h.get("errMsg")}
    return payload.get("header") or payload.get("response", {}).get("header") or {}


def body_of(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return payload.get("body") or payload.get("response", {}).get("body") or {}


def main() -> int:
    env = load_env()
    raw = env.get("ONBID_SERVICE_KEY", "")
    if not raw:
        print("ONBID_SERVICE_KEY 가 .env에 없습니다.")
        return 1
    # Encoding/Decoding 어느 쪽이 와도 정규화 (SPEC §6.4)
    key = urllib.parse.unquote(raw)

    base_params = {
        "serviceKey": key,
        "resultType": "json",
        "pageNo": 1,
        "numOfRows": 10,
        "prptDivCd": PRPT_DIV_ALL,
        "pvctTrgtYn": "N",
        "dspsMthodCd": "0001",
        "lctnSdnm": "서울특별시",
    }

    # ── D11: 오퍼레이션명 확인 ────────────────────────────────
    print("=" * 70)
    print("[D11] 오퍼레이션명 판별")
    winner = None
    for op in ("getRlstCltrList2", "getRlstCltrList"):
        status, payload = call(op, base_params)
        hdr = header_of(payload)
        code = hdr.get("resultCode")
        msg = hdr.get("resultMsg")
        n = len(items_of(payload))
        print(f"  {op:22} HTTP {status} · resultCode={code} · msg={msg} · items={n}")
        if code == "00" and winner is None:
            winner = op
        if not isinstance(payload, dict):
            print(f"    ↳ 비JSON 응답: {str(payload)[:200]}")
    if not winner:
        print("  → 정상 응답 없음. 상세는 위 출력 참조.")
        return 2
    print(f"  ✅ 사용할 오퍼레이션: {winner}")

    # ── 기준 응답 확보 ───────────────────────────────────────
    status, payload = call(winner, base_params)
    body = body_of(payload)
    items = items_of(payload)
    total = body.get("totalCount")
    print()
    print(f"[기준] totalCount={total} · numOfRows={body.get('numOfRows')} · 수신 {len(items)}건")
    print("       조건: 서울특별시 / 매각(0001) / 재산유형 전체 / pvctTrgtYn=N")

    if items:
        print()
        print("[샘플 1건 필드]")
        for k, v in list(items[0].items()):
            sv = str(v)
            print(f"  {k:26} = {sv[:70]}")

    # ── D15: numOfRows 최대값 ────────────────────────────────
    print()
    print("=" * 70)
    print("[D15] numOfRows 최대 허용값 탐색")
    for n in (100, 500, 1000):
        p = dict(base_params, numOfRows=n)
        st, pl = call(winner, p)
        hdr = header_of(pl)
        received = len(items_of(pl))
        print(f"  numOfRows={n:<5} resultCode={hdr.get('resultCode')} · 수신 {received}건")

    # ── D14 / D13 / D12: 대량 표본으로 분포 확인 ──────────────
    print()
    print("=" * 70)
    print("[D12/D13/D14] 표본 수집 (pvctTrgtYn Y·N 양쪽, 최대 300건)")
    sample: list[dict[str, Any]] = []
    for pvct in ("N", "Y"):
        for page in (1, 2, 3):
            p = dict(base_params, numOfRows=100, pageNo=page, pvctTrgtYn=pvct)
            st, pl = call(winner, p)
            got = items_of(pl)
            sample.extend(got)
            if len(got) < 100:
                break
    print(f"  표본 {len(sample)}건")

    if sample:
        print()
        print("[D14] pbctStatCd 분포 — 목록이 반환하는 상태 범위")
        for code, cnt in Counter(str(i.get("pbctStatCd")) for i in sample).most_common():
            nm = next((str(i.get("pbctStatNm")) for i in sample
                       if str(i.get("pbctStatCd")) == code), "")
            print(f"  {code:6} {nm:12} {cnt:5}건")

        print()
        print("[D13] apslPrcCtrsLowstBidRto 단위 (퍼센트=50 / 비율=0.5)")
        vals = [i.get("apslPrcCtrsLowstBidRto") for i in sample]
        def _num(v: Any) -> bool:
            return v not in (None, "") and str(v).replace(".", "", 1).replace("-", "", 1).isdigit()

        nums = [float(str(v)) for v in vals if _num(v)]
        if nums:
            print(f"  표본 {len(nums)}건 · min={min(nums)} · max={max(nums)} · 예시={nums[:8]}")
            print(f"  → 판정: {'퍼센트(0~100)' if max(nums) > 1.5 else '비율(0~1)'}")
        else:
            print(f"  숫자 없음. 원본 예시: {vals[:5]}")

        print()
        print("[D12] lowstBidPrcIndctCont 비수치 표기 탐색")
        nonnum: Counter[str] = Counter()
        for i in sample:
            v = str(i.get("lowstBidPrcIndctCont", ""))
            if v and not v.replace(",", "").replace(" ", "").isdigit():
                nonnum[v[:40]] += 1
        if nonnum:
            for v, c in nonnum.most_common(10):
                print(f"  {c:5}건  {v!r}")
        else:
            print("  비수치 표기 없음 (이번 표본 한정)")

        print()
        print("[참고] 감정가(apslEvlAmt) 결측률")
        miss = sum(1 for i in sample if not i.get("apslEvlAmt"))
        print(f"  결측 {miss}/{len(sample)}건 ({miss * 100 // max(len(sample), 1)}%)")

        print()
        print("[참고] PNU(ltnoPnu) 제공률")
        pnu = sum(1 for i in sample if i.get("ltnoPnu"))
        print(f"  제공 {pnu}/{len(sample)}건 ({pnu * 100 // max(len(sample), 1)}%)")
        ex = next((str(i.get("ltnoPnu")) for i in sample if i.get("ltnoPnu")), None)
        if ex:
            print(f"  예시 {ex} (길이 {len(ex)}, 앞10자리 법정동코드 {ex[:10]})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
