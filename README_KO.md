# onbid-mcp

[English README](README.md)

[온비드(한국자산관리공사)](https://www.onbid.co.kr)의 공매 물건 데이터를 LLM 이 조회할 수 있게
해 주는 MCP 서버.

Claude Desktop 에서 **"강남구에서 3회 이상 유찰된 물건 보여줘"** 라고 물으면, 직접 수집한
데이터에서 답이 나옵니다. 구독료도, 스크래핑도 없습니다.

> **상태.** 파이프라인과 MCP 툴 4종이 실데이터로 끝까지 동작합니다(서울 6,902건, 좌표 99.9%).
> 남은 것은 최종 검수(M7)와 예약 배치 1주 관찰입니다. 정확한 진행 상황은
> [docs/TASKS.md](docs/TASKS.md) 를 보세요.

---

## 무엇을 쓸 수 있나

stdio 로 툴 4종과 리소스 4종을 제공합니다.

| 툴 | 하는 일 |
|---|---|
| `search_auction_items` | 지역·용도·재산유형·수의계약 여부·가격·최저가율·유찰횟수·마감일·상태로 검색. 한글 명칭을 그대로 씁니다(`"강남구"`, `"아파트"`). 커서 페이지네이션. |
| `get_auction_detail` | 물건관리번호로 단건 조회. 형제 공매조건번호와 온비드 원문 링크를 함께 줍니다. |
| `get_auction_stats` | 6개 축의 분포와 낙찰가율. **집계값만** 주고 개별 물건은 주지 않습니다. |
| `get_address_geocode` | 주소 → 좌표. 서버 측 일일 호출 상한이 걸립니다. |

| 리소스 | 담고 있는 것 |
|---|---|
| `onbid://codes/regions` | **물건이 실제로 있는** 시군구·읍면동 |
| `onbid://codes/usages` | 용도 3단 계층 트리 |
| `onbid://codes/property-types` | 재산유형 코드표 |
| `onbid://dataset/status` | 배치 기준 시각·건수·좌표율 — 데이터가 얼마나 신선한지 |

모든 응답에 `meta`(출처·`synced_at`·`is_realtime: false`·건수·잘림 여부·고지)와
`query_echo`(기본값·상한을 적용한 **실제 조건**)가 붙습니다.

---

## 시작하기 전에

이 서버는 **자기 DB** 를 조회합니다. 호스팅 서비스가 아니라 직접 수집하는 구조라 키가 필요합니다.

| 필요한 것 | 발급처 | 참고 |
|---|---|---|
| 온비드 서비스키 | [공공데이터포털](https://www.data.go.kr) | 온비드 OpenAPI 5종을 활용신청합니다. 개발계정은 보통 즉시 승인됩니다. |
| Supabase 프로젝트 | [supabase.com](https://supabase.com) | 무료 요금제로 충분합니다 — 서울 데이터가 7천 행 정도입니다. 일반 PostgreSQL 도 됩니다. |
| 카카오 REST API 키 | [Kakao Developers](https://developers.kakao.com) | 지오코딩용. 지도 SDK 용 JavaScript 키가 **아니라** REST API 키여야 합니다. |

그 외에 Python 3.11+ 와 Claude Desktop(또는 stdio 를 지원하는 MCP 클라이언트)이 필요합니다.

수집 범위는 기본이 **서울·매각 물건**입니다. 넓히는 것은 필터 한 줄이지만, 아래의 지오코딩·쿼터
수치는 서울 기준입니다.

---

## 설치

```bash
git clone https://github.com/daehyub71/onbid-mcp.git
cd onbid-mcp
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # 위 세 가지 키를 채웁니다
python scripts/migrate.py     # 테이블 생성 (재실행해도 안전)
```

그다음 첫 데이터를 수집합니다. 2분쯤 걸리고 일일 API 쿼터 안에 충분히 들어옵니다.

```bash
python scripts/run_batch.py
```

이런 출력이 나오면 정상입니다.

```
── 물건 ──
  ok · 수집 6902 · 적재 6902 · 이력 0 · tombstone 0
── 좌표 ──
  ok · 대상 500 · 좌표 500 (근사 0) · 실패 0 · 호출 133
```

좌표는 `--geocode-budget 1000` 으로 몇 번 더 돌리면 채워집니다. 캐시가 대부분을 흡수하므로
6,902건 전체가 카카오 호출 800회 정도면 끝납니다. 진행 상황은 `dataset/status` 의 좌표율로
확인하세요.

---

## Claude Desktop 연결

`claude_desktop_config.json` 에 서버를 추가합니다.

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "onbid": {
      "command": "/절대경로/onbid-mcp/venv/bin/python",
      "args": ["-m", "onbid_mcp.server"],
      "cwd": "/절대경로/onbid-mcp",
      "env": {
        "PYTHONPATH": "/절대경로/onbid-mcp",
        "SUPABASE_DATABASE_URL": "postgresql://...",
        "ONBID_SERVICE_KEY": "...",
        "KAKAO_REST_API_KEY": "..."
      }
    }
  }
}
```

여기서 걸리기 쉬운 것이 넷입니다.

- **`PYTHONPATH` 가 필요합니다 — `cwd` 만으로는 안 됩니다.** Claude Desktop 은 설정의 `cwd` 를
  적용하지 않아서, `python -m onbid_mcp.server` 가 패키지를 못 찾고 `ModuleNotFoundError` 로
  즉시 죽습니다. 앱은 이것을 "Server disconnected" 로 표시하기 때문에 경로 문제가 아니라
  연결 문제처럼 보입니다.
- **venv 인터프리터를 절대경로로** 씁니다. Claude Desktop 은 셸 `PATH` 를 물려받지 않아,
  그냥 `python` 이라고 쓰면 의존성이 없는 시스템 파이썬이 잡힙니다.
- **키는 `env` 에 넣습니다.** 앱은 프로젝트의 `.env` 파일을 읽지 않습니다.
- **로그가 stdout 으로 나가면 안 됩니다.** stdout 이 JSON-RPC 채널이라 한 줄만 섞여도 프레임이
  깨지고 연결이 조용히 끊깁니다. 이 서버가 stderr 로만 로그를 내는 이유입니다.

Claude Desktop 을 재시작한 뒤 이렇게 물어보세요.

> 강남구에서 3회 이상 유찰된 물건 중 최저가율 60% 이하인 것 보여줘

연결이 안 되면 `~/Library/Logs/Claude/mcp-server-onbid.log` 를 보세요. 화면에는 "Server
disconnected" 만 뜨지만 로그에는 실제 파이썬 오류가 그대로 남습니다.

Claude Desktop 없이 서버만 확인하려면:

```bash
python scripts/mcp_smoke.py        # stdio 로 툴 목록과 실제 호출을 확인
```

---

## 데이터 갱신 유지하기

깃허브 Actions 워크플로 2개가 포함돼 있습니다. 저장소 Secrets 에 `ONBID_SERVICE_KEY`,
`SUPABASE_DATABASE_URL`, `KAKAO_REST_API_KEY` 를 등록하면 알아서 돕니다.

| 워크플로 | 시각 (KST) | 하는 일 |
|---|---|---|
| `onbid-daily` | 월~토 04:00 | 변경분 수집 + 회차 이력 + 좌표 |
| `onbid-weekly` | 일 04:00 | 코드표 + **전량 수집** — 끝난 물건을 표시할 수 있는 유일한 회차 |

깃허브 cron 은 UTC 만 지원해서 KST 04:00 이 전날 19:00 UTC 이고, 그래서 요일이 하나 밀립니다.
최근 일주일 점검은 이렇게 합니다.

```bash
python scripts/batch_health.py
```

cron 이 걸러지면 아무 흔적도 남지 않습니다 — 깃허브는 **실행됐다가 실패한** 경우만 메일을
보냅니다. 그래서 이 도구가 날짜를 셉니다.

---

## 알아둘 만한 설계 판단

가이드를 읽어서가 아니라 실측으로 정해진 것들입니다.

**끝난 물건은 삭제하지 않고 표시합니다.** 온비드는 진행 중인 물건만 반환하므로, 사라진 물건과
애초에 없던 물건을 구분할 수 없습니다. 그래서 `종료추정` 으로 남기되 세 조건이 모두 성립할
때만 판정합니다 — 전량 모드·수집 범위 일치·수집 완주. 범위를 틀리면 멀쩡한 6,594건이 뒤집힌다는
것을 실측으로 확인했습니다.

**기본키는 복합키입니다.** `cltrMngNo` 하나로는 유일하지 않습니다 — 한 물건관리번호에
`pbctCdtnNo` 가 최대 10개 붙고, 입찰정보 API 는 조건번호와 무관하게 같은 회차 이력을 돌려줍니다.
통계는 `(물건관리번호, 개찰일시, 회차)` 로 중복을 제거합니다. 행을 그대로 세면 실제 13건의
낙찰 사건이 62건으로 보입니다.

**비율은 읽지 않고 계산합니다.** 온비드가 비율 필드를 주지만 실측 채움률이 0% 입니다.
`min_bid_rate` 는 금액에서 직접 계산하며, 100% 를 넘는 것이 정상입니다(실측 최대 150.2%, 9.8%).
클램프하지 않습니다.

**결과 0건은 빈 배열이 아니라 오류입니다.** `no_result` 는 LLM 에게 조건을 완화하라고 알립니다.
빈 배열이면 "그런 물건이 없다" 고 단정해 버립니다.

**낙찰가율 통계는 편향돼 있고, 그 사실이 숫자보다 중요합니다.** 볼 수 있는 낙찰은 "낙찰됐다가
계약이 무산되어 다시 나온" 건뿐입니다 — 정상적으로 끝난 거래는 목록 API 에 나오지 않습니다.
그래서 모든 응답에 이 주의사항이 함께 갑니다.

---

## 개발

```bash
ruff check .
mypy core/ onbid_mcp/ api/ tests/ scripts/
pytest -q            # 595건, 네트워크 없음
pytest -m db -q      # 361건, 내 DB 대상 (전부 롤백 트랜잭션 안)
pytest -m live -q    # 실호출, 기본 제외
```

db 테스트는 실 스키마를 대상으로 하되 항상 롤백되는 트랜잭션 안에서 돌아 흔적을 남기지
않습니다 — 실행 전후 테이블 행 수 비교로 확인했습니다. 순수 테스트는 접속 문자열을 일부러
망가뜨려도 통과합니다.

로컬 전용 HTTP API(`api/main.py`)도 있습니다. 루프백에만 바인딩되며 curl 로 데이터를 들여다볼
때 편합니다. MCP 사용에는 필요하지 않습니다.

---

## 문서

명세주도(SDD)로 진행하며, 문서가 기준입니다.

- [docs/SPEC.md](docs/SPEC.md) — 요구사항·데이터 모델·MCP 툴 계약·미해소 항목
- [docs/PLAN.md](docs/PLAN.md) — 아키텍처·마일스톤·테스트 전략·리스크
- [docs/TASKS.md](docs/TASKS.md) — 진도율 대시보드·트러블슈팅 기록
- [docs/API_FINDINGS.md](docs/API_FINDINGS.md) — 실측한 API 동작. **활용가이드보다 우선합니다**
  (가이드가 틀린 곳이 여러 군데 있었습니다)

---

## 보안

키는 `.env`(로컬) 또는 깃허브 Secrets · MCP 설정의 `env`(배포)에만 두고 코드에 넣지 않습니다.
온비드는 인증키를 쿼리 파라미터로 요구하고 httpx 는 INFO 레벨에서 요청 URL 을 통째로 기록하므로,
클라이언트가 import 시점에 `httpx` 로거를 WARNING 으로 낮춥니다 — 그러지 않으면 로깅을 켜는
순간 키가 샙니다. 설정 객체도 같은 이유로 `repr` 에서 값을 가립니다.

`onbid_*` 테이블은 RLS 활성 + 정책 없음 + grant revoke 로 `service_role` 전용이며, anon 키로는
전 테이블이 HTTP 401 임을 실측 확인했습니다. HTTP API 는 루프백 밖으로는 바인딩을 거부합니다.

---

## 한계와 하지 않는 것

- **조회만 합니다.** 랭킹·점수화·추천을 하지 않습니다 — 툴은 공공데이터를 그대로 돌려주고
  판단은 사용자 몫입니다. 공인중개사법이 게시형 표시·광고를 제한하기 때문에 의도적으로 그렇게
  설계했습니다.
- 중개·감정·법률·투자 자문을 하지 않습니다.
- 기본 범위는 서울·매각·진행 중 물건입니다.
- 낙찰가율 통계는 편향된 표본에서 나옵니다(위 설명 참조).

## 라이선스

미정. 온비드 활용가이드 문서는 저장소에 포함하지 않습니다 — 여기서 쓰는 응답 구조는
[docs/API_FINDINGS.md](docs/API_FINDINGS.md) 에 실측으로 기록돼 있습니다.
