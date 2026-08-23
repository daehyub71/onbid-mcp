# 온비드 공매물건 MCP — 개발 계획서 (PLAN)

**프로젝트명**: `onbid-mcp`
**작성일**: 2026-08-18
**버전**: 1.0
**기준 문서**: `docs/SPEC.md` v1.0 (요구사항 ID F/N/AC/D는 SPEC을 따른다)
**실측 근거**: `docs/API_FINDINGS.md` (2026-08-19 실호출)
**주요 변경**: 2026-08-19 적재 대상을 SQLite → **Supabase(PostgreSQL)** 로 변경 (SPEC 부록 A #38~43)
**최종 갱신**: 2026-08-22 — M1 구현 반영, 실측으로 뒤집힌 리스크 항목 정정
**범위**: Phase 1 — Core 파이프라인 + MCP 서버. 지도 Web은 범위 밖(SPEC §2.3)

---

## 1. 이 문서의 목적

SPEC이 **무엇을 만드는가**를 정의했다면, PLAN은 **어떤 순서로, 어떤 구조로, 무엇을 근거로 완료 판정하는가**를 정한다.
구현 중 판단이 갈리면 SPEC이 상위 기준이며, PLAN과 충돌하면 SPEC을 따르고 PLAN을 갱신한다.

---

## 2. 개발 방법론

**SDD** — `SPEC.md` → `PLAN.md` → `TASKS.md` → 실행. UI가 없는 프로젝트이므로 `DESIGN.md`는 작성하지 않는다.
Phase 2에서 Web을 착수할 때 별도 저장소에서 DESIGN 단계를 거친다.

**TDD** — 순수 함수(파서·매핑·계산·파생)는 **테스트를 먼저 작성**한다. 본 프로젝트는 로직의 대부분이
"외부 응답 → 정규화 → 저장 → 조회"라 순수 함수 비중이 높고, TDD 효율이 좋은 구조다.

**계약 우선 (SPEC §1.5②)** — MCP 툴 계약(SPEC §8)이 이미 확정되어 있다. 하위 모듈은 그 계약을 만족하도록
역산해 설계하며, 계약을 바꿔야 할 근거가 나오면 SPEC을 먼저 고친다.

---

## 3. 아키텍처 설계

### 3.1 모듈 구성

```
onbid-mcp/
├─ core/                        # 독립 패키지 — onbid_mcp/, api/를 절대 import하지 않는다 (N8.2)
│  ├─ config.py                 # .env 로딩, 설정 객체
│  ├─ onbid/
│  │   ├─ endpoints.py          # 서비스별 URL·오퍼레이션 상수 (SPEC §6.4)
│  │   ├─ client.py             # HTTP, serviceKey, 재시도, TPS 리미터, resultCode 예외화
│  │   ├─ parser.py             # items.item 모양 흡수, 페이지 정보, 원시 형변환
│  │   ├─ collector.py          # 페이지·pvctTrgtYn 순회, 중복 제거 (F1.9)
│  │   ├─ bidinfo.py            # 회차별 유찰 이력, 대상 선별·예산 롤링 (F1.7·F1.11)
│  │   └─ links.py              # 온비드 원문 상세 URL 조립 (F1.15)
│  ├─ codes/
│  │   ├─ constants.py          # 정적 코드표 — 재산유형·처분방식·입찰상태 (SPEC §6.5)
│  │   ├─ usage.py              # 용도 3단 트리 재귀 순회 (리프 = 03 NODATA)
│  │   ├─ address.py            # 물건이 존재하는 시도·시군구·읍면동 조합
│  │   ├─ index.py              # 용도 트리 조회 — 확장·후보·경로 (F6.12)
│  │   └─ resolve.py            # 툴 파라미터 해석 — 명칭↔코드, 후보 반환 (F6.6·F6.7)
│  ├─ normalizer/
│  │   ├─ address.py            # 지오코딩 주소 선택 — PNU → 물건명 → 읍면동 (F2.1)
│  │   ├─ trailers.py           # 꼬리표 제거 — 깨뜨리는 것만 (F2.2)
│  │   ├─ amounts.py            # 금액 — "비공개" 원문 보존 (F4.7)
│  │   ├─ datetimes.py          # 일시 — KST 부여, 2999 sentinel (§7.1)
│  │   ├─ pnu.py                # PNU 19자리 분해, 법정동코드 추출
│  │   └─ status.py             # 상태 파생 — 코드·이름 두 경로
│  ├─ store/
│  │   ├─ connection.py         # async psycopg 재사용 (N1.3·C10)
│  │   ├─ mapping.py            # 응답 → onbid_cltr 행
│  │   ├─ cltr.py               # 복합키 배치 upsert (F4.1·F4.10)
│  │   ├─ history.py            # 변경 diff — 적재보다 먼저 (F4.4·F4.14)
│  │   ├─ bid_round.py          # 회차 이력 — 개찰일시까지 키 (F1.7·§7)
│  │   ├─ codes.py              # 용도·주소 코드표 upsert (F6.12)
│  │   ├─ batch_run.py          # 배치 메타 + 재개 지점 (F4.6·F4.15·N2.2)
│  │   ├─ geocode.py           # 좌표 적재·대상 선별 (F3.6)
│  │   ├─ query.py             # 조회 쿼리 빌더 — 순수 함수 (F5.1·F6.7)
│  │   └─ tombstone.py          # 사라진 물건 표시 — 범위 필수 (F4.2·F4.13)
│  ├─ geocoder/
│  │   ├─ cache.py              # 주소 캐시 — 실패도 캐시한다 (F3.2)
│  │   ├─ kakao.py              # 카카오 로컬 — 429 즉시 중단 (F3.3~F3.5)
│  │   └─ resolver.py           # 폴백 사다리 — 단계별 status·level (F3.1·F3.6)
│  ├─ stats/
│  │   ├─ distribution.py      # 분포 6축 — 합계 보존·혼재 경고 (§8.3)
│  │   └─ win_rate.py          # 낙찰가율 2지표 — caveat 강제 (§8.3·D18)
│  └─ pipeline/                 # 배치 오케스트레이션 — **커밋은 여기서만** (F4.16)
│      ├─ batch.py             # 물건 배치: 수집 → 이력+적재 → tombstone → 메타 (전량/증분)
│      ├─ rounds.py            # 회차 이력 배치: 시도 시각순 롤링 (F1.11·F1.16)
│      ├─ codes.py             # 코드표 갱신 배치 — 소스별 실패 격리 (F6.12·F7.2)
│      └─ geocode.py           # 좌표 패스 — lat is null 대상, 호출 예산 (F3.5)
├─ onbid_mcp/                   # core를 import만 함 (`mcp`로 두면 SDK를 가린다)
│  ├─ server.py                 # stdio 엔트리
│  ├─ common.py                 # meta·query_echo·오류 래퍼
│  ├─ tools/                    # 툴 4종
│  └─ resources/                # Resource 4종
├─ api/                         # FastAPI (Phase 2 Web 대비, 얇게)
├─ docs/
│  ├─ SPEC.md  PLAN.md  TASKS.md
│  ├─ API_FINDINGS.md           # 실호출 실측 기록 (가이드와의 차이)
│  └─ api-guides/               # 온비드 활용가이드 원본 (로컬 전용 — 공개 리포 제외)
├─ migrations/                  # NNN_*.sql — 재실행 가능한 DDL (콘솔 수동 변경 금지)
├─ scripts/                     # 실호출 프로브 (표준 라이브러리만, 패키지 설치 불요)
├─ tests/
│  ├─ fixtures/onbid/           # 실응답 캡처 (서비스키 제거)
│  └─ ...
├─ .env                         # gitignore
├─ requirements.txt  requirements-dev.txt
└─ pyproject.toml               # ruff / mypy / pytest 설정
```

### 3.2 의존 방향

```
        config
          │
     ┌────┴─────┬──────────┐
     ▼          ▼          ▼
  onbid/     codes/    geocoder/
     │          │          │
     └────┬─────┴────┬─────┘
          ▼          ▼
     normalizer    store  ◀── stats
          │          │
          └────┬─────┘
               ▼
           pipeline
               │
        ┌──────┴──────┐
        ▼             ▼
   onbid_mcp/        api/
```

**규칙**
- 화살표 역방향 import 금지. `core/`는 `onbid_mcp/`·`api/`를 모른다.
- `onbid_mcp/`와 `api/`는 서로를 모른다. **공통 로직은 반드시 `core/store`·`core/stats`에 둔다.**
- MCP는 HTTP를 거치지 않고 `core`를 직접 호출한다 (SPEC §9.2).

### 3.3 배치 운영 설계

온비드 물건목록이 `mdfcnYmd`(최종수정일) 필터를 지원하므로 두 모드로 나눈다.

| 모드 | 주기 | 실행 시각 (KST) | 조회 범위 | 목적 |
|---|---|---|---|---|
| **전량(full)** | 주 1회 | **일 04:00** (`0 19 * * 6` UTC) | 조건 전체 | tombstone 판정의 기준. 사라진 물건을 찾으려면 전량 스냅샷이 필요 |
| **증분(delta)** | 매일 | **월~토 04:00** (`0 19 * * 0-5` UTC) | `mdfcnYmd` = 최근 N일 | 변경분만. 호출량·소요를 크게 줄인다 |
| **회차(rounds)** | 매일 | 위 배치에 이어서 | 시도가 오래된 순 | 유찰 이력·낙찰가. 예산 1,000건이라 이틀에 한 바퀴 (F1.16) |
| **코드표(codes)** | 주 1회 | 일 04:00, 전량 앞 | 용도 트리·주소 | 거의 변하지 않아 주간으로 충분 |

깃허브 Actions 는 UTC 만 지원하므로 KST 04:00 은 전날 19:00 UTC 다. 요일이 하나 밀리는 것에
주의한다 — **UTC 토요일** 19:00 이 KST 일요일 04:00 이다.

**중요**: tombstone(SPEC F4.2)은 **전량 모드에서만 판정한다.** 증분 모드의 "응답에 없음"은 "변경이 없음"이지
"사라짐"이 아니다. 이걸 구분하지 않으면 멀쩡한 물건이 매일 `종료추정`으로 뒤집힌다.

수집 루프 구조:

```
for pvct in (Y, N):                     # F1.9 — 필수 파라미터가 단일값
  for page in 1..N:                     # totalCount 기반 종료
    GET getRlstCltrList(
      prptDivCd = 재산유형 10종 쉼표 나열,
      dspsMthodCd = 0001,               # 매각만 (SPEC §2.1)
      lctnSdnm = "서울특별시",           # F1.10 — 코드가 아니라 문자열
      pvctTrgtYn = pvct,
      [mdfcnYmdStart/End]               # 증분 모드에서만
    )
    → TPS 리미터 대기 → 파싱 → upsert
```

지오코딩은 수집과 **분리된 후속 패스**로 돈다(§4 M4 참조).

---

## 4. 마일스톤

Phase 1은 M0~M7로 구성한다. 각 마일스톤은 **테스트 통과 없이 완료를 선언하지 않는다.**

| # | 마일스톤 | 핵심 산출물 | 완료 기준 | 선행 |
|---|---|---|---|---|
| **M0** | 사전 준비 | 키·가이드·저장소 골격 | 실호출 가능한 상태 | — |
| **M1** | 수집 | `core/onbid`, `core/codes` | 서울·매각 물건 목록과 코드 트리를 메모리로 확보 | M0 |
| **M2** | 정규화 | `core/normalizer` | 응답 → 내부 모델 변환, 단위 테스트 통과 | M1 |
| **M3** | 적재 | `migrations/`, `core/store`, `core/pipeline` | Supabase 적재 + 멱등성 + tombstone + **RLS 차단 실측** | M2 |
| **M3.5** | 운영 자동화 | `.github/workflows/` | 매일·주간 배치가 사람 손 없이 돈다 | M3 |
| **M4** | 지오코딩 | `core/geocoder` | 좌표 부여, `ok` 90% 이상 | M3 |
| **M5** | 조회 계층 | `core/store` 쿼리, `core/stats`, `api/` | 필터·커서·집계 동작 | M3 |
| **M6** | MCP 서버 | `onbid_mcp/` 툴 4종 + Resource 4종 | Claude Code에서 호출 성공 | M5 |
| **M7** | 검수·품질 | 실패 리포트, 게이트 통과 | SPEC §12 AC1~AC13 전부 충족 | M4, M6 |

### 4.1 순서에 대한 설계 결정

**① 적재(M3)를 지오코딩(M4)보다 앞에 둔다** — 계획서 v0.3은 `수집 → 정제 → 지오코딩 → 적재` 순이었다.
그러나 지오코딩은 429 중단·재개(N2.2)와 캐시가 필요한 **재개 가능한 후속 패스**다. DB에 행이 먼저 있어야
"어디까지 처리했는가"를 상태로 남길 수 있다. 좌표를 메모리에서 다 채운 뒤 한 번에 적재하는 구조는
중단 시 전부 날아간다.
→ **좌표 없이 먼저 적재하고, 지오코딩은 `lat IS NULL` 행을 대상으로 도는 별도 패스로 만든다.**

**② M5(조회)를 M4(지오코딩)와 병렬로 둔다** — 조회 계층은 좌표에 의존하지 않는다. `lat/lng`가 null이어도
SPEC §8.1은 물건을 반환하도록 규정한다. 두 마일스톤은 M3 이후 독립적으로 진행 가능하다.

**③ M6(MCP)이 M7(검수)보다 앞이다** — 계획서 v0.3이 M5.5를 M6 앞에 둔 이유("MCP로 대화하며 데이터 품질을
검증한다")를 그대로 계승한다. 검수는 MCP로 데이터를 훑으면서 하는 편이 빠르다.

**④ M1 첫 작업은 실호출 스모크 테스트다** — SPEC §14의 남은 미확정 D11~D15가 **실호출 1회로 전부 닫힌다.**
클라이언트를 다 만들고 나서 오퍼레이션명이 틀렸다는 걸 알면 손해다.

### 4.2 마일스톤 상세

#### M0. 사전 준비

- 온비드 API 활용신청 5종 (**완료 2026-08-18**)
- 활용가이드 확보 — 물건목록·물건상세·공고목록·코드/주소 **완료**, 물건상세 입찰정보 **미확보**
- `.env` 구성: 온비드 서비스키(Encoding/Decoding 양쪽), 카카오 REST 키(앱 1288016)
- 저장소·가상환경·검증 도구(ruff/mypy/pytest) 설정
- **VWorld는 이 단계에서 발급하지 않는다.** 지오코딩 폴백 4단계와 Phase 2 지도용이며, M4에서 카카오만으로
  `ok` 90%에 도달하면 Phase 1에는 필요 없다.

#### M1. 수집

- **실호출 스모크 → D11~D15 해소** ✅ 2026-08-19 — 오퍼레이션명에 접미사 `2`, `numOfRows=5000` 동작,
  **온비드 비율 필드 채움률 0%**(자체 계산으로 전환), `"비공개"` 확인, 목록은 진행 계열만 반환
- **페이지·그룹 순회** ✅ 2026-08-22 — 서울 전량 6,910건을 3회 호출로 수집 확인
- `OnbidClient`: `serviceKey` 정규화(`unquote` 후 `params=` 전달), `resultType=json`, 10 TPS 리미터, 지수 백오프 3회
- **`resultCode`는 HTTP 200에 실려 온다** — 상태코드만 보고 성공 판정하지 않는다 (SPEC §8.7)
- `items.item`이 단건일 때 배열이 아닐 수 있는 문제를 파서에서 흡수
- 물건목록 페이지 순회 (`pvctTrgtYn` Y/N 2회)
- 용도 코드 트리: `getOnbidUsgCodeInfo`를 `upCtgrId=10000`부터 재귀 순회. **리프는 `03`, 빈 루트는 `99` 오류**
- 주소 조합: `getOnbidDtlAddrInfo`. **행정구역 코드표가 아니라 물건이 존재하는 주소**이므로 `dtlAddr` 를 버리고 조합만 남긴다
- `raw_payload` 보존 — **응답 행을 손대지 않는다.** 수집 그룹은 `CollectedItem.group` 에 분리 (F1.3)
- 실패 처리 두 갈래 — 페이지 실패는 기록 후 계속, **쿼터·키 오류는 즉시 중단 + 재개 지점** (F1.4·F1.13)
- 수집 요약 로깅 — 건수·페이지·실패·소요시간. **실패가 있으면 `WARNING` 이상으로 올린다** (F1.5)
- **fixtures 캡처** — 이후 모든 테스트가 이 fixture 위에서 돈다. 서비스키는 반드시 제거한다.
- 온비드 원문 링크 조립 ✅ 2026-08-22 — 규칙이 문서에 없어 실호출로 확정. 식별자 4개 필수 (F1.15)

#### M2. 정규화

- 주소 선택: **PNU 조립 → 물건명 파싱 → 읍면동 조합** (F2.1). `cltrRadr`·`zadrNm` 은 물건목록에 없어 쓸 수 없다
- 금액 파싱: `lowstBidPrcIndctCont`가 비수치일 때 null + 원문 보존 (F4.7)
- 일시 파싱: `yyyyMMddHHmm` / `yyyyMMddHHmmss` → KST ISO8601
- `status` 파생: `pbctStatCd` 8종 → 6종 (SPEC §7.1)
- PNU 검증: 19자리 문자열, 선행 0 보존, 앞 10자리 법정동코드 추출
- 명칭↔코드 매핑 + **실패 시 후보 목록 반환** (F6.7)
- 전 항목 TDD

#### M3. 적재 (Supabase)

- `migrations/001_init.sql` — 테이블 7종 + 인덱스 + **RLS 활성화 + grant revoke** (SPEC §6.6·§7)
- `migrations/002_bid_round_attempt.sql` — `bid_round_synced_at` + 대상 선별용 부분 인덱스 (F1.16)
- psycopg 연결 관리 — **커넥션 재사용** + **`prepare_threshold=None`**(트랜잭션 풀러, C10)
- 복합키 `(cltr_mng_no, pbct_cdtn_no)` **배치 upsert** (`insert ... on conflict do update`)
- tombstone: **전량 모드에서만** `status='종료추정'` 판정
- `first_seen_at` / `last_seen_at`, `onbid_cltr_history` diff 적재 — **diff 는 upsert 앞에서**.
  뒤에서 비교하면 차이가 0이 되어 조용히 사라진다. `upsert_with_history` 로 순서를 묶는다 (F4.14)
- `onbid_batch_run` 메타 + `resume_token` — 행은 **시작 시점에** 열고, 완주하면 재개 지점을 지운다 (F4.15)
- 코드표·회차 적재 — 용도/주소는 지우지 않고 upsert, 회차는 `opbd_dt` 까지 키에 넣는다
- **멱등성 테스트(AC2)**: 배치 순서 그대로 2회 실행 후 스냅숏·행 수·이력 건수 비교
- `core/pipeline` — **적재 계층을 실제로 호출하는 유일한 층**. 커밋 경계를 여기서만 정한다 (F4.16)
  · 메타 행은 열자마자 커밋, 데이터는 한 트랜잭션
  · 모드는 `ListingFilter` 하나에서 파생 — 수집 조건과 tombstone 범위가 어긋날 수 없다
  · **수집이 완주하지 못하면 tombstone 을 건너뛴다** (F4.17). 실패 페이지의 물건이 종료 처리되는 것을 막는다
- **첫 실적재** — 서울 전량을 실제로 커밋하고 건수·상태·`batch_run` 을 확인한다.
  여기까지 와야 M4 지오코딩의 전제(`lat is null` 대상 행)가 생긴다
- **RLS 차단 실측**: `SUPABASE_ANON_KEY`로 `onbid_*` SELECT 시도 → 차단 확인 (AC12)

> **왜 M3에서 RLS를 검증하는가**: 테이블이 생기는 순간부터 공유 프로젝트의 anon 키에 노출된다.
> M7 검수까지 미루면 그 사이 기간 내내 열려 있게 된다. **테이블 생성과 차단은 같은 마이그레이션에서 끝낸다.**

#### M3.5. 운영 자동화 (2026-08-23 신설)

- `.github/workflows/onbid-daily.yml` — 매일 04:00 KST, 증분 + 회차
- `.github/workflows/onbid-weekly.yml` — 일요일 04:00 KST, 코드표 + 전량 (tombstone 판정)
- 두 워크플로를 같은 `concurrency` 그룹에 묶는다 — 겹치면 tombstone 기준 시각이 어긋난다
- 시크릿은 깃허브 Secrets. **공개 저장소라 실행 로그도 공개**이므로 첫 실행에서 노출 여부를 확인
- 권한 최소화(`contents: read`), `workflow_dispatch` 로 수동 실행 지원

> **왜 M4보다 먼저인가**: 변경 이력·tombstone·낙찰가율 표본은 시간이 쌓여야 생기는 값이다.
> 지오코딩을 기다리는 동안 흐른 날짜만큼은 되찾을 수 없다. 좌표는 나중에 `lat is null` 대상을
> 일괄로 채우면 되므로 순서를 바꿔도 손해가 없다.

> **재시도하지 않는다**: 실패한 회차를 즉시 다시 돌리지 않는다. 재개 지점(F4.15)과 시도
> 시각(F1.16)이 이월을 이미 처리하므로 다음 정기 실행이 이어받는다. 즉시 재시도는 같은 원인으로
> 또 실패하면서 쿼터만 태운다.

#### M4. 지오코딩

- `geocode_cache` 선행 조회
- 카카오 로컬 클라이언트: 429 **즉시 중단** + 재개 지점 기록
- 폴백 6단계 (SPEC F3.1)
- 일일 호출량 로깅 + 상한
- `lat IS NULL` 대상 배치로 분리 실행
- VWorld 폴백은 카카오만으로 90% 미달일 때만 착수

#### M5. 조회 계층

- 쿼리 빌더: 지역·용도·재산유형·**수의계약여부(`pvct_trgt`)**·가격·최저가율·유찰횟수·마감일·상태
- **opaque cursor 페이지네이션** (offset 금지 — tombstone으로 행 상태가 변한다)
- `sort` 화이트리스트, 기본 마감일 오름차순
- 집계 축 + **재산유형 혼재 caveat** (SPEC §8.3). 최저가율 구간은 **`100%+` 를 별도로 둔다**
- **낙찰가율 집계** — `win_to_appraisal`(감정가 대비)과 `win_to_min_bid`(경쟁 강도)를 분리.
  **모집단 편향 caveat 강제** — 표본이 재공매 물건에 한정되므로 일반 낙찰가율이 아니다
- FastAPI 3종 (Phase 2 대비, 로컬 바인딩만)

#### M6. MCP 서버

- stdio 서버, 툴 4종 + Resource 4종
- 공통 래퍼: `meta`(source/synced_at/is_realtime/count/truncated/notice) + `query_echo`
- 오류 5종 매핑, **`no_result`를 빈 배열로 반환하지 않는다**
- tool description에 판단 금지·배치분·원문 확인 명시 (F6.9)
- `get_address_geocode` 일일 상한 (F6.10)
- Claude Code 연결 검증

#### M7. 검수·품질

- SPEC §12 AC1~AC13 전수 확인 (AC12·AC13 은 M3 에서 1차 확인 완료 — M7 은 재확인)
- `approx`/`failed` 전수 육안 검수 및 원인 분류 리포트
- 시나리오 S1~S7 실제 대화 수행
- 키 노출 점검 (코드·커밋·로그)

---

## 5. 테스트 전략

### 5.1 계층별

| 계층 | 방식 | 비고 |
|---|---|---|
| `onbid/parser`, `normalizer`, `codes` | **TDD 순수 함수 테스트** | fixture 기반. 외부 호출 없음 |
| `onbid/client` | HTTP mock (`respx`/`responses`) | 재시도·`resultCode` 분기·TPS 리미터 |
| `store` 쿼리 빌더 | **순수 테스트** — 생성된 SQL 문자열·파라미터 검증 | 네트워크 불요. 기본 실행에 포함 |
| `store` 통합 | `pytest -m db` — **실 테이블 + 롤백 트랜잭션** (별도 테스트 테이블을 두지 않는다) | 멱등성·tombstone·커서 페이지네이션. 스키마 차이로 인한 위양성이 없다. 기본 실행에서 제외 |
| `geocoder` | HTTP mock | 폴백 단계 전환, 429 중단·재개 |
| `stats` | `pytest -m db` | 집계 정확성, caveat 부착 조건 |
| `onbid_mcp/tools` | **계약 테스트** | 입출력 스키마, `meta`·`query_echo` 존재, 오류 5종 |
| 실호출 | `pytest -m live` 별도 마커 | 기본 실행에서 제외. 스모크·검수용 |

### 5.2 fixture 정책

- `tests/fixtures/onbid/` 에 실응답 JSON을 커밋한다. **`serviceKey`는 반드시 제거**한다.
- 최소 확보 대상: 정상 목록(다건), **단건 응답**(`items.item`이 배열이 아닌 케이스), `NODATA_ERROR`,
  **최저입찰가 비공개 케이스**, 감정가 null 케이스, 용도 트리 각 depth.
- 특이 케이스를 만나면 fixture로 추가하고 테스트를 먼저 쓴다.

### 5.3 검증 명령

```bash
source venv/bin/activate
ruff check .                          # 1. 린트
mypy core/ onbid_mcp/ api/ tests/ scripts/  # 2. 타입 체크
pytest tests/ -v        # 3. 테스트 (live·db 마커 제외)

pytest -m db            # DB 통합 테스트 (Supabase 연결 필요)
pytest -m live          # 온비드·카카오 실호출 (쿼터 소모)
```

**마커 정책**: 네트워크가 필요한 테스트는 전부 마커로 분리한다. 기본 `pytest`는 오프라인에서
완주해야 하며, 이것이 fixture 기반 순수 테스트 비중을 높게 유지하는 강제 장치다.

3단계 전부 통과해야 마일스톤 완료로 본다 (N5.3).

---

## 6. 리스크와 대응

| 리스크 | 영향 | 대응 | 관련 |
|---|---|---|---|
| 오퍼레이션명 불일치(`getRlstCltrList` vs `...2`) | M1 착수 지연 | M1 첫 작업을 스모크로 배치 | D11 |
| 개발계정 트래픽 소진 | 배치 미완주 | 증분 모드 우선, 전량은 주 1회. 운영계정 신청 병행 | C2 |
| ~~최저입찰가 비공개 케이스 과다~~ | 실측 6,910건 중 1건뿐 | null 허용 + `min_bid_amt_text` 보존 | F4.7 ✅ |
| ~~최저가율 단위 오해~~ | **온비드 비율 필드는 채움률 0%** — 아예 오지 않는다 | **자체 계산으로 확정**(산출률 95%). 온비드 제공값을 쓰지 않는다 | F4.5·F4.9 ✅ |
| 최저가율 100% 초과를 이상치로 오인 | 전체의 9.8%(676건)가 통계에서 사라짐 | 상한을 두지 않는다. 구간에 `100%+` 를 별도로 둔다 | SPEC §8.3 |
| 용도 트리 순회 비용 | 리프마다 1회씩 **116회 호출·12초** | 매 배치가 아니라 주기적으로만 갱신한다. live 테스트는 `max_depth=2` 로 얕게 | F1.2 |
| 입찰정보 대상을 잘못 잡아 쿼터 낭비 | 일 1,000건 한도를 `03` 응답으로 태움 | 유찰 0회·수의계약 물건을 사전 제외. 실측으로 대상이 1,100여 건까지 줄었다 | F1.11 |
| **낙찰가율을 일반 통계로 오독** | 표본이 재공매 물건뿐이라 실제보다 왜곡됨 | `meta.population` 과 `caveat` 을 응답에 강제. 지표 이름에 분모를 명시 | §8.3, D18 |
| 회차 이력 적재 시 PK 충돌 | 여러 공매 사건이 섞여 `pbct_nsq` 가 중복 | PK 에 `opbd_dt` 포함. 조회는 `opbd_dt` 정렬 | §7, D19 |
| 페이지 하나의 실패가 전체 수집을 날림 | 수천 건을 다시 받아야 함 | 페이지 실패는 기록 후 계속. 예외를 던지지 않고 결과에 담아 반환 | F1.4·F1.14 |
| 배치 중 쿼터 소진 | 남은 범위를 알 수 없어 처음부터 다시 | 즉시 중단 + `stopped_at` 기록 → `batch_run.resume_token` 으로 이어받기 | F1.13·N2.2 |
| **불완전 수집 회차의 tombstone** | 실패한 페이지의 물건이 통째로 `종료추정` 이 된다 | `is_complete` 가 거짓이면 판정을 건너뛰고 `partial` 로 닫는다 | F4.17 |
| 목록 상태 범위 불명 | tombstone 전략 과잉/과소 | 스모크에서 `pbctStatCd` 분포 집계 | D14 |
| 카카오 쿼터 타 프로젝트와 공유 | 429 | 캐시 우선, 호출량 로깅, 중단·재개 | C3, C4 |
| 지오코딩 90% 미달 | AC3 미충족 | VWorld 폴백 착수 → 그래도 미달 시 `approx` 허용 범위를 SPEC에서 재조정 | AC3 |
| 재산유형 혼재로 통계 오독 | 잘못된 결론 | caveat + breakdown 강제 (SPEC §8.3) | §2.1 |
| 키 유출 | 쿼터 소진·과금 | `.env` + `.gitignore`, fixture에서 키 제거, M7 점검 | N4 |
| **공개 저장소의 Actions 로그** | 실행 로그가 공개되므로 시크릿이 찍히면 즉시 유출 | 워크플로가 시크릿을 출력하지 않고, 클라이언트가 httpx 요청 로그를 막는다(N4.5). 첫 실행에서 육안 확인 | F8.4 |
| **cron 지연·누락** | 깃허브 cron 은 혼잡 시 밀리거나 건너뛴다 | 배치가 멱등(AC2)이고 이월이 자동이라 다음 실행이 따라잡는다. 분 단위 정확도를 요구하지 않는다 | F8.1 |
| **전량·증분 동시 실행** | tombstone 기준 시각이 어긋나고 같은 행을 두 트랜잭션이 다툰다 | 두 워크플로를 같은 concurrency 그룹으로 | F8.3 |
| **공유 Supabase의 anon 노출** | 다른 웹앱 키로 공매 데이터 조회 가능 → §2.4 위반 소지 | 마이그레이션에서 RLS 활성 + revoke를 **테이블 생성과 동시에**. M3에서 실측 | §6.6, AC12 |
| service_role 키 유출 | RLS 우회 전체 권한 | 서버 전용. 로그·fixture·커밋 금지 | §6.6 R2 |
| 원격 DB 지연으로 N1.1 미달 | MCP 응답 느려짐 | 인덱스 설계 준수, 커넥션 재사용, M5에서 실측 | N1.1, N1.3 |
| 네트워크 단절 | 조회·배치 중단 | `upstream_error` 반환, 배치는 `resume_token`으로 복구 | C8, AC9 |

---

## 7. Phase 2 준비 (지금 하지 않지만 구조로 열어두는 것)

| 항목 | 지금 하는 일 | 이유 |
|---|---|---|
| 공적장부 연동 | `ltno_pnu` / `rdnm_pnu` 저장 | **온비드가 PNU를 직접 제공**한다. 계획서가 "전체의 관문"이라 했던 주소→PNU 변환이 불필요 |
| 지도 Web | `core`를 import 가능한 패키지로 유지, `api/` 얇게 | Web은 별도 저장소에서 `core` 또는 **Supabase를 직접** 공유 |
| Web의 DB 접근 | Phase 1은 anon 전면 차단 | Phase 2에서 **RLS 정책을 새로 설계**한다. 전면 차단을 그냥 푸는 방식은 금지 (§6.6 R6) |
| 낙찰가율 통계 | `pbct_stat_cd` 원본 보존, tombstone | D14 결과에 따라 되살아날 수 있다 |
| 등기부 분석 | 물건상세의 **등기사항증명서 주요정보 목록** 존재를 기록 | 계획서 Phase 2 #9(등기부 OCR)의 상당 부분이 불필요해질 가능성 |
| 카카오톡 알림 | `onbid_cltr_history` 스키마 확보 | 알림 트리거의 데이터 전제 |

**Phase 1에서는 위 어떤 것도 구현하지 않는다.** 스키마와 경계만 열어둔다.
