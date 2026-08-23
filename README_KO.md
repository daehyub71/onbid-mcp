# onbid-mcp

[English README](README.md)

[온비드(한국자산관리공사)](https://www.onbid.co.kr) OpenAPI 의 공매 물건 데이터를 Claude Code
같은 LLM 클라이언트에 노출하는 MCP 서버.

> **상태: 진행 중.** M0~M3 완료 — 수집·정규화·Supabase 적재가 실데이터로 끝까지 동작한다
> (2026-08-23 서울 6,902건 적재). 지오코딩(M4)·조회 계층(M5)·MCP 툴(M6)은 아직이다.

## 왜 만드나

온비드는 OpenAPI 로 공매 물건을 공개하지만 원본 그대로는 다루기 번거롭다. 금액이 자유 문자열로
오고, 목록 응답에는 주소가 없으며, 끝난 물건은 응답에서 그냥 사라지고, 일일 호출 한도가 작아
단순 크롤러로는 한 바퀴를 못 돈다. 이 프로젝트가 그 특이사항을 배치 계층에서 한 번에 흡수해,
LLM 이 "강남구에서 세 번 이상 유찰된 물건은?" 같은 평범한 질문을 던질 수 있게 한다.

## 구현 상태

| 계층 | 패키지 | 상태 |
|---|---|---|
| 수집 | `core/onbid`, `core/codes` | ✅ 유량 제어·재시도 분류·페이지 순회·코드 트리 |
| 정규화 | `core/normalizer` | ✅ 주소·금액·일시·PNU·상태 파생 |
| 적재 | `core/store`, `migrations/` | ✅ 복합키 upsert·변경 이력·tombstone·배치 메타 |
| 오케스트레이션 | `core/pipeline` | ✅ 물건·회차·코드표 배치, 커밋 경계 명시 |
| 지오코딩 | `core/geocoder` | ⬜ M4 |
| 조회·통계 | `core/stats`, `api/` | ⬜ M5 |
| MCP 툴 | `onbid_mcp/` | ⬜ M6 |

## 알아둘 만한 설계 판단

가이드를 읽어서가 아니라 실측으로 정해진 것들이다.

**끝난 물건은 삭제하지 않고 표시한다.** 온비드는 진행 중인 물건만 반환하므로, 사라진 물건과
애초에 없던 물건을 구분할 수 없다. 그래서 `종료추정` 으로 남기되, 판정 전에 세 조건이 모두
성립해야 한다 — 전량 모드·수집 범위 일치·수집 완주. 범위를 틀리면 멀쩡한 6,594건이 뒤집힌다는
것을 실측으로 확인했다.

**기본키는 복합키다.** `cltrMngNo` 하나로는 유일하지 않다 — 한 물건관리번호에 `pbctCdtnNo` 가
최대 10개 달린다. 상세·입찰정보 조회도 두 값을 함께 요구한다.

**비율은 읽지 않고 계산한다.** 온비드가 비율 필드를 주지만 실측 채움률이 0% 다. `min_bid_rate`
는 금액에서 직접 계산하며, 100% 를 넘는 것이 정상이다(실측 최대 150.2%, 9.8%). 클램프하지 않는다.

**회차 이력은 마지막 시도 시각으로 롤링한다.** 입찰정보 API 는 하루 1,000건인데 대상이
1,088건이다. 재개 토큰 대신 — 진행 위치가 스칼라가 아니라 *집합*이다 — 오래 안 본 순서로 뽑아
상태 없이 한 바퀴를 돈다.

**변경 diff 는 적재보다 먼저 한다.** 뒤에서 비교하면 차이가 조용히 0이 되므로, 두 동작을 하나의
호출로 묶어 순서를 틀릴 수 없게 했다.

## 요구 사항

- Python 3.11+
- 온비드 API 가 승인된 공공데이터포털 서비스키
- Supabase(PostgreSQL) 프로젝트
- 카카오 로컬 REST API 키 (M4 부터 필요)

## 설치

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # 키를 채운다
python scripts/migrate.py     # 테이블 생성 (재실행해도 안전)
```

## 실행

```bash
python scripts/run_batch.py                    # 코드표 → 물건 → 회차
python scripts/run_batch.py --sgg 강남구 --rounds-budget 0
python scripts/run_batch.py --mode delta --since 20260820
python scripts/run_batch.py --dry-run
```

## 개발

```bash
ruff check .
mypy core/ onbid_mcp/ api/ tests/ scripts/
pytest -q            # 450건, 네트워크 없음
pytest -m db -q      # 164건, Supabase 대상 (전부 롤백 트랜잭션 안)
pytest -m live -q    # 실호출, 기본 제외
```

db 테스트는 실 스키마를 대상으로 하되 항상 롤백되는 트랜잭션 안에서 돌아 흔적을 남기지 않는다.
`live` 마커가 없는 테스트는 네트워크를 타지 않는다.

## 문서

명세주도(SDD)로 진행하며, 문서가 기준이다.

- [docs/SPEC.md](docs/SPEC.md) — 요구사항·데이터 모델·MCP 툴 계약·미해소 항목
- [docs/PLAN.md](docs/PLAN.md) — 아키텍처·마일스톤·테스트 전략·리스크
- [docs/TASKS.md](docs/TASKS.md) — 진도율 대시보드·트러블슈팅 기록
- [docs/API_FINDINGS.md](docs/API_FINDINGS.md) — 실측한 API 동작. **활용가이드보다 우선한다**
  (가이드가 틀린 곳이 여러 군데 있었다)

## 보안

키는 `.env` 에만 둔다. 온비드는 인증키를 쿼리 파라미터로 요구하고 httpx 는 INFO 레벨에서 요청
URL 을 통째로 기록하므로, `core/onbid/client.py` 가 import 시점에 `httpx` 로거를 WARNING 으로
낮춘다 — 그러지 않으면 로깅을 켜는 순간 키가 샌다. `onbid_*` 테이블은 RLS 활성 + 정책 없음 +
grant revoke 로 `service_role` 전용이며, anon 키로는 전 테이블이 HTTP 401 임을 실측 확인했다.

## 라이선스

미정. 온비드 활용가이드 문서는 저장소에 포함하지 않는다 — 여기서 쓰는 응답 구조는
`docs/API_FINDINGS.md` 에 실측으로 기록돼 있다.
