-- 002. 입찰정보 시도 시각 (F1.16)
--
-- 회차 배치의 예산 롤링 기준. 일일 트래픽이 1,000건인데 대상이 1,100여 건이라
-- 한 번에 다 돌 수 없다 — 오래 안 본 것부터 처리해 며칠에 걸쳐 한 바퀴를 돈다.
--
-- **성공·이력없음(03)·실패를 가리지 않고 갱신한다.** 성공만 갱신하면 이력이 없는 물건을
-- 매일 다시 호출해 예산을 태우고, 실패를 빼면 고장난 한 건이 매일 예산을 선점한다.
--
-- 재실행 가능해야 한다 (AC13).

alter table onbid_cltr
  add column if not exists bid_round_synced_at timestamptz;

comment on column onbid_cltr.bid_round_synced_at is
  '입찰정보 마지막 시도 시각. 성공·이력없음·실패 무관하게 갱신 (F1.16)';

-- 대상 선별 질의 전용 부분 인덱스.
-- 유찰 0회와 수의계약가능은 애초에 대상이 아니라 인덱스에서도 뺀다 (F1.11).
create index if not exists idx_onbid_cltr_round_due
  on onbid_cltr (bid_round_synced_at nulls first, fail_cnt desc)
  where fail_cnt >= 1 and pvct_trgt_yn is not true;
