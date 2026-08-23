"""카카오·Supabase 키 스모크 — 연결·권한만 확인한다. 데이터 변경은 하지 않는다."""

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "scripts")
from smoke_onbid import load_env  # noqa: E402


def ctx() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


env = load_env()

print("=" * 70)
print("[1] 카카오 로컬 API — 주소 → 좌표")
kakao = env.get("KAKAO_REST_API_KEY", "")
tests = [
    ("도로명 있음", "서울특별시 서대문구 창천동 72-22"),
    ("꼬리표 포함", "서울특별시 강남구 역삼동 123-4 외 2필지"),
    ("읍면동만", "서울특별시 서대문구 창천동"),
]
for label, addr in tests:
    qs = urllib.parse.urlencode({"query": addr})
    url = f"https://dapi.kakao.com/v2/local/search/address.json?{qs}"
    st, body = get(url, {"Authorization": f"KakaoAK {kakao}"})
    if st == 200:
        d = json.loads(body)
        docs = d.get("documents", [])
        if docs:
            doc = docs[0]
            print(f"  ✅ {label:12} HTTP 200 · {len(docs)}건 · x={doc.get('x')} y={doc.get('y')}"
                  f" · type={doc.get('address_type')}")
        else:
            print(f"  ⚠️  {label:12} HTTP 200 · 결과 0건")
    else:
        print(f"  ❌ {label:12} HTTP {st} · {body[:160]}")

print()
print("[1-b] 좌표 → 법정동코드 (coord2regioncode)")
url = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json?" + urllib.parse.urlencode(
    {"x": "126.936", "y": "37.556"})
st, body = get(url, {"Authorization": f"KakaoAK {kakao}"})
if st == 200:
    for d in json.loads(body).get("documents", []):
        print(f"  ✅ {d.get('region_type')} · {d.get('address_name')} · code={d.get('code')}")
else:
    print(f"  ❌ HTTP {st} · {body[:160]}")

print()
print("=" * 70)
print("[2] Supabase REST — 키 유효성 및 권한 범위")
sb_url = env.get("SUPABASE_URL", "").rstrip("/")
svc = env.get("SUPABASE_SERVICE_KEY", "")
anon = env.get("SUPABASE_ANON_KEY", "")
print(f"  SUPABASE_URL 호스트: {urllib.parse.urlparse(sb_url).hostname}")
def key_kind(k: str) -> str:
    """키 값을 노출하지 않고 형식만 판별한다."""
    if k.startswith("sb_"):
        return "신형(sb_*)"
    return "JWT(legacy)" if k.count(".") == 2 else "알 수 없음"


print(f"  SERVICE_KEY 형식: {key_kind(svc)}")
print(f"  ANON_KEY 형식:    {key_kind(anon)}")

for label, key in (("service", svc), ("anon", anon)):
    st, body = get(f"{sb_url}/rest/v1/", {"apikey": key, "Authorization": f"Bearer {key}"})
    print(f"  [{label:7}] 루트 접근 HTTP {st}" + ("" if st == 200 else f" · {body[:120]}"))

print()
print("[3] 기존 테이블 접근 범위 — anon 키가 다른 프로젝트 테이블을 읽는가 (SPEC §6.6 근거)")
for tbl in ("ksc_stocks", "ksc_bars", "ksa_signals"):
    line = f"  {tbl:14}"
    for label, key in (("service", svc), ("anon", anon)):
        st, body = get(f"{sb_url}/rest/v1/{tbl}?select=*&limit=1",
                       {"apikey": key, "Authorization": f"Bearer {key}"})
        if st == 200:
            n = len(json.loads(body)) if body.strip().startswith("[") else "?"
            mark = f"{label}=읽힘({n}행)"
        elif st in (401, 403):
            mark = f"{label}=차단({st})"
        elif st == 404:
            mark = f"{label}=없음"
        else:
            mark = f"{label}=HTTP{st}"
        line += f"  {mark:22}"
    print(line)

print()
print("[4] onbid_* 테이블 존재 여부 (아직 미생성이어야 정상)")
st, body = get(f"{sb_url}/rest/v1/onbid_cltr?select=*&limit=1",
               {"apikey": svc, "Authorization": f"Bearer {svc}"})
print(f"  onbid_cltr · service HTTP {st} · {body[:140]}")
