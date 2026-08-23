# 온비드 공매물건 MCP — 명세서 (SPEC)

**프로젝트명**: `onbid-mcp`
**작성일**: 2026-08-18
**버전**: 1.0
**상위 문서**: `idea_md/공매물건-지도검색-MVP-개발계획서-v0.3.md` (이하 "계획서 v0.3")
**상태**: 초안 — §14 미확정 항목 해소 후 확정

---

## 1. 개요

### 1.1 목적

한국자산관리공사 온비드(Onbid)의 **공매 물건 데이터를 수집·정규화·좌표화**하여, LLM이 MCP 툴로 조회할 수 있는 **데이터 레이어**를 구축한다.

본 프로젝트(Phase 1)의 산출물은 **Core 파이프라인 + MCP 서버**이며, 지도 Web은 범위 밖이다(§2.3).

### 1.2 배경

- 온비드 API는 **좌표를 제공하지 않고**, 지역코드·주소 문자열 기준으로만 물건을 반환한다. bbox 검색이 API 차원에서 불가능하다.
- 따라서 사전 수집 후 좌표를 부여해 로컬 DB에 적재하고, 조회는 DB를 대상으로 한다(자체 인덱스 방식).
- 기존 경·공매 플랫폼은 대부분 유료 구독 기반이며 물건 정보와 공적장부·시세가 분리되어 있다. 무료 공공 API만으로 반복 검토 작업을 자동화하는 것이 목표다.

### 1.3 핵심 가치 가설

> "유찰이 반복돼 저렴해진 물건이 어느 지역·용도에 몰려 있는지 조건 조회 한 번으로 확인할 수 있다."

Phase 1(MCP)은 이 문장을 **LLM 대화로** 검증한다. 지도 시각화는 Phase 2에서 같은 데이터 위에 얹는다.

### 1.4 포지셔닝

| Phase | 대상 | 산출물 |
|---|---|---|
| **Phase 1 (본 SPEC)** | 개인 사용 (작성자 본인) | Core + MCP 서버 (stdio) |
| Phase 2 | 공개 검토 | 지도 Web, 공적장부 툴, 카카오 로그인·알림 |
| Phase 3 | 중개사 B2B | 확인·설명서 초안, 계약서 검토 |

Phase 2 이후는 계획서 v0.3 §13을 따르며 본 SPEC의 범위가 아니다.

### 1.5 설계 원칙

**① MCP는 데이터 레이어, 판단은 사용자 몫**

툴은 공공데이터 조회만 수행하고 결론을 내지 않는다. 판단 주체를 사용자(및 사용자의 LLM)로 유지하는 것이 법적 안전성의 핵심이다.

- 툴 이름에 판단 뉘앙스를 넣지 않는다 (❌ `analyze_property_risk`, `recommend_items`)
- 응답은 원천 데이터 + 출처 + 조회시점으로 구성한다
- 파생 지표는 **계산식이 명시적인 것만** 포함한다 (최저가율 = 최저입찰가 ÷ 감정가)

**② 계약 우선 (contract-first)**

MCP 툴의 입출력 계약(§8)을 먼저 확정하고, DB 스키마(§7)와 store 쿼리를 그 계약에서 역산한다. 계약 없이 파이프라인을 만들면 마지막 단계에서 필드 부족이 드러나 스키마를 거슬러 수정해야 한다.

**③ 툴 표면은 좁게**

툴 개수를 늘리는 대신 파라미터를 정교하게 만든다. 노출된 툴이 많을수록 LLM의 오호출과 법적 표면이 함께 늘어난다.

---

## 2. 범위

### 2.1 데이터 범위

| 축 | 범위 | 사유 |
|---|---|---|
| 지역 | **서울특별시** | 지오코딩 쿼터 절약, 실패 케이스 육안 검수 가능 |
| 물건 종류 | **부동산만** (자동차·유가증권 제외) | 후속 대장·지적 연동 대상이 부동산에 한정 |
| 물건 상태 | **입찰 진행 중** 수집 + **종료 추정분 tombstone 보존** | §2.2 참조 |
| 물건유형 | `cltrTypeCd=0001` (부동산) | 위와 동일 |
| **처분방식** | **`dspsMthodCd=0001` (매각)만 — 임대 제외 (사용자 확정 2026-08-18)** | 온비드는 매각·임대가 섞여 있다. 임대 물건은 감정가·유찰 개념이 달라 최저가율 가설이 성립하지 않는다 |
| **재산유형** | **전 유형 (`prptDivCd` 10종 전체, 사용자 확정 2026-08-18)** | 처분방식 필터(매각)로 임대를 걸러내므로 재산유형은 제한하지 않는다 |

**실측 규모 (2026-08-19 기준)**: 서울·부동산·매각 조건 전량 **6,161건**
(`pvctTrgtYn=N` 5,005건 + `Y` 1,156건). 재산유형 분포는 **압류재산 57.6% · 기타일반재산 42.2%**이며
나머지 8종은 합계 0.15%에 불과하다 — 전 유형 수집을 택했으나 실질은 2종이므로 통계 혼재 우려는 예상보다 작다.

> **범위 조합 주의**: 임대 제외는 `dspsMthodCd`(처분방식) 축으로 처리하며 `prptDivCd`(재산유형) 축과는 무관하다.
> `prptDivCd`는 요청 필수 파라미터이므로 10종을 쉼표로 모두 나열해 호출한다.

**전 재산유형 수집에 따른 필수 보정** — 재산유형마다 감정가 산정과 유찰 저감 체계가 다르므로:

1. `search_auction_items`·`get_auction_stats`에 **재산유형 필터·집계 축을 반드시 제공한다** (§8.1, §8.3)
2. 최저가율 분포를 **재산유형 구분 없이 합산해 제시하지 않는다** — 서로 다른 저감 체계가 섞여 오독을 유발한다
3. 배치 소요·지오코딩 쿼터를 압류재산 단독 대비 재산정한다 (N1.2)

**처분방식·재산유형 축은 계획서 v0.3에 없었다.** 공고목록 API 활용가이드에서 두 축의 존재를 확인하고 추가했다.
**변경 일자**: 2026-08-18 / **사유**: 임대 물건 혼입 시 `min_bid_rate`가 무의미해짐

### 2.2 종료 물건 취급 (계획서 v0.3에서 변경)

계획서 v0.3 §2.1은 "입찰 진행 중"만 수집한다고 규정했으나, 그 경우 **전일 존재하던 물건이 익일 응답에서 사라지면 추적이 불가능**하다. 변경 이력·상태 알림·분포 통계가 모두 성립하지 않는다.

→ **수집 대상은 진행 중 물건으로 유지하되, 응답에서 사라진 물건은 삭제하지 않고 `status='종료추정'`으로 표시하여 보존한다** (tombstone). 종료 물건을 새로 수집하지는 않는다.

**변경 일자**: 2026-08-18 / **사유**: 변경 이력·통계·알림의 전제 확보

### 2.3 범위 밖 (Out of scope)

| 항목 | 사유 |
|---|---|
| 지도 Web UI | Phase 2. 본 SPEC은 데이터 레이어에 한정 |
| 공적장부(건축물대장·토지대장·토지이용계획) 조회 | Phase 2. PNU 변환 선행 필요 |
| 실거래가 비교 | Phase 2 |
| 카카오 로그인·카카오톡 알림 | Phase 2. 개인 사용 단계에서는 불필요 |
| 등기부 OCR, 계약서 검토 | Phase 2·3 |
| MCP 서버 공개 배포 | 계획서 v0.3 §10 게이트 통과 후 별도 판단 |

### 2.4 Non-goals (항구적 금지)

| 항목 | 사유 |
|---|---|
| 중개 행위 및 알선 | 공인중개사법상 무등록 중개업 해당 |
| **게시형** 중개대상물 표시·광고 | 공인중개사법 제18조의2 제3항. 조회형(조건 입력 → 결과)만 허용 |
| 물건 랭킹·추천·점수화 | 판단 주체 이전. §1.5① 위배 |
| "저평가", "급매" 등 평가·유인 표현 | 표시·광고 소지 |
| 투자 자문·법률 자문 | 자격 요건 위반 |

---

## 3. 사용자 및 사용 시나리오

### 3.1 사용자

**Phase 1 사용자는 작성자 본인 1인**이며, MCP 클라이언트(Claude Code / Claude Desktop)를 통해 접근한다. 스스로 물건을 판단할 수 있는 사용자를 전제하며, 일반 소비자 대상 제공은 하지 않는다.

### 3.2 대표 시나리오

| # | 시나리오 | 사용 툴 |
|---|---|---|
| S1 | "서울 강남구 아파트 중 3회 이상 유찰된 물건 보여줘" | `search_auction_items` |
| S2 | "그 중 최저가율 60% 이하만" | `search_auction_items` (min_rate/max_rate) |
| S3 | "이 물건 상세 정보와 온비드 원문 링크" | `get_auction_detail` |
| S4 | "서울 전체에서 유찰 횟수 분포가 어떻게 되나" | `get_auction_stats` |
| S5 | "이 주소 좌표가 어떻게 되나" | `get_address_geocode` |
| S6 | "용도 코드에 뭐가 있나" | Resource `onbid://codes/usages` |
| S7 | "데이터는 언제 기준이야?" | Resource `onbid://dataset/status` |

---

## 4. 기능 요구사항

우선순위: **P0** = MVP 필수, **P1** = MVP 포함하되 후순위, **P2** = Phase 2 이월

### F1. 수집 (core/collector)

| ID | 요구사항 | 우선 |
|---|---|---|
| F1.1 | 온비드 물건목록 API에서 **서울·부동산·진행중** 조건으로 전체 물건을 페이지 순회 수집한다 | P0 |
| F1.2 | 온비드 코드조회 API에서 **용도 트리와 주소 조합**을 수집하여 캐시한다. 용도는 `upCtgrId` 재귀 순회(**리프는 `03 NODATA_ERROR`**, 빈 `upCtgrId` 는 `99` 오류라 루트 `10000` 을 명시해야 한다). 실측 116노드 · 116회 호출 · 12초 | P0 |
| F1.3 | API 응답 원본을 `raw_payload`로 보존하여 파싱 오류 시 재처리가 가능해야 한다. **응답 행에 우리 값을 써넣지 않는다** — 온비드는 `pvctTrgtYn`을 응답에도 담아 보내므로 덮어쓰면 원본이 아니게 되고 API 값과의 불일치가 숨겨진다. 수집 그룹 같은 부가 정보는 별도 필드에 둔다 | P0 |
| F1.4 | HTTP 오류·타임아웃 시 지수 백오프로 최대 3회 재시도하고, 초과 시 해당 페이지를 실패 기록 후 다음 페이지를 계속한다. **연속 실패가 임계치를 넘으면 해당 그룹을 포기한다** — 쿼터를 소모하며 헛돌지 않기 위해서다 | P0 |
| F1.13 | **쿼터 소진(`22`)·키 문제(`20`·`21`·`30`~`33`)는 페이지 실패와 다르게 처리한다.** 즉시 전체를 중단하고 **재개 지점(그룹·페이지)** 을 기록한다 (N2.2) | P0 |
| F1.14 | 수집은 **어떤 실패에도 예외를 던지지 않는다.** 이미 받은 데이터를 버리지 않도록 실패 내역을 결과에 담아 반환한다 | P0 |
| F1.5 | 1회 실행의 수집 결과를 요약 로깅한다 (요청 페이지 수, 수집 건수, 실패 페이지 수, 소요 시간) | P0 |
| F1.6 | 물건 상세 API 연동 (목록에 없는 필드 보강). 일일 트래픽 제약상 온디맨드 또는 분할 배치로 수행한다 | P1 |
| F1.7 | 물건상세 **입찰정보 API**(`OnbidCltrBidDtlSrvc2/getCltrBidInf2`)로 **회차별 유찰 이력**(`prcnBidClgList`)과 회차별 입찰정보(`cseqBidInfClgList`)를 취득한다. **일일 트래픽 1,000건 · 물건당 1회 호출** | P1 |
| F1.11 | 호출 대상에서 **두 부류를 제외한다** — 둘 다 `03 NODATA_ERROR` 가 돌아와 쿼터만 소모한다. ① **유찰 0회**(`prcnBidClgList` 가 비어 있음, 서울 물건의 68%) ② **수의계약가능(`pvctTrgtYn=Y`)** — 수의계약은 입찰이 아니라 입찰정보가 없다(실측 18/18건 `03`). 두 조건 적용 후 서울 대상은 **1,088건**(2026-08-23 첫 실적재 실측)이라 **하루 예산 안에 들어온다** | P1 |
| F1.16 | **회차 배치의 예산 롤링은 재개 토큰이 아니라 `bid_round_synced_at`(마지막 시도 시각) 오름차순으로 한다.** 대상이 1,100건 규모의 *집합*이라 스칼라 토큰으로 표현할 수 없고, 토큰 방식은 배치가 죽으면 진행 위치를 잃는다. 시각순 정렬은 상태를 따로 들고 다니지 않으면서 자동으로 라운드로빈이 된다. **시도 시각은 성공·이력없음(`03`)·실패를 가리지 않고 갱신한다** — 성공만 갱신하면 이력이 없는 물건을 매일 다시 호출해 예산을 태우고, 실패를 제외하면 고장난 한 건이 매일 예산을 선점한다. 새로 등장한 물건은 null 이라 자연히 최우선이 된다 | P1 |
| F1.12 | 롤링 순회와 별개로, MCP `get_auction_detail` 호출 시 해당 물건의 입찰정보를 **온디맨드 취득**할 수 있다. 일일 호출량은 F3.5와 동일하게 로깅·상한 관리한다 | P1 |
| F1.15 | **온비드 원문 링크(`onbid_url`)를 응답 행에서 조립한다.** 식별자 4개(`onbidCltrno`·`onbidPbancNo`·`pbctNo`·`pbctCdtnNo`)가 모두 있어야 하며, **하나라도 없으면 링크를 만들지 않고 null 로 둔다** — 부분 URL 은 온비드가 HTTP 500 을 내므로 깨진 링크를 주는 것보다 없는 편이 낫다 | P0 |
| F1.8 | `mdfcnYmdStart`/`mdfcnYmdEnd`를 이용한 **증분 수집 모드**를 제공한다. 전량 모드와 증분 모드를 배치 옵션으로 분리한다 | P0 |
| F1.9 | 물건목록은 `pvctTrgtYn` = `Y`·`N` **양쪽을 모두 순회**해야 전량이 확보된다 | P0 |
| F1.10 | 지역 필터는 `lctnSdnm="서울특별시"` **문자열**로 지정한다 (온비드는 법정동코드를 쓰지 않는다) | P0 |

### F2. 주소 정제 (core/normalizer)

> **정정 (2026-08-22)**: `cltrRadr`(도로명 전체)·`zadrNm`(지번 전체)은 **물건상세 응답에만 있고
> 물건목록에는 없다.** 물건상세는 물건당 1회 호출이라 6,910건 전량에 쓸 수 없다.
> 08-18에 "전체 주소가 별도 필드로 온다"고 적었던 것은 물건상세 가이드를 보고 판단한 오류다.
>
> **목록에서 쓸 수 있는 주소 소스는 셋이다.**
>
> | 소스 | 커버 | 성격 |
> |---|---|---|
> | `ltnoPnu` 지번PNU 19자리 | **76%** | 구조화된 코드. 파싱 실패가 없다 |
> | `onbidCltrNm` 물건명 | 100% | 전체 주소를 담지만 건물명·층·호 꼬리표가 붙는다 |
> | `lctnSdnm`+`lctnSggnm`+`lctnEmdNm` | 100% | 읍면동까지만 |

| ID | 요구사항 | 우선 |
|---|---|---|
| F2.1 | 지오코딩에 넣을 주소를 아래 순서로 고른다. **① `ltnoPnu` 에서 지번주소 조립** (법정동코드+산여부+본번+부번 → `읍면동 본번-부번`) **② 물건명 파싱** **③ 시도+시군구+읍면동 조합**. 어느 소스를 썼는지 함께 기록한다 | P0 |
| F2.6 | PNU 조립 결과는 **물건명과 대조해 검증할 수 있다.** 전량 6,910건 적용 결과 PNU 5,246 + 물건명 1,335 = **지번 특정 95.2%**, 동 근사 4.7%, 실패 1건. PNU↔물건명 불일치 10건은 모두 물건명의 공백 표기 차이였다 | P1 |
| F2.7 | 지오코딩 결과의 신뢰도 판정을 위해 **주소를 어느 소스에서 얻었는지 기록한다** (`pnu` / `item_name` / `district`). `district` 는 지번이 특정되지 않아 동 중심 근사가 되며 `geocode_status='approx'` 로 이어진다 | P0 |
| F2.2 | 꼬리표 제거는 **깨뜨리는 것만** 한다. 카카오 실측 결과 지오코딩을 실패시키는 것은 **`외 N필지`·`외 N필` 뿐**이며, 건물명·`제O층`·`제OOO호`·`제지하O층`·`N동 N호`·괄호 부기·쉼표 상세는 모두 흡수된다. **흡수되는 꼬리표는 남긴다** — 건물명이 있으면 오히려 정확도가 오른다 | P0 |
| F2.8 | 상세주소 절단(`strip_detail_suffix`)은 그보다 공격적이므로 **지오코딩 실패 시 폴백에서만** 쓴다. 평상시에 쓰면 건물명이 사라져 정확도가 떨어진다 | P1 |
| F2.9 | 어떤 정제도 **결과가 비거나 구두점만 남으면 되돌린다.** 그런 문자열로는 지오코딩할 수 없다 | P0 |
| F2.3 | 원본 주소 필드는 어떤 경우에도 원형 그대로 보존한다 | P0 |
| F2.4 | 정제 결과가 빈 문자열이거나 시군구 이하가 소실되면 정제를 적용하지 않고 원본을 사용한다 | P1 |
| F2.5 | 정제 규칙은 순수 함수로 구현하고 단위 테스트를 선행 작성한다 (TDD) | P1 |

### F3. 지오코딩 (core/geocoder)

| ID | 요구사항 | 우선 |
|---|---|---|
| F3.1 | 아래 폴백을 순서대로 시도한다 | P0 |
| F3.0 | **온비드가 `ltnoPnu`(지번PNU 19자리)·`rdnmPnu`(도로명PNU)를 제공하므로 원본 그대로 저장한다.** PNU 앞 10자리가 법정동코드이며 별도 변환이 필요 없다. **단 실측 결측률이 지번PNU 28.9% / 도로명PNU 36.2%로 낮지 않아, PNU가 지오코딩을 대체하지는 못한다** | P0 |
| F3.7 | **`lctnEmdNm`(읍면동)은 실측 결측률 0%**이다. 따라서 폴백 5단계(`시도+시군구+읍면동` 조합)는 항상 성립하며, **`failed`는 원리적으로 발생하지 않아야 한다.** 발생 시 버그로 간주하고 원인을 조사한다 | P0 |
| F3.2 | 모든 시도 전에 `onbid_geocode_cache` 를 먼저 조회한다 (동일 주소 재호출 금지) | P0 |
| F3.3 | 카카오 로컬 API 429 응답 감지 시 **즉시 중단**하고 처리 위치를 기록하여 다음 실행에서 이어받는다 | P0 |
| F3.4 | 일시적 오류(5xx, 타임아웃)는 지수 백오프로 최대 3회 재시도한다 | P0 |
| F3.5 | 일일 외부 API 호출량을 로깅한다 (카카오 앱을 타 프로젝트와 공유하므로 필수) | P0 |
| F3.6 | 결과에 `geocode_status` / `geocode_level` / `geocode_src`를 기록한다 | P0 |

**폴백 단계 (F3.1)**

| 단계 | 시도 | 제공자 | status | level |
|---|---|---|---|---|
| 0 | 캐시 조회 | — | (캐시값) | (캐시값) |
| 1 | `cltrRadr` 도로명주소 | 카카오 | ok | road |
| 2 | `zadrNm` 지번주소 | 카카오 | ok | jibun |
| 3 | 꼬리표 제거 후 재시도 | 카카오 | ok | trimmed |
| 4 | 정제 주소 재시도 | VWorld | ok | trimmed |
| 5 | `lctnSdnm`+`lctnSggnm`+`lctnEmdNm` 조합 → 중심좌표 근사 | 카카오 | approx | dong_center |
| 6 | 전부 실패 | — | failed | (null) |

**계획서 v0.3 대비 완화된 점**: 5단계가 "읍면동 절단 후 내부 테이블 조회"에서 **"온비드가 준 읍면동 3필드를
그대로 조합해 조회"** 로 바뀌었다. 자체 읍면동 중심좌표 테이블(구 D6)이 불필요하다. 또한 PNU가 확보되므로
지오코딩이 실패해도 **법정동 단위 위치는 항상 알 수 있다.**

### F4. 적재 (core/store)

| ID | 요구사항 | 우선 |
|---|---|---|
| F4.1 | **`(cltr_mng_no, pbct_cdtn_no)` 복합키**를 기준으로 `insert ... on conflict do update`하며, 동일 입력에 대해 **재실행 시 결과가 동일해야 한다** (멱등성) | P0 |
| F4.10 | 적재 대상은 **Supabase(PostgreSQL)** 이며 psycopg로 직접 접속한다. 6천여 건을 **배치 upsert**로 처리하고 행 단위 왕복을 만들지 않는다 | P0 |
| F4.11 | DDL은 `migrations/NNN_*.sql`로 관리하고 재실행 가능하게 작성한다. **콘솔에서 손으로 스키마를 바꾸지 않는다** (§6.6) | P0 |
| F4.2 | 이번 배치 응답에 없는 물건은 삭제하지 않고 `status='종료추정'`으로 갱신한다 (tombstone). **전량 모드에서만 판정**하며, **판정 범위는 수집 범위와 일치해야 한다** | P0 |
| F4.13 | tombstone 판정은 **범위를 필수 인자로 받는다.** 수집을 한 자치구로 좁힌 뒤 서울 전체를 판정하면 나머지가 통째로 종료 처리된다 — 실측상 강남구만 수집하고 전체 범위로 판정하면 **6,594건이 잘못 뒤집힌다** | P0 |
| F4.3 | 모든 행에 `first_seen_at` / `last_seen_at`을 기록한다. `last_seen_at`은 응답에 등장한 배치에서만 갱신한다 | P0 |
| F4.4 | `min_bid_amt`, `fail_cnt`, `status` 변경 시 `onbid_cltr_history` 에 이력을 적재한다. **F1.7의 `prcnBidClgList`가 회차별 이력을 통째로 제공하므로, 유찰 물건은 첫 배치부터 전체 이력을 확보한다.** 자체 diff 누적은 유찰 0회 물건과 상태 변경 추적을 담당한다 — 두 수단이 상호 보완이며 어느 쪽도 단독 주 수단이 아니다. **처음 본 물건은 이력을 남기지 않는다** — null→값 은 '변경' 이 아니라 '등장' 이고 그 시각은 `first_seen_at` 이 이미 갖고 있다. 이름·썸네일 등 나머지 필드는 추적하지 않는다(잡음이 하락 곡선을 덮는다) | P1 |
| F4.14 | **diff 판정은 적재보다 먼저 수행한다.** upsert 뒤에 비교하면 DB 값이 이미 새 값이라 차이가 0이 되고 **예외 없이 조용히** 이력이 사라진다. 두 동작을 묶은 단일 경로(`upsert_with_history`)를 기본으로 제공해 순서를 잊을 수 없게 한다 | P0 |
| F4.5 | 최저가율은 **자체 계산(`min_bid_amt / appraisal_amt`)이 주 수단**이다. `appraisal_amt`가 0·null이거나 최저입찰가 파싱이 실패하면 **null**로 저장한다 (0 치환·0 나눗셈 금지). **실측상 6,161건 중 95.0%가 계산 가능하다** | P0 |
| F4.9 | 온비드의 비율·할인율 필드(`apslPrcCtrsLowstBidRto`·`frstCtrsLowstBidPrcRto`·`feeRate`)는 **실측 채움률 0%**이므로 신뢰하지 않는다. 원본은 보존하되 조회·통계에 사용하지 않는다 | P0 |
| F4.7 | **`lowstBidPrcIndctCont`는 숫자가 아닌 문자열일 수 있다** — 실측 6,910건 중 `"비공개"` 1건. 파싱 실패 시 `min_bid_amt`를 null로 두고 원문을 `min_bid_amt_text`에 보존한다. **값이 아예 없는 것과 가려진 것을 구분한다** — 전자는 데이터 부재, 후자는 온비드가 의도적으로 감춘 것 | P0 |
| F4.12 | 금액은 **음수와 소수를 거부**한다. 금액이 음수일 수 없고, 원 단위 필드의 소수는 데이터 문제이지 반올림 대상이 아니다 — 조용히 받아들이면 최저가율이 음수가 되거나 금액이 어긋난다 | P0 |
| F4.8 | 압류재산(`0007`)의 `lowstBidPrcIndctCont`는 온비드 화면상 **'공매예정가격'** 으로 표시된다. 표기 차이를 응답·문서에 반영한다 | P1 |
| F4.6 | 배치 실행 메타(`synced_at`, 처리 건수, 지오코딩 성공률)를 `onbid_batch_run` 테이블에 기록한다. **행은 배치 시작 시점에 연다** — 끝날 때 한 번에 쓰면 배치가 죽었을 때 아무 흔적도 남지 않는다. `mode`(`full`|`delta`|`rounds`|`codes`)와 `status`(`ok`|`partial`|`failed`)는 허용값을 검증한다 — `mode` 오타는 tombstone 판정 여부를 뒤집는다 | P0 |
| F4.15 | **완주(`ok`)한 배치는 `resume_token` 을 남기지 않는다.** 남으면 다음 실행이 이유 없이 중간부터 시작한다. 재개 지점 조회는 **종료된 배치만** 본다 — 방금 연 자기 행을 읽으면 재개가 성립하지 않는다. 전량·증분은 순회 방식이 달라 재개 지점을 공유하지 않는다 (N2.2) | P0 |
| F4.16 | **배치 오케스트레이션은 트랜잭션 경계를 명시한다.** `onbid_batch_run` 행은 열자마자 **즉시 커밋**한다 — 데이터와 같은 트랜잭션에 묶으면 배치가 죽었을 때 메타까지 함께 사라져 F4.6이 무의미해진다. 반면 **이력·적재·tombstone 은 한 트랜잭션**으로 커밋한다 — 이력만 남고 적재가 실패한 상태를 만들지 않는다 | P0 |
| F4.17 | **수집이 완주하지 못했으면 tombstone 을 판정하지 않는다.** 페이지 실패·상한 도달·중단이 있으면 '응답에 없음' 이 '사라짐' 을 뜻하지 않는다 — 실패한 페이지의 물건이 통째로 종료 처리된다. `CollectResult.is_complete` 가 거짓이면 판정을 건너뛰고 배치를 `partial` 로 닫는다. F4.2·F4.13 과 함께 tombstone 오작동을 막는 **세 번째 잠금장치**다 | P0 |

### F5. 조회 API (FastAPI) — MCP의 내부 의존

| ID | 요구사항 | 우선 |
|---|---|---|
| F5.1 | `GET /api/items` — 조건 필터 조회 (§8.1 `search_auction_items`와 동일 필터 집합) | P0 |
| F5.2 | `GET /api/items/{cltr_no}` — 단건 상세 | P0 |
| F5.3 | `GET /api/stats` — 분포 집계 | P0 |
| F5.4 | `GET /api/items?bbox=` — 좌표 사각형 조회. **MCP 툴로는 노출하지 않으며** Phase 2 지도 전용 | P1 |
| F5.5 | 서버는 로컬 바인딩(127.0.0.1)만 허용한다 | P0 |

> **주**: MCP 서버는 HTTP를 거치지 않고 `core`를 직접 import한다(§9.2). FastAPI는 Phase 2 Web을 위한 것이며, MCP와 동일한 store 쿼리 함수를 공유한다.

### F6. MCP 서버 (onbid_mcp/)

| ID | 요구사항 | 우선 |
|---|---|---|
| F6.1 | stdio 트랜스포트로 동작하는 MCP 서버를 제공한다 | P0 |
| F6.2 | 툴 4종을 노출한다: `search_auction_items`, `get_auction_detail`, `get_auction_stats`, `get_address_geocode` (계약은 §8) | P0 |
| F6.3 | 모든 툴 응답에 `meta` 블록을 포함한다 (§8.6) | P0 |
| F6.4 | 모든 툴 응답에 `query_echo`(실제 적용된 조건)를 포함한다 | P0 |
| F6.5 | 오류는 §8.7 오류 코드 체계로 구분해 반환한다 | P0 |
| F6.6 | `region` / `usage` / `prpt_div` 파라미터는 **코드값과 한글 명칭을 모두 수용**하며, 값 모양으로 자동 판별한다. 단 `region`은 온비드에 코드가 없으므로 **명칭 전용**이다. `prpt_div`는 쉼표 복수 지정을 받는다 | P0 |
| F6.13 | **쉼표 목록은 일부만 맞으면 전체를 실패로 본다.** 맞는 항목만 조용히 살리면 사용자가 요청하지 않은 범위로 조회된다 | P0 |
| F6.14 | 해석이 **여러 곳에 해당하면**(예: `신사동` → 강남구·은평구) 실패가 아니라 **모호**로 구분하고, 후보가 아니라 **매칭된 것들**을 보여 사용자가 고르게 한다 | P0 |
| F6.7 | 명칭 매칭 실패 시 `invalid_param` 오류에 **후보 목록**을 담아 반환한다. 검색어가 비었으면 **상위 분류**를 보여준다 — 빈 목록보다 "무엇을 고를 수 있는지"가 유용하다 | P0 |
| F6.12 | **용도를 중분류로 지정하면 하위 소분류까지 확장해 조회한다.** 물건 데이터에는 소분류 코드만 들어 있어(실측 35종), 확장하지 않으면 `주거용건물` 검색이 **0건**이 된다(확장 시 3,506건) | P0 |
| F6.8 | `sort`는 화이트리스트로 제한하고, 기본값은 중립적인 마감일 오름차순으로 한다 | P0 |
| F6.9 | 툴 description에 "판단하지 않음 / 배치 수집분 / 입찰 전 온비드 원문 확인" 취지를 명시한다 | P0 |
| F6.10 | `get_address_geocode`는 서버 측 **일일 호출 상한**을 두고 초과 시 `quota_exceeded`를 반환한다 | P1 |
| F6.11 | 트랜스포트(stdio)와 툴 로직을 분리해 Phase 2의 HTTP 전환 시 툴 코드를 재사용할 수 있어야 한다 | P1 |

### F7. MCP Resource (onbid_mcp/)

| ID | 요구사항 | 우선 |
|---|---|---|
| F7.1 | `onbid://codes/regions` — **물건이 실제로 존재하는** 서울 시군구·읍면동 조합 (실측 247조합·25개 자치구). `getOnbidDtlAddrInfo` 는 행정구역 코드표가 아니라 **등록 물건의 주소 목록**이므로, 결과는 "검색해도 물건이 없는 지역"을 걸러 준다 | P0 |
| F7.2 | `onbid://codes/usages` — 용도 3단 계층 트리 (`getOnbidUsgCodeInfo`를 `upCtgrId=10000`부터 재귀 순회하여 구축) | P0 |
| F7.3 | `onbid://codes/property-types` — 재산유형 10종 코드표 (§6.5 정적 상수) | P0 |
| F7.4 | `onbid://dataset/status` — 최근 배치 기준일시, 총 건수, **재산유형별 건수**, 상태별 건수, 지오코딩 성공률 | P0 |

> Resource로 노출하는 이유: 코드표를 툴로 만들면 LLM이 매 검색마다 왕복 1회를 추가로 쓴다. Resource는 필요할 때만 읽히므로 툴 표면(§1.5③)을 넓히지 않는다.

---

## 5. 비기능 요구사항

| ID | 항목 | 요구사항 |
|---|---|---|
| N1.1 | 응답 성능 | MCP 툴 단건 응답 p95 **1초 이내** (원격 Supabase 조회 기준). 인덱스 미사용 쿼리를 만들지 않는다 |
| N1.3 | 연결 관리 | 배치·MCP 모두 커넥션을 재사용하며 **`prepare_threshold=None`** 으로 연다 (C10). 툴 호출마다 새 연결을 여는 구조를 만들지 않는다 |
| N1.2 | 배치 성능 | **수집 단계는 3~4회 호출로 1분 이내** (실측: `numOfRows=5000`, 전량 6,161건). 병목은 지오코딩이며 **전량 배치 30분 이내** 완주를 목표로 한다 |
| N2.1 | 멱등성 | 배치 재실행 시 DB 상태가 동일해야 한다 (F4.1) |
| N2.2 | 중단 복구 | 429·장애로 중단된 배치가 다음 실행에서 **처리 위치부터 재개**되어야 한다 |
| N2.3 | Graceful degradation | 외부 API 장애 시 기존 DB 데이터로 조회는 정상 동작하며, `meta.synced_at`으로 신선도를 알린다 |
| N3.1 | 쿼터 | 카카오 로컬 API 호출은 캐시 우선. 일일 호출량을 로깅하고 상한 도달 시 중단 |
| N3.2 | 컨텍스트 예산 | `search_auction_items` 기본 `limit=20`, 최대 50. 목록 응답 필드는 12개 이하로 제한 |
| N4.1 | 키 관리 | 모든 키는 `.env`에 저장하고 `.gitignore`에 등록. 코드·문서·로그·커밋에 평문 기입 금지 |
| N4.2 | 키 유출 대응 | 노출 이력이 있는 키는 즉시 재발급 |
| N4.3 | 노출 범위 | MCP 서버는 stdio 로컬 전용. 네트워크 리스닝을 하지 않는다 (DB 아웃바운드 연결은 예외) |
| N4.5 | **HTTP 클라이언트 로그 차단** | 온비드는 인증키를 **쿼리 파라미터**로 요구하고, httpx 는 INFO 레벨에서 요청 URL을 통째로 기록한다 — 로깅을 켜는 순간 키가 로그에 남는다. 예외도 경고도 없이 조용히 새므로 `core/onbid/client.py` 가 import 시점에 `httpx` 로거를 WARNING 으로 낮춘다. **M6 MCP 서버·크론 등 로깅을 켜는 모든 진입점에 같은 전제가 필요하다** |
| N4.4 | 권한 최소화 | `onbid_*` 테이블은 RLS 활성 + 정책 없음 + anon/authenticated grant revoke. **접근은 service_role 전용** (§6.6) |
| N5.1 | 테스트 | 순수 로직 모듈(정제·매핑·계산)은 **테스트 선행 작성**(TDD). 외부 API는 fixture로 대체 |
| N5.2 | 커버리지 | `core/normalizer`, `core/store` 쿼리 빌더는 분기 커버리지 확보 |
| N5.3 | 검증 | `ruff check .` → `mypy core/ onbid_mcp/` → `pytest tests/ -v` 3단계 통과가 마일스톤 완료 조건 |
| N6.1 | 로깅 | 배치 실행별 구조화 로그 (수집·정제·지오코딩·적재 각 단계 건수와 실패) |
| N6.2 | 실패 추적 | 지오코딩 `approx`/`failed` 건은 원인과 함께 조회 가능해야 한다 |
| N7.1 | 출처 표기 | 모든 툴 응답 `meta.source`에 "온비드(한국자산관리공사) / 공공데이터포털" 표기 |
| N7.2 | 면책 표기 | 모든 툴 응답 `meta.notice`에 정보 제공 목적·원문 확인 안내 포함 |
| N7.3 | 실시간 오인 방지 | `meta.is_realtime: false` 고정 |
| N8.1 | 이식성 | Python 3.11+, macOS·Windows 양쪽에서 동작 (개발 환경 이중화) |
| N8.2 | 계층 경계 | `core/`는 `onbid_mcp/`를 import하지 않는다. 의존 방향은 `onbid_mcp → core` 단방향 |
| N8.3 | 파일명 | 리포 내 모든 파일·디렉토리명은 ASCII (macOS NFD / Linux NFC 정규화 차이 회피) |

---

## 6. 기술 스택

| 레이어 | 선택 | 비고 |
|---|---|---|
| 언어 | Python 3.11+ | `venv/` 가상환경 |
| Core | requests(또는 httpx), pydantic | 독립 패키지, import 가능 |
| DB | **Supabase (PostgreSQL)** | Phase 2 Web과 단일 진실원천 공유. 개발 환경 이중화(Mac·Windows) 대응 |
| DB 접속 | **psycopg (직접 연결)** | 대량 upsert에 유리. PostgREST의 1000행 상한·`ON CONFLICT` 제약을 받지 않음 |
| API | FastAPI | Phase 2 Web용. core를 얇게 감쌈 |
| MCP | Python MCP SDK | stdio 트랜스포트 |
| 지오코딩 | **카카오 로컬 REST API** | 주소 파싱이 관대해 실패율 낮음 |
| 지오코딩 보조 | VWorld | 폴백 4단계 |
| 검증 | ruff, mypy, pytest | §5 N5.3 |

**의존성 추가는 사전 확인 대상이다.** 위 목록 외 패키지 도입 시 사용자 승인 후 `requirements.txt`에 반영한다.

### 6.1 좌표계

카카오·VWorld 양쪽 모두 **WGS84**로 통일한다.

### 6.2 외부 API — 온비드 계열 확정

온비드 오픈API는 **레거시 계열**(데이터ID `150008xx`, `15000920`)과 **차세대 계열**(`151572xx`)로 나뉜다.
**차세대 계열로 통일한다.** 차세대에 부동산 물건목록이 존재함을 확인했으므로 계열을 혼용할 이유가 없다.

| 서비스 | 데이터 ID | SPEC 대응 | 상태 |
|---|---|---|---|
| 차세대 온비드 부동산 물건목록 조회서비스 | (승인됨) | F1.1 | **승인 2026-08-18 (개발계정)** |
| 차세대 온비드 코드 및 주소 조회서비스 | (승인됨) | F1.2, F7.1, F7.2 | **승인 2026-08-18 (개발계정)** |
| 차세대 온비드 공고목록 조회서비스 | (승인됨) | `plnm_no`, 입찰 일정 | **승인 2026-08-18. 활용가이드 확보 (§6.4)** |
| 차세대 온비드 부동산 물건상세 조회서비스 | `15157247` | F1.6, §8.2 | **승인 2026-08-18 (개발계정)** |
| 차세대 온비드 물건상세 입찰정보 조회서비스 | `15157251` | F1.7 | **승인 2026-08-18 (개발계정)** |

**활용신청은 5종 전부 완료되었다(2026-08-18).** 남은 M0 작업은 각 API의 활용가이드 확보와 `.env` 구성이다.

**변경 일자**: 2026-08-18 / **사유**: 차세대 계열에 부동산 물건목록 존재 확인, 계열 혼용 불필요

계정 제약: 개발계정 일일 트래픽 제한(오퍼레이션당 1,000건 수준)이 있다. 물건 상세를 건별로 호출하면
서울 수백~수천 건 규모에서 즉시 소진되므로, **상세 보강은 온디맨드 또는 분할 배치**로 설계한다(F1.6).
운영계정 전환은 M1 진행 중 병행 신청한다.

### 6.3 키 관리

| 용도 | 키 종류 | 출처 | 노출 |
|---|---|---|---|
| 온비드 조회 | 일반 인증키(서비스키) 1개 | 공공데이터포털 | 서버 전용. `.env`의 `ONBID_SERVICE_KEY` 단일 항목 |
| DB 쓰기·조회 | `SUPABASE_SERVICE_KEY` / `SUPABASE_DATABASE_URL` | Supabase | **서버 전용 — 절대 클라이언트에 노출 금지.** service_role은 RLS를 우회한다 |
| (Phase 2) Web 읽기 | `SUPABASE_ANON_KEY` | Supabase | Phase 1에서는 사용하지 않으며, 공매 테이블 접근이 **차단되어야 한다**(§6.6) |
| 배치 지오코딩 | REST API 키 | 카카오 앱 1288016 (무료 쿼터 보유 앱) | **서버 전용 — 절대 노출 금지** |
| 지오코딩 폴백 | 인증키 | VWorld | 서버 전용 |

- 카카오 무료 쿼터는 **앱 단위로 귀속**되므로 앱 1288016의 REST API 키를 재사용한다. 여러 프로젝트가 공유해도 제한되지 않으나, 합산 소진 위험이 있어 F3.5 호출량 로깅이 필수다.
- 카카오 REST API 키는 서버 호출용이므로 도메인 등록이 불필요하다.
- 지도 SDK에 REST API 키를 사용하면 키 종류 불일치로 오류가 발생한다 (Phase 2 유의사항).

### 6.4 온비드 API 호출 규약 (활용가이드 확정분)

활용가이드 4종(물건목록·물건상세·공고목록·코드/주소) 확보로 확정된 사항이다.

> **활용가이드와 실제 응답이 여러 곳에서 다르다.** 실호출 검증 결과는 `docs/API_FINDINGS.md`에
> 정리되어 있으며, 충돌 시 **API_FINDINGS가 가이드보다 우선**한다.

**서비스별 엔드포인트·오퍼레이션**

| 서비스 | Base URL (`https://apis.data.go.kr/B010003/` +) | 오퍼레이션 | 추가 필수 파라미터 |
|---|---|---|---|
| 부동산 물건목록 (`SVC-API-001` v2.0) | `OnbidRlstListSrvc2` | **`getRlstCltrList2`** ✅실측확정 | **`prptDivCd`**, **`pvctTrgtYn`** |
| 부동산 물건상세 (v2.0) | `OnbidRlstDtlSrvc2` | `getRlstDtlInf` (Call Back `getRlstDtlInf2`) | **`cltrMngNo`** (`pbctCdtnNo` 옵션) |
| 공고목록 (`SVC-API-015` v2.0) | `OnbidPbancListSrvc2` | `getPbancList` (Call Back `getPbancList2`) | `cltrTypeCd` `prptDivCd` `opbdDtStart` `opbdDtEnd` |
| 코드 및 주소 (`SVC-API-025` v1.0) | `OnbidCodeSrvc` | `getOnbidUsgCodeInfo` / `getOnbidDtlAddrInfo` | 없음 |
| 물건상세 **입찰정보** | **`OnbidCltrBidDtlSrvc2`** ✅실측확정 | **`getCltrBidInf2`** | **`cltrMngNo`**, **`pbctCdtnNo`** |

공통 필수: `serviceKey` `pageNo` `numOfRows` `resultType`

> **`pvctTrgtYn`(수의계약가능여부)이 물건목록의 필수 파라미터다.** 단일 값(Y/N)만 받으므로
> **전량 수집에는 Y·N 2회 순회가 필요하다.** `prptDivCd`는 쉼표 복수 지정이 되어 1회로 끝난다.

> **가이드 본문의 오퍼레이션명(`getRlstCltrList`)은 틀렸다.** 실호출 시
> `NO_OPENAPI_SERVICE_ERROR`(코드 12)가 반환된다. **서비스명·오퍼레이션명 양쪽에 접미사 `2`가 붙는다.**

### 6.4.1 오류 응답 봉투가 두 종류다 (실측)

문서화된 `{"header":{"resultCode":...}}` 외에, **포털 게이트웨이 단계의 오류는 전혀 다른 봉투로 온다.**

```json
{"OpenAPI_ServiceResponse": {"cmmMsgHeader": {
  "errMsg": "NO_OPENAPI_SERVICE_ERROR", "returnReasonCode": "12",
  "returnAuthMsg": "해당 오픈API 서비스가 없거나 폐기됨" }}}
```

**세 번째 형식도 있다.** 입찰정보 서비스에서 데이터가 없을 때 관측된다.

```json
{"result": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}}
```

- 파서는 **세 봉투를 모두 인식**해야 한다: `header` · `OpenAPI_ServiceResponse.cmmMsgHeader` · `result`
- 게이트웨이 봉투는 **HTTP 400**과 함께 오기도 한다 (정상 응답은 200 + `resultCode`)
- `returnReasonCode`와 `resultCode`는 동일 체계로 취급한다
- 잘못된 오퍼레이션명·서비스명·키 문제는 주로 게이트웨이 봉투로 나타난다

**공통 규약**

| 항목 | 확정 내용 |
|---|---|
| Base URL | `https://apis.data.go.kr/B010003/{서비스영문명}` — **HTTPS**, 기관코드 `B010003` |
| 오퍼레이션 | 공고목록: `getPbancList`. 단, 가이드의 Call Back URL은 `[서비스URL]/getPbancList2`로 표기됨 — **실호출로 확인 필요** |
| 인증 | `serviceKey` — 포털의 **Encoding/Decoding은 같은 키의 두 표현**이다. 어느 쪽을 받아도 `urllib.parse.unquote()`로 정규화한 뒤 HTTP 클라이언트의 `params=`로 넘긴다(클라이언트가 다시 인코딩). 서비스키에는 `%`가 포함되지 않으므로 `unquote`는 멱등하고 안전하다. **URL 문자열에 수동으로 붙이지 않는다** |
| 응답 형식 | `resultType=json` 지원 (XML/JSON 모두) |
| 페이징 | `pageNo`, `numOfRows` **필수**. 응답에 **`totalCount` 제공**. **`numOfRows=5000`까지 정상 동작 확인**(상한 미발견) — 서울 전량이 3~4회 호출로 끝난다 |
| 응답 구조 | `response.header{resultCode,resultMsg}` + `response.body{items.item[], numOfRows, pageNo, totalCount}` |
| 유량 | **초당 최대 10 TPS**, 평균 응답 500ms |
| 날짜 형식 | 일자 `yyyyMMdd`(CHAR 8), 일시 `yyyyMMddHHmm`(CHAR 12) — **KST, 타임존 표기 없음** |

**배치 설계에 직결되는 제약**

- **개찰일 구간 필수는 공고목록 전용 제약이다.** 수집 본체인 **물건목록에서는 기간 파라미터가 옵션**이므로
  날짜 윈도우 순회가 필요 없다. 대신 `pvctTrgtYn` Y/N 2회 순회가 필요하다.
- 물건목록은 **`mdfcnYmdStart`/`mdfcnYmdEnd`(최종수정일 구간)** 을 지원하므로 **증분 수집이 가능하다.
  전량 배치는 주 1회, 일일 배치는 증분으로 운영해 호출량을 크게 줄인다** (F1.8).
- 10 TPS 제한이 있으므로 수집기는 **요청 간 최소 간격 또는 토큰 버킷**을 두어야 한다 (F1.4 보강).
- 공공데이터포털 공통 함정: `items.item`이 **단건일 때 배열이 아닐 수 있다.** 파서는 dict/list 양쪽을 처리해야 한다.

### 6.5 온비드 코드 체계 (활용가이드 확정분)

| 코드 | 값 |
|---|---|
| `cltrTypeCd` 물건유형 | `0001` 부동산 / `0002` 자동차 / `0003` 동산 |
| `dspsMthodCd` 처분방식 | `0001` 매각 / `0002` 임대 |
| `prptDivCd` 재산유형 | `0002` 공유재산 / `0003` 금융권담보재산 / `0004` 불용품 / `0005` 기타일반재산 / `0006` 유입재산 / `0007` 압류재산 / `0008` 수탁재산 / `0010` 국유재산 / `0011` 공공개발재산 / `0013` 파산재산 |
| `pbctStatCd` 입찰결과구분 | `0001` 입찰준비중 / `0002` 입찰진행중 / `0003` 입찰마감 / `0006` 개찰중 / `0009` 수의계약가능 / `0010` 낙찰 / `0011` 유찰 / `0012` 취소 |
| `pbancKindCd` 공고유형 | `0001` 일반 / `0002` 재공고 / `0003` 정정 / `0004` 연기 / `0005` 취소 / `0006` 긴급 |
| `bidDivCd` 입찰구분 | `0001` 인터넷 / `0002` 현장 |

> `prptDivCd`는 복수 지정 시 쉼표로 구분한다 (`0007,0005`).

> **코드표와 실제 응답의 명칭이 다르다 (실측).** 가이드는 `bidDivCd=0001`을 "인터넷"이라 하지만
> 실제 응답 `bidDivNm`은 **"전자입찰"** 이다. **표시용 명칭은 코드표가 아니라 응답의 `*Nm` 필드를 쓴다.**

### 6.6 Supabase 운영 규약

**변경 일자**: 2026-08-19 / **사유**: 적재 대상을 로컬 SQLite에서 Supabase(PostgreSQL)로 변경.
Phase 2 Web과 단일 진실원천을 공유하고, Mac·Windows 교차 작업에서 데이터가 갈라지지 않게 한다.

#### ⚠️ 공유 프로젝트의 권한 경계

이 Supabase 프로젝트는 **`krx-stock-charts`·`krx-signal-alerts`·`utube-trend-tracer`와 공유**된다.

**anon 키는 테이블이 아니라 프로젝트 단위로 발급된다.** 따라서 아무 조치 없이 테이블을 추가하면
**기존 웹앱 3개의 anon 키로 공매 데이터가 조회 가능**해진다. 테이블 접두어(`onbid_`)는 이름 구분일 뿐
권한 분리가 아니다.

이는 보안 문제인 동시에 **법적 문제**다. 공매 물건이 무제한 조회되면 §2.4의 "게시형 표시·광고 금지"
경계를 넘어 사실상 카탈로그가 된다.

**규칙**

| # | 규칙 |
|---|---|
| R1 | 모든 `onbid_*` 테이블에 **RLS를 활성화**하고 **정책을 만들지 않는다** → anon·authenticated 전면 차단 |
| R2 | 접근은 **`service_role`(RLS 우회)로만** 한다. `SUPABASE_SERVICE_KEY`·`SUPABASE_DATABASE_URL`은 서버 전용 |
| R3 | RLS 활성화에 더해 `anon`·`authenticated` 역할의 **grant를 명시적으로 revoke**한다 (이중 방어) |
| R4 | 테이블 접두어는 `onbid_`로 통일한다 (워크스페이스 관례) |
| R5 | **anon 키로 실제 SELECT가 차단되는지 실측 확인**한다 (AC12) — 설정만 믿지 않는다 |
| R6 | Phase 2에서 Web을 붙일 때 RLS 정책을 **새로 설계**한다. Phase 1의 전면 차단을 그대로 푸는 방식은 금지 |

#### 마이그레이션 관리

- DDL은 `migrations/NNN_*.sql`로 버전 관리하고 **손으로 콘솔에서 바꾸지 않는다**
- 재실행 가능하게 작성한다 (`create table if not exists`, `create index if not exists`)

---

## 7. 데이터 모델

> 온비드 API 실제 응답 필드명이 미확보 상태다(§14 D1). 아래 스키마는 계획서 v0.3 §4를 기준으로 하되, 활용가이드 확보 후 필드 매핑을 확정한다.

> **DDL은 PostgreSQL 기준**이다 (2026-08-19 SQLite에서 변경). 모든 테이블은 `onbid_` 접두어를 쓰고
> RLS를 활성화하되 정책을 만들지 않는다(§6.6 R1). `migrations/001_init.sql`로 버전 관리한다.

```sql
-- 물건 (온비드 부동산 물건목록 응답에 1:1 대응)
create table if not exists onbid_cltr (
  cltr_mng_no       text        not null,   -- 물건관리번호 (cltrMngNo)
  pbct_cdtn_no      text        not null,   -- 공매조건번호 (pbctCdtnNo)
  onbid_cltr_no     text,                   -- 온비드물건번호 (onbidCltrno)
  onbid_pbanc_no    text,                   -- 온비드공고번호
  pbct_no           text,                   -- 공매번호
  pbct_nsq          text,                   -- 회차
  pbct_sn           text,                   -- 차수 (실측 채움률 12.9%)
  cltr_nm           text,                   -- 물건명

  -- 주소·위치
  jibun_addr        text,                   -- 지번주소 전체 (zadrNm)
  road_addr         text,                   -- 도로명주소 전체 (cltrRadr)
  sd_nm             text,
  sgg_nm            text,
  emd_nm            text,                   -- 실측 결측 0%
  ltno_pnu          text,                   -- 지번PNU 19자리 (선행 0 보존을 위해 text)
  rdnm_pnu          text,
  lat               double precision,
  lng               double precision,
  geocode_status    text,                   -- ok | approx | failed
  geocode_level     text,                   -- road | jibun | trimmed | dong_center
  geocode_src       text,                   -- kakao | vworld

  -- 분류
  prpt_div_cd       text,
  prpt_div_nm       text,
  dsps_mthod_cd     text,                   -- 0001 매각만 적재
  usg_lcls_id       text,
  usg_mcls_id       text,
  usg_scls_id       text,
  usg_lcls_nm       text,
  usg_mcls_nm       text,
  usg_scls_nm       text,

  -- 금액·비율
  appraisal_amt     bigint,                 -- 감정평가금액 원 (수천억 대비 bigint)
  min_bid_amt       bigint,                 -- 파싱 실패·"비공개" 시 null (F4.7)
  min_bid_amt_text  text,                   -- 원문 (lowstBidPrcIndctCont)
  min_bid_rate      numeric(8,5),           -- 자체 계산 min_bid_amt/appraisal_amt.
                                            -- 1.0 을 넘을 수 있다 (실측 최대 1.502)

  -- 진행 상태
  fail_cnt          integer,                -- 유찰횟수 (usbdNft)
  bid_prgn_cnt      integer,
  bid_start         timestamptz,            -- 2999 sentinel이면 null
  bid_end           timestamptz,
  bid_date_tbd      boolean     default false,  -- 일정 미정 (실측 0.3%)
  pbct_stat_cd      text,
  pbct_stat_nm      text,
  cltr_stat_mng_cd  text,                   -- 가이드 미기재 필드 (D16)
  status            text,                   -- 파생: 진행|마감|낙찰|유찰|취소|종료추정

  -- 물건 속성
  land_sqms         numeric(18,4),
  bld_sqms          numeric(18,4),
  share_yn          boolean,                -- 지분물건여부 (alcYn)
  batch_bid_yn      boolean,
  pvct_trgt_yn      boolean,
  crtn_yn           boolean,
  org_nm            text,
  rqst_org_nm       text,
  thumb_url         text,

  -- 메타
  onbid_url         text,                   -- 응답 행에서 조립 (F1.15). 식별자 4개 필수
  mdfcn_dt          timestamptz,            -- 온비드 최종수정일시 — 증분 수집 기준
  raw_payload       jsonb,                  -- 수집 원본 (F1.3). jsonb라 질의 가능
  first_seen_at     timestamptz not null default now(),
  last_seen_at      timestamptz,
  synced_at         timestamptz,
  bid_round_synced_at timestamptz,          -- 입찰정보 마지막 **시도** 시각 (F1.16).
                                            -- 성공·이력없음·실패를 가리지 않고 갱신한다

  primary key (cltr_mng_no, pbct_cdtn_no)
);

create index if not exists idx_onbid_cltr_geo    on onbid_cltr (lat, lng);
create index if not exists idx_onbid_cltr_rate   on onbid_cltr (min_bid_rate);
create index if not exists idx_onbid_cltr_bidend on onbid_cltr (bid_end);
create index if not exists idx_onbid_cltr_status on onbid_cltr (status);
create index if not exists idx_onbid_cltr_region on onbid_cltr (sgg_nm, usg_mcls_id);
create index if not exists idx_onbid_cltr_prpt   on onbid_cltr (prpt_div_cd);
create index if not exists idx_onbid_cltr_pnu    on onbid_cltr (ltno_pnu);
create index if not exists idx_onbid_cltr_mdfcn  on onbid_cltr (mdfcn_dt);

-- 회차별 입찰 이력 (getCltrBidInf2 의 prcnBidClgList / cseqBidInfClgList)
-- 유찰 물건은 첫 배치부터 전체 이력이 확보된다 (F1.7)
-- ⚠️ pbct_nsq 는 유일하지 않다. 한 물건의 이력에 **여러 공매 사건이 섞여** 있어
--    회차 번호가 반복된다 (실측: 25건 표본에서 중복 184건, 한 물건 최대 25회 중복).
--    이력은 정렬돼 있지도 않으므로 조회 시 opbd_dt 로 정렬한다.
create table if not exists onbid_cltr_bid_round (
  cltr_mng_no    text not null,
  pbct_cdtn_no   text not null,
  opbd_dt        timestamptz not null,   -- 개찰일시 (cltrOpbdDt) — 사건을 가르는 실질 키
  pbct_nsq       text not null,          -- 회차. 사건마다 1부터 다시 매겨진다
  pbct_sn        text,                   -- 차수
  result_nm      text,                   -- 유찰 | 낙찰 | 취소 (pbctStatNm)
  min_bid_amt    bigint,                 -- 그 회차 최저입찰가
  min_bid_amt_text text,                 -- 원문 (비공개 등)
  winning_amt    bigint,                 -- 낙찰가 (scfbAmt). 낙찰 회차에 실제로 채워진다
  synced_at      timestamptz not null default now(),
  primary key (cltr_mng_no, pbct_cdtn_no, opbd_dt, pbct_nsq)
);
create index if not exists idx_onbid_round_opbd on onbid_cltr_bid_round (opbd_dt);
create index if not exists idx_onbid_round_win  on onbid_cltr_bid_round (winning_amt)
  where winning_amt is not null;

-- 지오코딩 캐시 (원격 보관 — Mac·Windows 두 환경이 공유)
create table if not exists onbid_geocode_cache (
  addr       text primary key,
  lat        double precision,
  lng        double precision,
  src        text,
  level      text,
  cached_at  timestamptz not null default now()
);

-- 변경 이력 (F4.4)
create table if not exists onbid_cltr_history (
  id            bigserial primary key,
  cltr_mng_no   text not null,
  pbct_cdtn_no  text not null,
  field         text not null,          -- min_bid_amt | fail_cnt | status
  old_value     text,
  new_value     text,
  changed_at    timestamptz not null default now()
);
create index if not exists idx_onbid_hist on onbid_cltr_history (cltr_mng_no, pbct_cdtn_no, changed_at);

-- 용도 코드 트리 (getOnbidUsgCodeInfo 순회 결과, 대/중/소 3단)
create table if not exists onbid_usg_code (
  ctgr_id     text primary key,
  ctgr_nm     text,
  up_ctgr_id  text,
  up_ctgr_nm  text,
  depth       smallint,
  synced_at   timestamptz not null default now()
);

-- 주소 조합 (getOnbidDtlAddrInfo) — 행정구역 코드표가 아니라 **물건이 존재하는 주소**다.
-- 응답의 dtlAddr(번지·건물 상세)는 버리고 조합만 남긴다. 실측 서울 247조합.
create table if not exists onbid_addr_map (
  sd_nm      text not null,
  sgg_nm     text not null,
  emd_nm     text not null,
  synced_at  timestamptz not null default now(),
  primary key (sd_nm, sgg_nm, emd_nm)
);

-- 배치 실행 메타 (F4.6)
create table if not exists onbid_batch_run (
  run_id          bigserial primary key,
  mode            text,                 -- full | delta | rounds | codes
  started_at      timestamptz not null default now(),
  finished_at     timestamptz,
  status          text,                 -- ok | partial | failed
  collected       integer,
  upserted        integer,
  tombstoned      integer,
  geocode_ok      integer,
  geocode_approx  integer,
  geocode_failed  integer,
  resume_token    text,                 -- 429 중단 시 재개 지점 (N2.2)
  note            text
);

-- ── 권한 차단 (§6.6 R1·R3). 정책을 만들지 않아 anon·authenticated는 전면 차단된다 ──
alter table onbid_cltr           enable row level security;
alter table onbid_cltr_bid_round enable row level security;
alter table onbid_geocode_cache  enable row level security;
alter table onbid_cltr_history   enable row level security;
alter table onbid_usg_code       enable row level security;
alter table onbid_addr_map       enable row level security;
alter table onbid_batch_run      enable row level security;

revoke all on onbid_cltr, onbid_cltr_bid_round, onbid_geocode_cache,
              onbid_cltr_history, onbid_usg_code, onbid_addr_map, onbid_batch_run
       from anon, authenticated;
```

### 7.1 값 규약

| 항목 | 규약 |
|---|---|
| 금액 | 원 단위 정수. 문자열·만원 단위 금지 |
| 날짜·시각 | DB는 `timestamptz`로 저장(UTC 보관). 온비드 응답은 타임존 표기가 없으므로 **KST로 간주해 파싱**한다. MCP 응답은 ISO8601 + `+09:00`으로 직렬화한다 |
| 입찰일시 sentinel | **연도가 2900 이상이면 "일정 미정"** 으로 본다 (실측 `2999`, 18건·0.26%). 그대로 저장하면 정렬·필터가 오염되므로 `bid_start`/`bid_end`를 null로 두고 `bid_date_tbd=true` 로 표시한다. **파싱 실패와 미정을 구분한다** — 전자는 값이 이상한 것, 후자는 아직 정해지지 않은 것 |
| 일시 형식 | 필드마다 다르다 — `yyyyMMddHHmm`(입찰일시·개찰일시) · `yyyyMMddHHmmss`(수정일시) · `yyyy/MM/dd`(배분요구종기). **타임존 표기가 없어 KST로 간주**하지 않으면 9시간이 어긋난다 |
| 표시 명칭 | 코드→명칭 변환은 **응답의 `*Nm` 필드를 그대로 쓴다.** 가이드 코드표와 실제 명칭이 다르다 (§6.5) |
| `min_bid_rate` | **자체 계산** `min_bid_amt / appraisal_amt`. **1.0 을 넘을 수 있다** — 실측 6,910건 중 676건(9.8%)이 100% 초과이며 최대 1.502다. 상한을 두거나 1.0 으로 클램프하지 않는다. 산출 불가 시 null (실측 계산 가능률 95.0%) |
| `min_bid_amt` | 숫자 파싱에 성공한 경우만. "비공개" 등 비수치 표기는 null + `min_bid_amt_text` 보존 (F4.7) |
| PNU | `ltno_pnu` 19자리 **text**. 앞 10자리 = 법정동코드. 숫자형으로 저장하지 않는다 (선행 0 소실) |
| Y/N 필드 | 온비드의 `"Y"`/`"N"` 문자열은 **boolean으로 변환**해 저장한다 (`share_yn`·`batch_bid_yn`·`pvct_trgt_yn`·`crtn_yn`) |
| `raw_payload` | **jsonb**. 문자열로 저장하지 않는다 — 필드 누락·이상값 조사 시 SQL로 직접 질의할 수 있어야 한다 |
| 결측 | 빈 문자열 대신 null |
| `pbct_stat_cd` | 온비드 원본 코드를 **그대로 보존**한다 (파생 규칙이 바뀌어도 재계산 가능) |
| 회차 키 | `onbid_cltr_bid_round` 는 `(cltr_mng_no, pbct_cdtn_no, opbd_dt, pbct_nsq)` 가 키다. 개찰일시나 회차가 없는 행은 **적재하지 않는다** (변경 #62) |
| `winning_amt` | 낙찰 회차에만 채워진다. 유찰·취소 회차는 **null** — 0으로 채우면 낙찰가율 표본이 오염된다 (§8.3) |
| 코드표 | 용도·주소 코드표는 **지우고 다시 넣지 않는다**. 갱신 중 조회가 빈 표를 보게 된다 — upsert 로 덮어쓴다 |
| `status` | `pbct_stat_cd`에서 파생. `0001·0002·0009`→`진행`, `0003·0006`→`마감`, `0010`→`낙찰`, `0011`→`유찰`, `0012`→`취소`, 응답에서 소실→`종료추정`. **회차 이력에는 코드가 없어 이름(`pbctStatNm`)으로 파생**한다 — 코드가 있으면 코드를 우선한다(이름은 표기가 바뀔 수 있다) |

---

## 8. MCP 툴 계약

### 8.1 `search_auction_items`

조건에 맞는 공매 물건 목록을 조회한다.

**입력**

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `region` | string | — | 시군구명(`"강남구"`) 또는 코드. 값 모양으로 자동 판별 (F6.6) |
| `usage` | string | — | 용도명(`"아파트"`) 또는 코드 |
| `prpt_div` | string | — | 재산유형명(`"압류재산"`) 또는 코드(`"0007"`). 복수는 쉼표 구분 |
| `pvct_trgt` | enum | — | `입찰`(수의계약 불가) / `수의계약`(가능) / `전체`(기본). **성격이 다른 두 모집단을 구분한다** |
| `min_price` / `max_price` | integer | — | 최저입찰가 구간 (원) |
| `min_rate` / `max_rate` | number | — | 최저가율 구간. **상한은 1.0 이 아니다** (실측 최대 1.502) |
| `min_fail_cnt` | integer | — | 최소 유찰횟수 |
| `bid_end_after` / `bid_end_before` | string(date) | — | 입찰 마감일 구간 |
| `status` | enum | — | `진행`(기본) / `종료추정` / `전체` |
| `sort` | enum | — | 화이트리스트: `bid_end_asc`(기본) / `bid_end_desc` / `min_bid_amt_asc` / `min_bid_amt_desc` / `fail_cnt_desc` |
| `limit` | integer | — | 기본 20, 최대 50 |
| `cursor` | string | — | 다음 페이지 커서 |

**출력**

```json
{
  "items": [{
    "cltr_no": "...", "cltr_nm": "...", "clean_addr": "...",
    "usage_nm": "...", "appraisal_amt": 0, "min_bid_amt": 0,
    "min_bid_rate": 0.0, "fail_cnt": 0, "bid_end": "...", "prpt_div_nm": "...",
    "ltno_pnu": "...",
    "lat": 0.0, "lng": 0.0, "geocode_status": "ok"
  }],
  "total_count": 0,
  "next_cursor": null,
  "query_echo": { },
  "meta": { }
}
```

**`pvct_trgt`를 노출하는 이유 (실측 근거)**

수의계약가능(`pvctTrgtYn=Y`) 물건은 **전량이 유찰 경험자**이며(1,157/1,157), 유찰 10회 이상 574건 중
**389건(68%)이 이 그룹**이다. 즉 "유찰이 반복돼 저렴해진 물건"을 찾으면 상당수가 이미 입찰이 아니라
**수의계약 대상**이다. 취득 방법이 다른 두 모집단이므로 구분하지 못하면 사용자가 잘못된 판단을 한다.

**설계 결정**

- **페이지네이션은 opaque cursor**를 사용한다. tombstone(F4.2)으로 배치 중 행 상태가 바뀌므로 offset은 중복·누락을 유발한다.
- **`geocode_status='failed'` 물건도 결과에 포함한다.** 좌표가 없어도 물건 정보는 유효하며, LLM이 "지도에 표시되지 않는 이유"를 설명할 수 있어야 한다. 좌표 필드는 null로 반환한다.
- **`bbox`는 노출하지 않는다.** LLM이 위경도 사각형을 구성할 일이 없다. bbox는 F5.4(Web 전용)에만 존재한다.
- **기본 정렬은 마감일 오름차순**이다. 최저가율 오름차순을 기본으로 두면 서비스가 순위를 매기는 것이 되어 §2.4를 위배한다. 사용자가 명시적으로 지정한 정렬은 랭킹이 아니다.

### 8.2 `get_auction_detail`

물건관리번호로 단건 상세를 조회한다.

**입력**: `cltr_mng_no` (string, 필수) + `pbct_cdtn_no` (string, 옵션 — 생략 시 최신 회차)

**출력**: `cltr` 테이블 전체 필드(`raw_payload` 제외) + `onbid_url` + `meta`. 존재하지 않으면 `not_found` 오류.

### 8.3 `get_auction_stats`

현재 적재된 물건의 분포를 집계한다.

**입력**

| 파라미터 | 타입 | 설명 |
|---|---|---|
| `group_by` | enum(필수) | `min_bid_rate_bucket` / `fail_cnt` / `usage` / `region` / `prpt_div` / `pvct_trgt` |
| `region` / `usage` / `prpt_div` / `pvct_trgt` | string | 필터 (§8.1과 동일 규약) |
| `status` | enum | 기본 `진행` |

**출력**: `buckets[{key, label, count}]`, `n`, `query_echo`, `meta`

**설계 결정 — 낙찰가율 제외 (계획서 v0.3에서 변경)**

계획서 v0.3 §6.1은 출력에 "낙찰가율 분포"를 명시했으나, **낙찰가율은 낙찰된(=종료된) 물건에서만 산출**된다. §2.1이 진행 중 물건만 수집하므로 산출이 불가능하다.

→ **Phase 1 통계는 진행 중 물건의 스냅샷 분포(최저가율·유찰횟수·용도·지역)로 한정한다.** 낙찰가율과 시계열 추이는 `onbid_cltr_history` 와 tombstone 이 누적된 뒤 Phase 2에서 재검토한다.

**변경 일자**: 2026-08-18 / **사유**: 수집 범위와 출력 정의의 논리적 모순 해소

> `group_by` 결과는 **집계값만** 반환하며 개별 물건 식별정보를 포함하지 않는다.

**낙찰가율 (`win_rate_bucket`) — 조건부 부활**

회차 이력의 낙찰 회차에 `scfbAmt` 가 채워져 있어 낙찰가율 산출이 가능하다.
**분모는 물건의 `appraisal_amt`(감정가)를 쓴다.** 회차 이력의 최저입찰가가 감정가의 정확한
분수(50%·45%·…·1%)로 떨어지는 것을 실측으로 확인했으므로(845행 중 90%가 0.25%p 배수),
**감정가는 이력 전 구간에서 일정**하다고 본다.

두 지표를 **분리해서** 제공한다 — 혼동하면 해석이 뒤집힌다.

| 지표 | 정의 | 의미 | 실측 범위 |
|---|---|---|---|
| `win_to_appraisal` | 낙찰가 ÷ **감정가** | 통상적 의미의 낙찰가율 | 3.4% ~ 71.4% |
| `win_to_min_bid` | 낙찰가 ÷ 그 회차 **최저입찰가** | 입찰 경쟁 강도 | 102% ~ 300% |

**⚠️ 모집단 편향을 반드시 함께 반환한다 (필수 구현)**

> 이 편향은 **D20 이 해소되면 걷힐 수 있다.** tombstone 된 물건에도 입찰정보 API 가 응답한다면
> 정상 낙찰·완료 건까지 표본에 넣을 수 있다. 그때 caveat 문구를 재검토한다.

우리가 보는 낙찰은 **"낙찰됐으나 계약이 무산되어 다시 공매에 나온"** 건뿐이다.
정상적으로 낙찰·계약 완료된 물건은 목록 API에 나오지 않으므로 표본에 없다.
따라서 이 분포는 **일반적인 낙찰가율이 아니다.** 응답 `meta` 에 다음을 강제한다.

```json
"caveat": "현재 재공매 중인 물건의 과거 낙찰 회차만 집계한 값입니다. 정상 낙찰되어 종료된 물건은 온비드 목록 API에 나오지 않아 표본에서 빠져 있으므로, 일반적인 낙찰가율로 해석하면 안 됩니다.",
"population": "재공매 물건의 과거 낙찰 회차",
"n": 0
```

**집계 단위는 낙찰 회차(이벤트)** 다. 한 물건이 여러 번 낙찰됐다 무산되면 여러 번 기여하며,
그 사실을 `meta` 에 물건 수와 회차 수를 함께 실어 드러낸다.

**최저가율 구간 설계 (실측 근거)**

분포가 0~150%에 걸쳐 있으므로 `min_bid_rate_bucket` 은 **10%p 단위로 나누되 100% 초과 구간을 둔다.**
`0~9%` 84건 … `60~69%` 940건(최빈) … `100%+` 676건. 중앙값 62.5%.
100% 초과를 이상치로 버리거나 100% 구간에 합치면 전체의 9.8%가 사라진다.

**재산유형 혼재 경고 (필수 구현)**

`group_by=min_bid_rate_bucket` 또는 `fail_cnt`를 **재산유형 필터 없이** 호출하면 저감 체계가 다른
유형들이 한 분포에 섞인다. 이 경우 응답 `meta`에 아래를 포함해 LLM이 단일 모집단으로 해석하지 않게 한다.

```json
"caveat": "재산유형 10종이 합산된 분포입니다. 유형별로 감정가 산정과 유찰 저감 체계가 다르므로 prpt_div로 구분해 조회하는 것이 정확합니다.",
"prpt_div_breakdown": { "압류재산": 0, "국유재산": 0 }
```

### 8.4 `get_address_geocode`

주소 문자열을 좌표로 변환한다.

**입력**: `address` (string, 필수)
**출력**: `lat`, `lng`, `bcode`(법정동코드), `level`, `src`, `matched_addr` + `meta`

**설계 결정**

- 이 툴은 공매 도메인이 아닌 주소 변환 도메인이나, Phase 1에서는 데이터 검수 편의를 위해 포함한다.
- **카카오 쿼터를 LLM 호출이 소진**하므로 서버 측 일일 상한을 둔다(F6.10).
- 공개 배포를 검토할 때 별도 서버로 분리할지 재판단한다(§13 게이트).

### 8.5 Resource

| URI | 내용 |
|---|---|
| `onbid://codes/regions` | **물건이 존재하는** 서울 시군구·읍면동 조합 (온비드는 법정동코드를 쓰지 않는다) |
| `onbid://codes/usages` | 부동산 용도 **3단 계층** 트리 (대/중/소분류) |
| `onbid://dataset/status` | 최근 배치 시각, 총 건수, 상태별 건수, 지오코딩 성공률 |

### 8.6 공통 `meta` 블록

모든 툴 응답에 포함한다.

```json
{
  "source": "온비드(한국자산관리공사) / 공공데이터포털",
  "synced_at": "2026-08-18T04:00:00+09:00",
  "is_realtime": false,
  "count": 20,
  "truncated": true,
  "notice": "정보 제공 목적입니다. 입찰 전 온비드 원문을 확인하세요."
}
```

- `is_realtime: false` — 배치 수집분임을 명시하여 실시간 오인을 방지한다
- `count` / `truncated` — LLM이 "전부 보여줬다"고 오인하지 않도록 한다
- `notice` — 툴 description에도 동일 취지를 기재한다 (F6.9)

### 8.7 `query_echo`와 오류 규약

**`query_echo`**: 실제로 적용된 조건(자동 판별·기본값 적용 후)을 되돌려준다. LLM이 "강남구로 검색했습니다"라고 사실과 다르게 말하는 것을 방지한다.

**오류 코드**

| 코드 | 상황 | LLM이 취할 행동 |
|---|---|---|
| `no_result` | 조건에 맞는 물건 없음 | 조건 완화 제안 |
| `invalid_param` | 파라미터 오류·명칭 매칭 실패 | 응답의 `candidates`로 재시도 |
| `not_found` | `cltr_no` 미존재 | 검색으로 유도 |
| `upstream_error` | 외부 API 장애 | 재시도 또는 캐시 데이터 안내 |
| `quota_exceeded` | 외부 API 쿼터 소진 | 재시도 중단 |

**온비드 `resultCode` → MCP 오류 매핑** (활용가이드 확정분)

| 온비드 | 의미 | MCP 오류 |
|---|---|---|
| `00` NORMAL_CODE | 정상 | — |
| `03` NODATA_ERROR | 데이터 없음 | `no_result` |
| `10` INVALID_REQUEST_PARAMETER_ERROR / `11` NO_MANDATORY_REQUEST_PARAMETERS_ERROR | 파라미터 오류 | `invalid_param` |
| `22` LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR | 일일 요청제한 초과 | `quota_exceeded` |
| `21` TEMPORARILY_DISABLE_THE_SERVICEKEY_ERROR / `30` SERVICE_KEY_IS_NOT_REGISTERED_ERROR / `31` DEADLINE_HAS_EXPIRED_ERROR | 키 문제 | `upstream_error` (재시도 금지, 운영자 알림) |
| `01` `02` `04` `05` `12` `99` | 서버·서비스 오류 | `upstream_error` (재시도 대상) |

> **주의 1**: 공공데이터포털은 HTTP 200에 오류 `resultCode`를 담아 반환한다. HTTP 상태코드만 보고 성공으로
> 판정하면 안 된다 (F1.4·F6.5 구현 시 필수 반영).
>
> **주의 2**: 게이트웨이 오류는 `OpenAPI_ServiceResponse.cmmMsgHeader.returnReasonCode`로 오며
> **HTTP 400을 동반한다** (§6.4.1). 두 봉투를 모두 파싱해 동일 체계로 매핑해야 한다.

**`no_result`를 빈 배열로 반환하지 않는다.** 빈 배열은 "데이터가 없다"와 "조회에 실패했다"를 구분하지 못해 LLM이 잘못된 결론을 낸다.

### 8.8 툴 금지 사항

- 물건 랭킹·추천·점수화 툴을 만들지 않는다
- "좋은 물건", "저평가", "급매" 등 평가·유인 표현을 응답에 넣지 않는다
- 계산 근거가 불명확한 파생 지표를 반환하지 않는다 (최저가율은 계산식이 명시적이므로 허용)

---

## 9. 아키텍처

### 9.1 디렉토리 구조

```
onbid-mcp/
├─ core/                 # 독립 패키지 — mcp를 import하지 않는다 (N8.2)
│  ├─ onbid/             # F1 수집 (클라이언트·파서·수집기)
│  ├─ codes/             # F1 용도·주소 코드
│  ├─ normalizer/        # F2
│  ├─ geocoder/          # F3
│  ├─ store/             # F4 적재
│  ├─ pipeline/          # F4.16 배치 오케스트레이션 — 커밋 경계를 여기서만 결정한다
│  └─ stats/             # F5.3 집계
├─ onbid_mcp/           # F6, F7 — core를 import만 함
│  ├─ server.py          # stdio 엔트리
│  ├─ tools/             # 툴 4종
│  └─ resources/         # Resource 4종
│  # ⚠️ 디렉토리명이 `mcp`이면 설치된 MCP SDK를 가려 `from mcp.server import ...`가 실패한다
├─ api/                  # F5 FastAPI (Phase 2 Web용)
├─ docs/                 # SPEC / PLAN / TASKS
├─ tests/
├─ .env                  # gitignore
└─ requirements.txt
```

Phase 2의 Web은 **별도 저장소**로 분리하고 `core`를 패키지로 공유한다. 지금 의존 방향을 단방향으로 고정해두면 분리 비용이 사실상 0이다.

### 9.2 데이터 흐름

```
[배치 — 1일 1회]
  온비드 물건목록 API (서울·부동산·진행중)
    → 주소 정제 (꼬리표 제거)
    → 지오코딩 (캐시 → 카카오 → VWorld → 동중심 근사)
    → Supabase upsert + tombstone
    → 변경 감지 → cltr_history

[조회 — MCP]
  LLM 툴 호출 → core.store 직접 쿼리 → 구조화 응답 + meta + query_echo
```

MCP 서버는 FastAPI를 거치지 않고 `core`를 직접 import한다. HTTP 홉을 넣으면 프로세스 관리와 실패 모드만 늘고 얻는 것이 없다. FastAPI와 MCP는 **동일한 store 쿼리 함수**를 공유하므로 로직 중복은 발생하지 않는다.

---

## 10. 제약 조건

| # | 제약 | 영향 |
|---|---|---|
| C1 | 온비드 API가 **좌표를 제공하지 않는다** | 자체 지오코딩·인덱스 필수. 실시간 프록시 불가 |
| C2 | 온비드 API 활용신청 **승인 대기 시간** (최대 24시간+) | M0 최우선. 목록·상세·공고·코드조회 4종 모두 신청 |
| C3 | 카카오 무료 쿼터는 **앱 단위 귀속**, 타 프로젝트와 공유 | 캐시 우선, 호출량 로깅, 429 중단·재개 |
| C4 | 카카오 로컬 API 일 100,000건 / 월 3,000,000건 | MVP 규모에서는 여유. 소진 시 429 |
| C5 | 온비드 주소 형식의 변주 | 파서 누락 가능. `raw_addr` 보존 + 실패 로그 축적 |
| C6 | 개발 환경 이중화 (macOS 주 / Windows 보조) | 경로·인코딩 의존 코드 금지, 파일명 ASCII |
| C7 | **Supabase 프로젝트를 기존 3개 프로젝트와 공유** | anon 키가 프로젝트 단위라 RLS 전면 차단이 필수 (§6.6) |
| C8 | **DB가 원격이므로 네트워크가 필수** | 오프라인 조회 불가. 장애 시 MCP 툴은 `upstream_error` 반환 |
| C9 | 무료 티어 용량·연결 수 제한 | 6천여 행은 무관하나 커넥션 재사용 필수 (N1.3) |
| C10 | **`SUPABASE_DATABASE_URL` 이 트랜잭션 풀러(pgbouncer, 6543)를 가리킨다** | prepared statement 를 지원하지 않으므로 psycopg 연결에 **`prepare_threshold=None`** 이 필수다. 없으면 반복 쿼리에서만 깨져 원인 찾기가 어렵다 |

---

## 11. 리스크

| 리스크 | 영향 | 대응 |
|---|---|---|
| API 승인 지연 | 착수 지연 | M0 최우선, 4종 동시 신청 |
| 지오코딩 실패율 과다 | 조회 품질 저하 | 폴백 6단계 + 전수 검수. `ok` 90% 이상 목표 |
| 온비드 응답 필드 불일치 | 스키마 재작업 | 활용가이드 선확보(§14 D1), `raw_payload` 보존 |
| 카카오 쿼터 공유 충돌 | 429 발생 | 캐시 우선, 중단·재개 로직 |
| 재배포 약관 제약 | 공개 불가 | Phase 1은 개인 사용이므로 무영향. 공개 시 §13 게이트 |
| API 키 유출 | 쿼터 소진·과금 | `.env` + `.gitignore`, 유출 시 즉시 재발급 |
| **anon 키로 공매 데이터 노출** | §2.4 게시형 금지 위반 소지 | RLS 전면 차단 + grant revoke + **실측 확인**(AC12) |
| **service_role 키 유출** | RLS를 우회하므로 전체 쓰기 권한 탈취 | 서버 전용, `.env` 격리, 코드·로그·fixture 금지 |
| Supabase 장애·네트워크 단절 | 조회·배치 전면 중단 | graceful degradation으로 `upstream_error`, 배치는 재개 토큰으로 복구 |
| LLM 툴 오호출 | 잘못된 조회 | `query_echo` + `invalid_param` 후보 반환 |

---

## 12. 수용 기준 (Definition of Done)

Phase 1 완료 조건:

- [ ] AC1. 배치가 **3회 연속 무중단 완주**한다
- [x] AC2. 배치 재실행 시 DB 상태가 동일하다 (멱등성) — **적재 계층 검증 완료 (2026-08-22)**. `start_run → 이력+적재 → 회차 → 코드표 → tombstone → finish_run` 를 실 Supabase에서 두 번 돌려 스냅숏 일치·행 수 불변·이력 1회만 기록을 확인 (`tests/test_store_idempotency.py`). 지오코딩·수집 포함 전 구간은 M4 이후 재확인
- [ ] AC3. 지오코딩 `ok` 비율 **90% 이상**
- [ ] AC4. `approx` / `failed` 건을 **전수 육안 확인**하고 원인을 분류한다
- [ ] AC5. Claude Code에서 툴 4종과 Resource 4종이 모두 정상 호출된다
- [ ] AC6. 모든 툴 응답에 `meta` + `query_echo`가 포함된다
- [ ] AC7. 오류 5종이 각각 구분되어 반환된다 (`no_result` 포함)
- [ ] AC8. §3.2 시나리오 S1~S7이 LLM 대화로 수행된다
- [ ] AC9. 외부 API 장애 상태에서도 기존 데이터 조회가 동작한다 (graceful degradation)
- [ ] AC10. `ruff check .` / `mypy core/ onbid_mcp/ api/ tests/ scripts/` / `pytest -q` + `pytest -m db -q` 전부 통과 *(2026-08-22 정정 — `scripts/` 가 타입 검사에서 빠져 있어 오류 37건이 누적됐던 것을 반영)*
- [ ] AC11. 키가 코드·커밋·로그에 노출되지 않음을 확인한다
- [x] AC12. **`SUPABASE_ANON_KEY`로 `onbid_*` 테이블 SELECT가 차단됨을 실측 확인한다** (§6.6 R5) — **7종 전부 HTTP 401, service_role 만 200 (2026-08-22)**
- [x] AC13. 마이그레이션이 재실행 가능함을 확인한다 (동일 DDL 2회 적용 시 오류 없음) — **확인 완료 (2026-08-22)**

---

## 13. 공개 배포 게이트 (참고)

**Phase 1은 개인 사용으로 한정하며, 공개 배포는 본 SPEC의 범위가 아니다.** 향후 공개를 검토할 경우 계획서 v0.3 §10의 기술·법적 게이트를 먼저 충족해야 한다. 특히 아래 두 건은 **사전 확인이 필요한 미해소 사항**이다.

- 온비드 데이터의 **재배포 조건** — 공공데이터포털 해당 API 이용허락범위(저작권 표시, 상업적 이용 가부, 재배포 제한) 개별 확인
- 카카오 **배치 지오코딩 결과의 DB 저장·재사용**이 약관상 허용되는지 데브톡 문의

---

## 14. 미확정 항목

**활용가이드 4종 확보(2026-08-18)로 D1~D4·D6·D7·D10이 해소되었다.**

| # | 항목 | 상태 |
|---|---|---|
| ~~D1~~ | 응답 필드명 | **해소** — 물건목록·물건상세·코드/주소 필드 확정, §7에 반영 |
| ~~D2~~ | 지역코드 체계 | **해소** — 온비드는 **법정동코드를 쓰지 않는다.** 시도/시군구/읍면동 **문자열**로 식별하며, 법정동코드는 `ltnoPnu` 앞 10자리에서 파생 |
| ~~D3~~ | 용도코드 계층 | **해소** — 대/중/소 3단. `getOnbidUsgCodeInfo`를 `upCtgrId`로 재귀 순회 |
| ~~D4~~ | 총건수 제공 | **해소** — `totalCount` 제공 |
| ~~D6~~ | 읍면동 중심좌표 테이블 | **해소(불필요)** — 온비드가 읍면동 3필드를 분리 제공하고 PNU까지 주므로 자체 테이블이 필요 없다 |
| ~~D7~~ | 감정가 제공 여부 | **해소** — `apslEvlAmt`가 **물건목록 응답에 단일 값으로 포함**된다. 물건상세 없이도 최저가율 산출 가능 |
| ~~D9~~ | 재산유형 범위 | **해소** — 전 유형 + 매각 필터 (사용자 확정) |
| ~~D10~~ | 감정평가 복수 시 분모 | **해소** — 목록·상세 모두 `apslEvlAmt` 단일 값. 상세의 `apslEvlClgList`는 감정평가**서 첨부** 목록이라 별개 |

**M1 스모크 실측(2026-08-19)으로 D11~D15가 해소되었다.**

| # | 항목 | 실측 결과 |
|---|---|---|
| ~~D11~~ | 오퍼레이션명 | **해소** — `OnbidRlstListSrvc2/getRlstCltrList2`. **서비스명·오퍼레이션명 양쪽에 `2`** |
| ~~D12~~ | 비공개 표기 문자열 | **해소** — 정확히 `"비공개"`. 빈도는 6,161건 중 **1건**으로 극히 드묾 |
| ~~D13~~ | 최저가율 단위 | **해소(부정적)** — `apslPrcCtrsLowstBidRto`·`frstCtrsLowstBidPrcRto`·`feeRate` **채움률 0%**. 단위 문제가 아니라 **필드가 오지 않는다.** 자체 계산으로 전환 (F4.5, F4.9) |
| ~~D14~~ | 목록의 상태 범위 | **해소** — 진행 계열만 반환: 입찰준비중 77.3% · 수의계약가능 18.8% · 입찰마감 2.0% · 입찰진행중 1.9%. **유찰(0011)·낙찰(0010)·취소(0012)는 나오지 않는다** → §2.2 tombstone 유지 확정, §8.3 낙찰가율 제외 확정 |
| ~~D15~~ | `numOfRows` 최대값 | **해소** — `5000`까지 정상, 상한 미발견. **서울 전량이 3~4회 호출로 끝나 트래픽 우려가 사라졌다** |
| ~~D8~~ | 물건상세 입찰정보 회차별 이력 | **해소 (2026-08-19, 활용가이드 없이 실호출로)** — `OnbidCltrBidDtlSrvc2/getCltrBidInf2`. `prcnBidClgList`가 **회차별 이력 전체**를 제공. 대상 산정은 이후 실측으로 갱신됨 → **F1.11 참조**(유찰 0회·수의계약 제외 후 1,100여 건) |

| ~~D5~~ | ~~온비드 원문 상세 URL~~ | **해소(2026-08-22)** — 활용가이드에 없어 실호출로 확정. `.../CltrDtlController/mvmnCltrDtl.do` 에 식별자 4개(`onbidCltrno`·`onbidPbancNo`·`pbctNo`·`pbctCdtnNo`). 넷 다 목록 응답에 **채움률 100%** 라 별도 조회 없이 조립된다 |

**남은 미확정**

| # | 항목 | 해소 방법 | 차단 대상 |
|---|---|---|---|
| D16 | `cltrStatMngCd` — 활용가이드에 없는 필드의 의미 | 값 분포 관찰 또는 온비드 문의. 당장은 원본 보존만 | 없음(관찰용) |
| ~~D17~~ | ~~`scfbAmt` 가 실제로 채워지는가~~ | **해소(2026-08-22)** — `prcnBidClgList` 의 낙찰 회차에 채워져 있다(강남 표본 652회차 중 32건). **단 이것으로 편향이 해결되지는 않는다 → D20** | — |
| ~~D18~~ | ~~낙찰가율 통계 부활 여부~~ | **해소(2026-08-22) — 되살린다.** 분모는 감정가를 쓴다(이력 전 구간에서 일정함을 실측 확인). `win_to_appraisal` 과 `win_to_min_bid` 두 지표를 분리하고, **모집단 편향 caveat 을 강제**한다 (§8.3) | — |
| **D20** | **목록에서 사라진 물건도 `getCltrBidInf2` 가 응답하는가** | M3 이후 tombstone 표본으로 실측. **응답한다면 §8.3의 모집단 편향이 해소된다** — 정상 낙찰·계약 완료된 물건의 낙찰가를 얻을 수 있어 비로소 일반적인 낙찰가율을 낼 수 있다. 응답하지 않으면 편향 caveat 을 영구 유지한다 | §8.3 caveat |
| **D19** | 회차 이력의 **사건 경계를 어떻게 식별할 것인가** | `pbct_nsq` 가 사건마다 1부터 재시작해 중복된다. 개찰일시 순으로 정렬해 최저입찰가가 **상승 반전하는 지점**을 사건 경계로 볼 수 있으나 확정 규칙이 필요하다. 사건 단위 분석(사건당 유찰 횟수 등)에 필요 | M5 통계 |



---

## 부록 A. 계획서 v0.3 대비 변경 사항

| # | 변경 | 사유 | 일자 |
|---|---|---|---|
| 1 | 종료 물건을 tombstone으로 보존 (§2.2) | 변경 이력·통계·알림의 전제 확보 | 2026-08-18 |
| 2 | `get_auction_stats`에서 낙찰가율 제외 (§8.3) | 진행 중 물건만 수집하므로 산출 불가 — 논리적 모순 해소 | 2026-08-18 |
| 3 | `search_auction_items`에 `min_rate`/`max_rate` 추가 (§8.1) | 핵심 가치 가설 검증에 필수인데 v0.3 입력 목록에 누락 | 2026-08-18 |
| 4 | 코드표를 MCP Resource로 노출 (§8.5) | 툴 표면 확대 없이 코드 조회 경로 제공 | 2026-08-18 |
| 5 | `region`/`usage`가 코드·한글 모두 수용 (F6.6) | LLM 왕복 감소 | 2026-08-18 |
| 6 | `bbox`를 MCP 툴에서 제외 (§8.1) | LLM이 사용할 수 없는 파라미터. Web 전용으로 한정 | 2026-08-18 |
| 7 | `query_echo` 및 오류 코드 5종 규약 신설 (§8.7) | LLM 환각·오판 방지 | 2026-08-18 |
| 8 | `last_seen_at` / `first_seen_at` / `batch_run` / `code_map` 테이블 추가 (§7) | tombstone·재개·Resource 지원 | 2026-08-18 |
| 9 | MCP가 FastAPI를 거치지 않고 core를 직접 import (§9.2) | 불필요한 HTTP 홉 제거 | 2026-08-18 |
| 10 | 계약 우선(contract-first) 원칙 도입 (§1.5②) | M5.5에서 스키마 역행 수정 방지 | 2026-08-18 |
| 11 | 온비드 API를 **차세대 계열로 통일** (§6.2) | 차세대에 부동산 물건목록 존재 확인. 계열 혼용 불필요 | 2026-08-18 |
| 12 | F1.7(회차별 입찰정보) 신설 | 유찰 이력을 API에서 직접 취득하면 자체 diff 누적의 초기 공백이 사라짐 | 2026-08-18 |
| 13 | 미확정 항목 D7·D8 추가 (§14) | 감정가 제공 여부가 핵심 지표의 전제 | 2026-08-18 |
| 14 | **처분방식(매각만)·재산유형 축을 §2.1에 추가** | 온비드에 임대 물건이 섞여 있어 최저가율 가설이 오염됨 | 2026-08-18 |
| 15 | §6.4 호출 규약·§6.5 코드 체계 신설 | 공고목록 활용가이드로 확정 (HTTPS, 10 TPS, 개찰일 구간 필수, totalCount 제공) | 2026-08-18 |
| 16 | `status`를 온비드 `pbctStatCd` 8종에서 파생하도록 재정의, 원본 코드 보존 (§7·§7.1) | 자체 4종 분류가 실제 코드 체계와 불일치 | 2026-08-18 |
| 17 | 온비드 `resultCode` → MCP 오류 매핑표 추가 (§8.7) | HTTP 200에 오류를 담아 반환하므로 명시 필요 | 2026-08-18 |
| 18 | 재산유형 **전 유형 수집** 확정, 임대는 처분방식 축으로 제외 (§2.1) | 사용자 결정 | 2026-08-18 |
| 19 | `prpt_div` 필터·집계 축 및 혼재 경고 신설 (§8.1·§8.3) | 유형별 저감 체계 차이로 분포 통계가 오독될 수 있음 | 2026-08-18 |
| 20 | N1.2 배치 목표 30분 → 60분 | 수집 범위 확대·10 TPS·증분 순회 반영 | 2026-08-18 |
| 21 | **PK를 `(cltr_mng_no, pbct_cdtn_no)` 복합키로 변경** (§7) | 온비드 상세·입찰정보 조회가 두 값을 함께 요구 | 2026-08-18 |
| 22 | **PNU(`ltnoPnu`/`rdnmPnu`) 직접 저장** (F3.0) | 온비드가 PNU를 제공 — 계획서 Phase 2 "주소→PNU 변환"이 불필요해짐 | 2026-08-18 |
| 23 | F2 주소 정제를 **P0 → 보조(P1)로 격하** | 시도·시군구·읍면동·전체지번·전체도로명이 분리 제공되어 꼬리표 파싱 부담이 사라짐 | 2026-08-18 |
| 24 | 최저가율을 **온비드 제공값(`apslPrcCtrsLowstBidRto`) 우선**으로 변경, 자체 계산은 교차검증용 (F4.5) | API가 직접 제공 | 2026-08-18 |
| 25 | `min_bid_amt` 비수치("비공개") 대응 규약 신설 (F4.7) | `lowstBidPrcIndctCont`가 VARCHAR이며 비공개 표기가 존재 | 2026-08-18 |
| 26 | 증분 수집 모드(F1.8)·`pvctTrgtYn` 2회 순회(F1.9) 신설 | `mdfcnYmd` 필터 존재, `pvctTrgtYn`이 필수 단일값 | 2026-08-18 |
| 27 | `code_map` → `usg_code`(3단 트리) + `addr_map`(문자열)으로 분리 (§7) | 온비드에 지역코드가 없고 용도는 3단 계층 | 2026-08-18 |
| 28 | `alcYn`을 **지분물건여부**로 확정 (`share_yn`) | 앞선 판독에서 '낙찰여부'로 오독했던 것을 가이드로 정정 | 2026-08-18 |
| 29 | 오퍼레이션명 `getRlstCltrList2` 확정 (§6.4) | 실호출 검증 — 가이드 본문 표기가 오류 | 2026-08-19 |
| 30 | **최저가율을 자체 계산으로 되돌림** (F4.5·F4.9), 온비드 비율 필드 컬럼 삭제 | 실측 채움률 0% — 변경 24를 철회 | 2026-08-19 |
| 31 | 게이트웨이 오류 봉투(`OpenAPI_ServiceResponse`) 처리 규약 신설 (§6.4.1) | 문서화되지 않은 두 번째 오류 형식 발견 | 2026-08-19 |
| 32 | 입찰일시 `2999` sentinel 처리 규약 신설 (§7.1) | 실측 0.3% 관측 — 정렬·필터 오염 방지 | 2026-08-19 |
| 33 | 표시 명칭은 응답 `*Nm` 필드 사용 원칙 (§6.5·§7.1) | 코드표 "인터넷" vs 실제 "전자입찰" 불일치 | 2026-08-19 |
| 34 | F3.7 신설 — `lctnEmdNm` 결측 0%이므로 `failed`는 버그로 간주 | 실측 | 2026-08-19 |
| 35 | N1.2 배치 목표 60분 → 30분, 병목을 지오코딩으로 명시 | 수집이 3~4콜로 끝남이 실측됨 | 2026-08-19 |
| 36 | `cltr_stat_mng_cd`·`bid_date_tbd` 컬럼 추가 (§7) | 가이드 미기재 필드 발견, sentinel 플래그 필요 | 2026-08-19 |
| 37 | §2.1에 실측 규모(6,161건)·재산유형 분포 기록 | 실측 | 2026-08-19 |
| **38** | **적재 대상을 SQLite → Supabase(PostgreSQL)로 변경** (§6·§7·F4.10) | 사용자 요청. Phase 2 Web과 단일 진실원천 공유, Mac·Windows 교차 작업에서 데이터 분기 방지 | 2026-08-19 |
| 39 | §6.6 Supabase 운영 규약 신설 — 공유 프로젝트 RLS 전면 차단(R1~R6) | anon 키가 프로젝트 단위라 기존 웹앱 3개에서 공매 데이터 조회가 가능해짐. §2.4 게시형 금지와 직결 | 2026-08-19 |
| 40 | 접속 방식을 **psycopg 직접 연결**로 확정 (F4.10) | 대량 upsert. PostgREST의 1000행 상한·`ON CONFLICT` 제약 회피 | 2026-08-19 |
| 41 | 타입 현대화 — 시각 `timestamptz`, 원본 `jsonb`, Y/N `boolean`, 금액 `bigint` | PostgreSQL 전환에 따른 개선. `raw_payload`가 SQL 질의 가능해짐 | 2026-08-19 |
| 42 | 지오코딩 캐시를 **원격 보관**으로 변경 | Mac·Windows 두 환경이 캐시를 공유해 카카오 호출 중복 제거 | 2026-08-19 |
| 43 | AC12·AC13, C7~C9, N1.3·N4.4 추가 | 원격 DB·공유 프로젝트 전환에 따른 검증·제약 | 2026-08-19 |
| 44 | **F1.7 재정의 + F1.11 신설 — 입찰정보는 온디맨드·부분 배치** | 포털 상세기능 화면에서 **일일 트래픽 1,000건 · 물건당 1회 호출** 확인. 전량 6,161건 취득은 7일이 걸려 불가능 | 2026-08-19 |
| 45 | F4.4를 **자체 diff 누적 + API 이력의 상호 보완**으로 조정 | `prcnBidClgList`가 회차 이력 전체를 주므로 유찰 물건은 첫 배치부터 이력 확보 | 2026-08-19 |
| 46 | **D8 해소** — 입찰정보 Base URL `OnbidCltrBidDtlSrvc2` 확정 (§6.4) | 포털 `미리보기`로 경로 확인 후 실호출 검증. 활용가이드 문서 없이 해소 | 2026-08-19 |
| 47 | `onbid_cltr_bid_round` 테이블 신설 (§7) | 회차별 유찰 이력(`prcnBidClgList`) 적재 대상 | 2026-08-19 |
| 48 | **세 번째 오류 봉투 `{"result":{...}}` 발견** (§6.4.1) | 입찰정보 서비스의 `NODATA_ERROR`가 이 형식으로 반환됨 | 2026-08-19 |
| 49 | **`pvct_trgt` 파라미터·집계 축 신설** (§8.1·§8.3) | 수의계약가능 물건은 전량 유찰 경험자이며 유찰 10회+ 물건의 68%를 차지. 취득 방법이 다른 두 모집단을 구분하지 못하면 오판을 유발 — v0.3·SPEC v1.0 모두의 누락 | 2026-08-19 |
| 50 | 미확정 D17 신설 — tombstone 물건의 낙찰가 취득 가능성 | 가능하면 §8.3에서 제외한 낙찰가율 통계가 되살아남 | 2026-08-19 |
| 51 | F2 꼬리표 파서의 필요성 재확인 (P1 유지하되 근거 보강) | 카카오 실측: `"... 123-4 외 2필지"` → **결과 0건**. 폴백 3단계가 필수임이 실증됨 | 2026-08-22 |
| 55 | **F1.3에 "응답 행을 덮어쓰지 않는다" 명시** | 구현 중 `pvctTrgtYn`을 우리 값으로 덮어쓰던 것을 발견. 온비드가 같은 필드를 응답에 담아 보내 원본이 오염되고 불일치가 숨겨짐 | 2026-08-22 |
| 56 | **F1.13·F1.14 신설** — 쿼터·키 오류는 즉시 중단 + 재개 지점 기록, 수집은 예외를 던지지 않음 | F1.4가 페이지 실패만 다뤄 쿼터 소진 시 동작이 미정의였음. 예외를 던지면 이미 받은 수천 건이 버려짐 | 2026-08-22 |
| 57 | **F7.1·§7·§8.5 정정 — 주소 API는 행정구역 코드표가 아니다** | 실측: `getOnbidDtlAddrInfo` 는 등록 물건의 상세주소 목록(전국 17,847건·서울 1,636건)이며 `dtlAddr` 가 번지+건물명이다. "시군구·읍면동 명칭 목록"이라는 전제가 틀렸음 | 2026-08-22 |
| 58 | F1.2에 용도 트리 순회 규칙 명시 (리프 `03`, 루트 필수, 실측 116노드) | 구현 중 실측으로 확정 | 2026-08-22 |
| 59 | **F1.11에 수의계약 제외 조건 추가** | 실측: `pvctTrgtYn=Y` 물건은 18/18건이 `03`. 수의계약은 입찰이 아니라 입찰정보가 없다. 대상이 2,267 → 1,100여 건으로 줄어 **3일 롤링이 하루로 단축** | 2026-08-22 |
| 60 | **D17 해소 · D18 신설** | `prcnBidClgList` 의 낙찰 회차에 `scfbAmt` 가 채워져 있음을 확인 | 2026-08-22 |
| 61 | **§8.3 낙찰가율 통계 부활** (D18 해소) — `win_to_appraisal`·`win_to_min_bid` 분리, 모집단 편향 caveat 강제 | 최저입찰가가 감정가의 정확한 분수(845행 중 90%)라 **감정가가 이력 전 구간에서 일정**함을 확인. 단 표본은 재공매 물건에 한정되므로 일반 낙찰가율이 아님 | 2026-08-22 |
| 62 | **`onbid_cltr_bid_round` PK 수정** — `pbct_nsq` 단독으로는 중복 (실측 25건 표본에서 184건 충돌). `opbd_dt` 를 PK에 포함 | 한 물건의 이력에 **여러 공매 사건이 섞여** 있어 회차 번호가 1부터 재시작한다. 기존 PK로는 적재가 깨졌을 것 | 2026-08-22 |
| 63 | D19 신설 — 회차 이력의 사건 경계 식별 규칙 | 사건 단위 분석에 필요하나 확정 규칙 미정 | 2026-08-22 |
| 64 | **D5 해소 · F1.15 신설** — 온비드 원문 링크 조립 규칙 확정 | 활용가이드에 URL 규칙이 없어 실호출로 확인. 식별자 4개 필수(하나만 빠져도 HTTP 500)이며 모두 목록 응답에 채움률 100% | 2026-08-22 |
| 65 | **D17 범위 축소 · D20 신설** | D17 을 "`scfbAmt` 가 채워지는가"로 좁혀 해소하고, **원래 목적이던 편향 제거**(사라진 물건도 응답하는가)를 D20 으로 분리. 변경 #60 에서 해소 범위를 과하게 잡았던 것을 정정 | 2026-08-22 |
| 72 | **F4.13 신설 · F4.2 보강** — tombstone 판정에 범위를 필수화 | 강남구만 수집하고 전체 범위로 판정하면 6,594건이 잘못 종료 처리됨을 실측 확인. 증분 필터는 아예 거부하도록 타입 수준에서 막음 | 2026-08-22 |
| 71 | **C10 신설 · N1.3 보강** — 트랜잭션 풀러에서 prepared statement 비활성 필수 | 마이그레이션 검증 중 발견. 테스트를 단독 실행하면 통과하고 연속 실행에서만 실패해 원인 파악이 오래 걸린다 | 2026-08-22 |
| 70 | **F6.13·F6.14 신설** | 쉼표 목록의 부분 매칭과 동명 모호성 처리를 명문화. 실측상 서울 199개 읍면동 중 `신사동` 이 두 자치구에 걸쳐 실제로 모호해진다 | 2026-08-22 |
| 69 | **F6.12 신설 · F6.7·§7.1 status 규약 보강** | 용도 중분류 검색이 확장 없이는 0건임을 실측 확인(3,506건 vs 0건). 회차 이력에 `pbctStatCd` 가 없어 이름 경로가 필수임을 확인 | 2026-08-22 |
| 68 | **F4.12 신설 · F4.7·§7.1 일시 규약 보강** | 금액 음수·소수 거부를 명문화. 일시 sentinel 판정을 `2999` 문자열 매칭에서 **연도 임계값(2900)** 으로 바꿔 `9999` 같은 다른 표기에도 대응. 파싱 실패와 미정을 구분하도록 규정 | 2026-08-22 |
| 67 | **F2.2 재정의 · F2.8·F2.9 신설** | 카카오에 꼬리표 유형별로 실호출해 측정한 결과 **`외 N필지` 계열만 0건**을 만들고 나머지는 전부 흡수됨. 기존 F2.2는 흡수되는 것까지 지우도록 규정해 오히려 정확도를 떨어뜨릴 뻔했다. 전량 적용 시 제거 대상 30.8%, 주소 손상 0건 | 2026-08-22 |
| 66 | **F2.1 주소 우선순위 전면 교체 · F2.6 신설** | `cltrRadr`·`zadrNm` 이 **물건목록에 없다**(물건상세 전용). 변경 #23 이 물건상세 가이드를 보고 잘못 판단한 것. 대신 **PNU 에서 지번주소를 조립**하면 76%를 외부 의존 없이 정확히 얻는다(물건명 대조 일치율 96%) | 2026-08-22 |
| 53 | **`min_bid_rate` 범위를 `0.0~1.0` → 상한 없음으로 정정** (§7·§7.1·§8.1), 컬럼을 `numeric(8,5)` 로 확대 | 서울 전량 실측에서 **676건(9.8%)이 100% 초과, 최대 150.2%**. 사용자 확인 결과 정상 데이터 — 클램프하면 9.8%가 사라진다 | 2026-08-22 |
| 54 | §8.3 최저가율 구간에 **100% 초과 구간** 명시 | 위와 동일 | 2026-08-22 |
| 52 | **MCP 패키지 디렉토리 `mcp/` → `onbid_mcp/` 개명** (§9.1) | 설치된 MCP SDK와 이름이 충돌해 `from mcp.server import ...`가 `ModuleNotFoundError`로 실패. 의존성 설치 직후 실측 확인 | 2026-08-22 |
| 73 | **F4.4 보강 · F4.14 신설** — 첫 등장은 이력 아님, diff 는 적재보다 먼저 | 적재 뒤 비교하면 차이가 0이 되어 **예외 없이** 이력이 사라진다. 순서를 강제하는 단일 경로를 두는 것이 tombstone 범위(F4.13)와 같은 대응이다 | 2026-08-22 |
| 74 | **F4.6 보강 · F4.15 신설** — 배치 행은 시작 시 개설, `ok` 는 재개 지점 삭제, 재개 조회는 종료된 배치만 | 끝날 때 쓰면 죽은 배치가 흔적을 안 남긴다. 완주 후 토큰이 남으면 다음 실행이 중간부터 돈다 | 2026-08-22 |
| 75 | **§7.1 회차 키·`winning_amt`·코드표 규약 명문화** | 개찰일시 없는 회차는 PK 를 못 만든다. 유찰 회차 낙찰가를 0으로 채우면 §8.3 표본이 오염된다 | 2026-08-22 |
| 83 | **N4.5 신설 — httpx 요청 로그 차단** | 푸시 전 보안 점검에서 발견. 인증키가 쿼리 파라미터라 `INFO:httpx:HTTP Request: GET ...&serviceKey=...` 형태로 **로그에 평문 노출**된다(N4.1 위반). 첫 실적재 터미널 출력에서 실제로 확인 | 2026-08-23 |
| 82 | **첫 실적재 실측 반영** — F1.11 대상을 "1,100여 건" → **1,088건**으로 확정. API_FINDINGS 의 2,267 은 재현되지 않아 폐기 | 실 DB 6,902건 기준 재측정: 유찰 0회 4,671 · 유찰 ≥1회 2,231 · 수의계약 제외 1,088. 수의계약 그룹 1,143건은 **전원 유찰 1회 이상** | 2026-08-23 |
| 81 | **F1.16 신설 · `bid_round_synced_at` 컬럼 추가(§7) · F4.6 에 `rounds` 모드 추가** | 회차 배치의 예산 롤링을 설계하며 결정. 재개 토큰은 *집합*을 표현할 수 없어 부적합 — 마지막 **시도** 시각 오름차순이면 상태 없이 라운드로빈이 된다. 회차 배치를 `full` 로 기록하면 물건 배치의 재개 지점과 섞인다 | 2026-08-23 |
| 80 | **문서 정합성 점검 반영** — 테이블명 접두어 통일(`cltr_history`→`onbid_cltr_history` 등), AC10 검증 명령 정정, M3 에서 실측 완료된 AC12·AC13 체크 | 요구사항 본문이 실제 스키마·검증 명령과 어긋나 있었다. 사용자 요청으로 3개 문서 전수 대조 | 2026-08-22 |
| 77 | **F4.16 신설** — 배치 트랜잭션 경계 명문화 | 메타는 즉시 커밋(죽어도 흔적), 데이터는 한 트랜잭션(부분 적재 금지). 커밋 지점을 `core/pipeline` 한 곳으로 모은다 | 2026-08-22 |
| 78 | **F4.17 신설** — 불완전 수집에서 tombstone 판정 금지 | 페이지 실패·상한·중단이 있는 회차에 판정하면 **실패한 페이지의 물건이 통째로 종료 처리**된다. F4.2(증분 금지)·F4.13(범위 필수)에 이은 세 번째 잠금장치 | 2026-08-22 |
| 79 | **§9.1 디렉토리 구조를 실제와 일치**시키고 `core/pipeline` 명시 | 구조가 `collector/` 로 적혀 있었으나 실제는 `onbid/`·`codes/`. PLAN 표는 M3 산출물에 `core/pipeline` 을 포함했는데 태스크에 없어 **실적재 지점이 어느 마일스톤에도 없는 공백**이 생겼다 — 사용자 지적으로 발견 | 2026-08-22 |
| 76 | **AC2 부분 충족 표시** | 적재 계층 멱등성을 실 Supabase 에서 검증. 수집·지오코딩 포함 전 구간은 M4 이후 | 2026-08-22 |

---

## 부록 B. 용어

- **PNU**: 필지고유번호 19자리. `법정동코드(10) + 산여부(1) + 본번(4) + 부번(4)` (Phase 2)
- **최저가율**: 최저입찰가 ÷ 감정가. 유찰이 누적될수록 하락
- **유찰**: 입찰자가 없어 매각되지 않음. 다음 회차에 최저입찰가가 낮아진다
- **집합건물**: 아파트·오피스텔 등. 대지권 구조로 조회 경로가 다름
- **tombstone**: 삭제 대신 종료 표시로 남겨두는 행. 이력 추적을 위해 사용
- **게시형 / 조회형**: 상시 나열(카탈로그) vs 조건 입력 후 결과 반환(콘솔). 표시·광고 판단의 실무 기준
- **MCP Resource**: 툴과 달리 LLM이 필요 시 읽는 읽기 전용 자원. 툴 호출 왕복을 소비하지 않음
