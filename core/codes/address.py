"""주소 목록 수집 (F1.2·F7.1).

**주의: `getOnbidDtlAddrInfo` 는 행정구역 코드표가 아니다.**
현재 온비드에 등록된 **물건들의 실제 주소 목록**이며, 응답 한 행이 물건 한 건의
상세주소(``dtlAddr`` = ``"174 개일초등학교 급식실"``)다.

실측 규모: 전국 17,847건 · 서울 1,636건 · 서울 강남구 140건.

따라서 "서울에 어떤 읍면동이 있는가"가 아니라 **"물건이 실제로 있는 읍면동은 어디인가"**
를 알려준다. 조회 편의에는 오히려 이쪽이 유용하지만, 성격이 다르므로 혼동하면 안 된다.
여기서는 ``dtlAddr`` 를 버리고 **(시도, 시군구, 읍면동) 조합만** 남긴다.
"""

import logging
from dataclasses import dataclass
from typing import Any, Final

from core.onbid.client import OnbidClient
from core.onbid.parser import as_str, items_of, page_info

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE: Final = 1000
DEFAULT_MAX_PAGES: Final = 100


@dataclass(frozen=True, slots=True, order=True)
class AddressEntry:
    """물건이 존재하는 행정구역 조합.

    Attributes:
        sd_nm: 시도명.
        sgg_nm: 시군구명.
        emd_nm: 읍면동명.
    """

    sd_nm: str
    sgg_nm: str
    emd_nm: str


async def fetch_address_list(
    client: OnbidClient,
    *,
    sd_nm: str | None = None,
    sgg_nm: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[AddressEntry]:
    """물건이 존재하는 행정구역 조합을 수집한다.

    Args:
        client: 온비드 클라이언트.
        sd_nm: 시도명 필터.
        sgg_nm: 시군구명 필터.
        page_size: 페이지 크기.
        max_pages: 페이지 상한.

    Returns:
        중복을 제거하고 정렬한 조합 목록.
    """
    filters: dict[str, Any] = {"sdnm": sd_nm, "sggnm": sgg_nm}
    params = {k: v for k, v in filters.items() if v is not None}
    entries: set[AddressEntry] = set()
    skipped = 0

    for page in range(1, max_pages + 1):
        response = await client.call(
            "address", pageNo=page, numOfRows=page_size, **params
        )
        rows = items_of(response.payload)
        for row in rows:
            entry = _to_entry(row)
            if entry is None:
                skipped += 1
                continue
            entries.add(entry)

        if not rows or not page_info(response.payload).has_more:
            break
    else:
        logger.warning("주소 목록: 페이지 상한 %d 도달", max_pages)

    if skipped:
        logger.warning("주소 목록: 행정구역이 비어 건너뛴 행 %d개", skipped)
    logger.info("주소 목록 수집: %d개 조합 (필터 %s)", len(entries), params or "없음")
    return sorted(entries)


def _to_entry(row: dict[str, Any]) -> AddressEntry | None:
    """응답 행에서 행정구역 조합을 만든다. 하나라도 비면 ``None``.

    빈 문자열로 채워 넣으면 존재하지 않는 조합이 생겨 조회가 어긋난다.
    """
    sd_nm = as_str(row.get("sdnm"))
    sgg_nm = as_str(row.get("sggnm"))
    emd_nm = as_str(row.get("emdNm"))
    if not (sd_nm and sgg_nm and emd_nm):
        return None
    return AddressEntry(sd_nm=sd_nm, sgg_nm=sgg_nm, emd_nm=emd_nm)
