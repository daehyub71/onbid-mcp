"""온비드 원문 링크 조립 (SPEC §8.2 `onbid_url`, §14 D5).

활용가이드에 상세 페이지 URL 규칙이 없어 **실호출로 확정**했다.

```
https://www.onbid.co.kr/op/cltrpbancinf/cltrdtl/CltrDtlController/mvmnCltrDtl.do
  ?onbidCltrno=…&onbidPbancNo=…&pbctNo=…&pbctCdtnNo=…
```

실측으로 확인한 것:

- **식별자 4개가 전부 필요하다.** 하나라도 빠지면 온비드가 HTTP 500 을 낸다.
- 넷 모두 물건목록 응답에 **채움률 100%** 로 들어 있어 별도 조회 없이 조립된다.
- 검색 결과에 보이는 ``cltrScrnGrpCd`` · ``cltrPrptDivCd`` 는 **없어도 동작한다.**
- 경로의 ``mvmn`` 은 동산을 뜻하는 것처럼 보이지만 **부동산도 이 경로로 열린다**
  (압류재산·기타일반재산, 수의계약 가능/불가 모두 확인).
"""

from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlencode

from core.onbid.parser import as_str

DETAIL_URL: Final = (
    "https://www.onbid.co.kr/op/cltrpbancinf/cltrdtl/CltrDtlController/mvmnCltrDtl.do"
)
"""물건 상세 페이지 엔드포인트."""

REQUIRED_ID_FIELDS: Final = ("onbidCltrno", "onbidPbancNo", "pbctNo", "pbctCdtnNo")
"""상세 페이지가 요구하는 식별자. 순서가 곧 쿼리스트링 순서다."""


def detail_url(row: Any) -> str | None:
    """물건목록·물건상세 응답 행에서 온비드 원문 링크를 만든다.

    Args:
        row: 온비드 응답 행.

    Returns:
        상세 페이지 URL. 식별자가 하나라도 없으면 ``None``.

        **깨진 링크를 만들지 않는다** — 식별자가 빠진 URL 은 HTTP 500 을 내므로,
        사용자에게 그런 링크를 주는 것보다 링크가 없는 편이 낫다.
    """
    if not isinstance(row, Mapping):
        return None

    params: list[tuple[str, str]] = []
    for field in REQUIRED_ID_FIELDS:
        value = as_str(row.get(field))
        if value is None:
            return None
        params.append((field, value))

    return f"{DETAIL_URL}?{urlencode(params)}"
