-- ============================================================================
-- onbid-mcp 초기 스키마 (SPEC §7)
--
-- ⚠️ 이 Supabase 프로젝트는 krx-stock-charts · krx-signal-alerts ·
--    utube-trend-tracer 와 **공유**된다. anon 키는 테이블이 아니라 프로젝트 단위로
--    발급되므로, 조치 없이 테이블을 만들면 기존 웹앱 3개의 키로 공매 데이터가 읽힌다.
--    실측상 이 프로젝트의 87개 테이블 중 27개가 RLS 없이 anon 쓰기까지 열려 있다.
--
--    그래서 **테이블 생성과 권한 차단을 같은 파일에서** 끝낸다 (SPEC §6.6).
--    차단을 나중으로 미루면 그 사이 내내 노출된다.
--
-- 재실행 가능하다 (AC13). 콘솔에서 손으로 스키마를 바꾸지 않는다 (F4.11).
-- ============================================================================

-- ── 물건 ────────────────────────────────────────────────────────────────
-- 온비드 부동산 물건목록 응답에 1:1 대응한다.
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
  jibun_addr        text,                   -- 지번주소 전체 (물건상세 zadrNm)
  road_addr         text,                   -- 도로명주소 전체 (물건상세 cltrRadr)
  sd_nm             text,
  sgg_nm            text,
  emd_nm            text,                   -- 실측 결측 0%
  ltno_pnu          text,                   -- 지번PNU 19자리. 선행 0 보존을 위해 text
  rdnm_pnu          text,
  lat               double precision,
  lng               double precision,
  geocode_status    text,                   -- ok | approx | failed
  geocode_level     text,                   -- road | jibun | trimmed | dong_center
  geocode_src       text,                   -- kakao | vworld
  addr_source       text,                   -- pnu | item_name | district (F2.7)

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
  appraisal_amt     bigint,                 -- 감정평가금액(원). 수천억 대라 bigint
  min_bid_amt       bigint,                 -- 파싱 실패·"비공개" 시 null (F4.7)
  min_bid_amt_text  text,                   -- 원문 (lowstBidPrcIndctCont)
  min_bid_rate      numeric(8,5),           -- min_bid_amt / appraisal_amt.
                                            -- 1.0 을 넘는다 (실측 최대 1.502, 9.8%)

  -- 진행 상태
  fail_cnt          integer,                -- 유찰횟수 (usbdNft)
  bid_prgn_cnt      integer,                -- 입찰진행횟수
  bid_start         timestamptz,            -- 2999 sentinel 이면 null
  bid_end           timestamptz,
  bid_date_tbd      boolean     not null default false,  -- 일정 미정 (실측 0.26%)
  pbct_stat_cd      text,                   -- 원본 코드 보존 — 파생 규칙이 바뀌어도 재계산
  pbct_stat_nm      text,
  cltr_stat_mng_cd  text,                   -- 활용가이드 미기재 필드 (D16)
  status            text,                   -- 파생: 진행|마감|낙찰|유찰|취소|종료추정

  -- 물건 속성
  land_sqms         numeric(18,4),
  bld_sqms          numeric(18,4),
  share_yn          boolean,                -- 지분물건여부 (alcYn). 낙찰여부가 아니다
  batch_bid_yn      boolean,
  pvct_trgt_yn      boolean,                -- 수의계약가능여부. 취득 방법이 갈린다
  crtn_yn           boolean,
  org_nm            text,
  rqst_org_nm       text,                   -- 압류재산의 의뢰기관
  thumb_url         text,

  -- 메타
  onbid_url         text,                   -- 응답 행에서 조립 (F1.15)
  mdfcn_dt          timestamptz,            -- 온비드 최종수정일시 — 증분 수집 기준
  raw_payload       jsonb,                  -- 수집 원본 (F1.3). jsonb 라 질의 가능
  first_seen_at     timestamptz not null default now(),
  last_seen_at      timestamptz,            -- 응답에 등장한 마지막 배치 (F4.3)
  synced_at         timestamptz,

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
-- 지오코딩 대상 조회용. 좌표가 아직 없는 행만 훑는다 (M4 는 별도 패스).
create index if not exists idx_onbid_cltr_nogeo  on onbid_cltr (cltr_mng_no)
  where lat is null;

-- ── 회차별 입찰 이력 ────────────────────────────────────────────────────
-- getCltrBidInf2 의 prcnBidClgList. 유찰 물건은 첫 배치부터 전체 이력이 확보된다 (F1.7).
--
-- ⚠️ pbct_nsq 는 유일하지 않다. 한 물건의 이력에 **여러 공매 사건이 섞여** 있어
--    회차 번호가 1부터 재시작한다 (실측: 25건 표본에서 중복 184건, 한 물건 최대 25회).
--    이력은 정렬돼 있지도 않으므로 조회 시 opbd_dt 로 정렬한다.
create table if not exists onbid_cltr_bid_round (
  cltr_mng_no       text        not null,
  pbct_cdtn_no      text        not null,
  opbd_dt           timestamptz not null,   -- 개찰일시. 사건을 가르는 실질 키
  pbct_nsq          text        not null,   -- 회차. 사건마다 1부터 다시 매겨진다
  pbct_sn           text,                   -- 차수
  result_nm         text,                   -- 유찰 | 낙찰 | 취소 (pbctStatNm)
  status            text,                   -- 파생 상태
  min_bid_amt       bigint,                 -- 그 회차 최저입찰가
  min_bid_amt_text  text,                   -- 원문 (비공개 등)
  winning_amt       bigint,                 -- 낙찰가 (scfbAmt). 낙찰 회차에 채워진다
  synced_at         timestamptz not null default now(),

  primary key (cltr_mng_no, pbct_cdtn_no, opbd_dt, pbct_nsq)
);

create index if not exists idx_onbid_round_opbd on onbid_cltr_bid_round (opbd_dt);
-- 낙찰가율 집계용 (§8.3). 낙찰 회차만 부분 인덱스로 좁힌다.
create index if not exists idx_onbid_round_win  on onbid_cltr_bid_round (winning_amt)
  where winning_amt is not null;

-- ── 지오코딩 캐시 ───────────────────────────────────────────────────────
-- 원격 보관이라 Mac·Windows 두 환경이 공유한다 — 카카오 호출 중복을 줄인다.
create table if not exists onbid_geocode_cache (
  addr       text primary key,
  lat        double precision,
  lng        double precision,
  src        text,                          -- kakao | vworld
  level      text,                          -- road | jibun | trimmed | dong_center
  cached_at  timestamptz not null default now()
);

-- ── 변경 이력 (F4.4) ────────────────────────────────────────────────────
create table if not exists onbid_cltr_history (
  id            bigserial   primary key,
  cltr_mng_no   text        not null,
  pbct_cdtn_no  text        not null,
  field         text        not null,       -- min_bid_amt | fail_cnt | status
  old_value     text,
  new_value     text,
  changed_at    timestamptz not null default now()
);

create index if not exists idx_onbid_hist
  on onbid_cltr_history (cltr_mng_no, pbct_cdtn_no, changed_at);

-- ── 용도 코드 트리 ──────────────────────────────────────────────────────
-- getOnbidUsgCodeInfo 순회 결과. 대/중/소 3단, 실측 116노드.
create table if not exists onbid_usg_code (
  ctgr_id     text     primary key,
  ctgr_nm     text,
  up_ctgr_id  text,
  up_ctgr_nm  text,
  depth       smallint,                     -- 1 대분류 | 2 중분류 | 3 소분류
  synced_at   timestamptz not null default now()
);

create index if not exists idx_onbid_usg_up on onbid_usg_code (up_ctgr_id);

-- ── 주소 조합 ───────────────────────────────────────────────────────────
-- getOnbidDtlAddrInfo 는 행정구역 코드표가 아니라 **물건이 존재하는 주소**다.
-- 응답의 dtlAddr(번지·건물 상세)는 버리고 조합만 남긴다. 실측 서울 247조합.
create table if not exists onbid_addr_map (
  sd_nm      text        not null,
  sgg_nm     text        not null,
  emd_nm     text        not null,
  synced_at  timestamptz not null default now(),

  primary key (sd_nm, sgg_nm, emd_nm)
);

-- ── 배치 실행 메타 (F4.6) ───────────────────────────────────────────────
create table if not exists onbid_batch_run (
  run_id          bigserial   primary key,
  mode            text,                     -- full | delta | rounds | codes
  started_at      timestamptz not null default now(),
  finished_at     timestamptz,
  status          text,                     -- ok | partial | failed
  collected       integer,
  upserted        integer,
  tombstoned      integer,
  geocode_ok      integer,
  geocode_approx  integer,
  geocode_failed  integer,
  resume_token    text,                     -- 쿼터 중단 시 재개 지점 (N2.2)
  note            text
);

create index if not exists idx_onbid_batch_started on onbid_batch_run (started_at desc);

-- ============================================================================
-- 권한 차단 (SPEC §6.6)
--
-- 두 관문을 모두 막는다.
--   ① GRANT   — 권한 자체를 회수한다 (R3)
--   ② RLS     — 켜고 **정책을 만들지 않아** 기본 거부로 둔다 (R1)
--
-- service_role 은 RLS 를 우회하므로 배치·MCP 는 그대로 동작한다 (R2).
-- 설정만 믿지 않고 `tests/test_migrations.py` 가 카탈로그를 조회해 확인한다 (R5).
-- ============================================================================

alter table onbid_cltr           enable row level security;
alter table onbid_cltr_bid_round enable row level security;
alter table onbid_geocode_cache  enable row level security;
alter table onbid_cltr_history   enable row level security;
alter table onbid_usg_code       enable row level security;
alter table onbid_addr_map       enable row level security;
alter table onbid_batch_run      enable row level security;

revoke all on onbid_cltr           from anon, authenticated;
revoke all on onbid_cltr_bid_round from anon, authenticated;
revoke all on onbid_geocode_cache  from anon, authenticated;
revoke all on onbid_cltr_history   from anon, authenticated;
revoke all on onbid_usg_code       from anon, authenticated;
revoke all on onbid_addr_map       from anon, authenticated;
revoke all on onbid_batch_run      from anon, authenticated;

-- bigserial 이 만든 시퀀스도 함께 막는다. 테이블만 막으면 시퀀스가 남는다.
revoke all on sequence onbid_cltr_history_id_seq  from anon, authenticated;
revoke all on sequence onbid_batch_run_run_id_seq from anon, authenticated;
