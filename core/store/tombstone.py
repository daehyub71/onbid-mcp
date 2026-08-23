"""tombstone — 사라진 물건 표시 (F4.2).

온비드 물건목록은 **진행 계열만** 반환한다. 낙찰·유찰·취소로 끝난 물건은 응답에서 그냥
사라지므로, 우리가 표시해 두지 않으면 추적할 방법이 없다. 삭제하지 않고
``status='종료추정'`` 으로 남긴다 — 삭제하면 변경 이력·통계·알림이 모두 성립하지 않는다.

**두 가지를 틀리면 멀쩡한 데이터가 통째로 뒤집힌다.**

1. **증분 모드에서 판정** — 증분의 "응답에 없음" 은 "변경 없음" 이지 "사라짐" 이 아니다.
   매일 전체가 ``종료추정`` 이 된다. `TombstoneScope.from_filter` 가 증분 필터를 거부한다.
2. **수집 범위 무시** — 강남구만 수집하고 서울 전체를 판정하면 나머지 24개 구가
   종료 처리된다. 범위를 **필수 인자**로 받아 잊을 수 없게 한다.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import psycopg

from core.normalizer.status import AuctionStatus
from core.onbid.collector import ListingFilter

logger = logging.getLogger(__name__)

TABLE: Final = "onbid_cltr"


@dataclass(frozen=True, slots=True)
class TombstoneScope:
    """판정 범위. **수집한 범위와 정확히 같아야 한다.**

    Attributes:
        sd_nm: 시도명. 없으면 판정을 거부한다 — 전체를 쓸어버리는 실수를 막는다.
        sgg_nm: 시군구명. 수집을 한 구로 좁혔다면 여기도 좁혀야 한다.
    """

    sd_nm: str | None
    sgg_nm: str | None = None

    @classmethod
    def from_filter(cls, listing_filter: ListingFilter) -> "TombstoneScope":
        """수집 조건에서 판정 범위를 만든다.

        수집과 판정이 어긋나지 않게 **필터를 그대로 옮긴다.**

        Args:
            listing_filter: 이번 배치의 수집 조건.

        Returns:
            판정 범위.

        Raises:
            ValueError: 증분 모드일 때. 증분에서는 판정하면 안 된다.
        """
        if listing_filter.is_incremental:
            raise ValueError(
                "증분 모드에서는 tombstone 을 판정할 수 없다 — "
                "'응답에 없음' 이 '사라짐' 을 뜻하지 않는다 (F4.2)"
            )
        return cls(sd_nm=listing_filter.region_sd, sgg_nm=listing_filter.region_sgg)

    def where(self) -> tuple[str, dict[str, Any]]:
        """SQL 조건절과 파라미터.

        Raises:
            ValueError: 시도명이 없을 때.
        """
        if not self.sd_nm:
            raise ValueError("tombstone 판정 범위가 비었다 — 전체를 대상으로 삼지 않는다")

        clauses = ["sd_nm = %(sd_nm)s"]
        params: dict[str, Any] = {"sd_nm": self.sd_nm}
        if self.sgg_nm:
            clauses.append("sgg_nm = %(sgg_nm)s")
            params["sgg_nm"] = self.sgg_nm
        return " and ".join(clauses), params


async def mark_tombstones(
    conn: psycopg.AsyncConnection[Any],
    *,
    seen_before: datetime,
    scope: TombstoneScope,
) -> int:
    """이번 배치에서 보이지 않은 물건을 `종료추정` 으로 표시한다.

    **전량 모드에서만 호출한다** (F4.2).

    Args:
        conn: 열린 연결. 커밋은 호출자가 한다.
        seen_before: 이 시각보다 오래된 `last_seen_at` 을 "안 보였다" 로 본다.
            배치 시작 시각을 넘긴다.
        scope: 판정 범위. 수집 범위와 같아야 한다.

    Returns:
        새로 표시한 행 수. 이미 `종료추정` 인 행은 세지 않는다 — 배치 요약이 부풀지 않게.
    """
    condition, params = scope.where()
    params["seen_before"] = seen_before
    params["tombstone"] = AuctionStatus.PRESUMED_ENDED.value

    sql = f"""
        update {TABLE}
           set status = %(tombstone)s
         where {condition}
           and (last_seen_at is null or last_seen_at < %(seen_before)s)
           and status is distinct from %(tombstone)s
    """

    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        marked = cur.rowcount

    logger.info(
        "tombstone: %d건 표시 (범위 %s%s)",
        marked, scope.sd_nm, f" {scope.sgg_nm}" if scope.sgg_nm else "",
    )
    return marked
