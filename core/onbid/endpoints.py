"""온비드 OpenAPI 엔드포인트 정의 (SPEC §6.4).

여기 값은 전부 **실호출로 검증된 것**이다 (`docs/API_FINDINGS.md` §1).
활용가이드 본문과 어긋나는 부분이 있으나 **실측이 우선**이며, 가이드를 근거로 되돌리지 않는다.

주요 함정:

- 서비스명·오퍼레이션명 양쪽에 접미사 ``2`` 가 붙는다. 가이드 본문의 ``getRlstCltrList`` 로
  호출하면 ``NO_OPENAPI_SERVICE_ERROR``(12) + HTTP 400 이 반환된다.
- ``OnbidCodeSrvc`` 만 v1.0 이라 접미사가 없다.
- 입찰정보(``OnbidCltrBidDtlSrvc2``)는 서비스명 후보 18종을 실호출로 훑어도 찾지 못했고,
  포털 `미리보기` 가 생성한 URL로 확정했다.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

BASE_URL: Final = "https://apis.data.go.kr/B010003"
"""기관코드 B010003. 차세대 온비드 계열은 HTTPS를 쓴다 (레거시는 http://openapi.onbid.co.kr)."""

COMMON_REQUIRED_PARAMS: Final = frozenset({"serviceKey", "pageNo", "numOfRows", "resultType"})
"""전 서비스 공통 필수 파라미터."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """온비드 오퍼레이션 하나에 대한 호출 정의.

    Attributes:
        service: 서비스 영문명. URL 경로의 첫 세그먼트.
        operation: 오퍼레이션 영문명. URL 경로의 둘째 세그먼트.
        extra_required: 공통 파라미터 외에 이 오퍼레이션이 추가로 요구하는 파라미터.
        daily_traffic: 개발계정 일일 호출 한도. 포털에서 확인된 값만 채운다.
    """

    service: str
    operation: str
    extra_required: frozenset[str] = field(default_factory=frozenset)
    daily_traffic: int | None = None

    @property
    def url(self) -> str:
        """전체 호출 URL. 쿼리스트링은 포함하지 않는다."""
        return f"{BASE_URL}/{self.service}/{self.operation}"

    @property
    def required_params(self) -> frozenset[str]:
        """공통 필수 + 오퍼레이션 고유 필수 파라미터."""
        return COMMON_REQUIRED_PARAMS | self.extra_required

    def missing_params(self, params: Mapping[str, Any]) -> frozenset[str]:
        """주어진 파라미터에서 빠진 필수 항목을 반환한다.

        값이 ``None`` 이면 쿼리스트링에 실리지 않으므로 누락으로 간주한다.

        Args:
            params: 호출에 쓸 파라미터.

        Returns:
            빠진 필수 파라미터 이름 집합. 모두 충족되면 빈 집합.
        """
        return frozenset(
            name for name in self.required_params if params.get(name) is None
        )


ENDPOINTS: Final[Mapping[str, Endpoint]] = {
    # 수집 본체. pvctTrgtYn 이 단일값 필수라 전량 수집에 Y·N 2회 순회가 필요하다 (F1.9).
    "realestate_list": Endpoint(
        service="OnbidRlstListSrvc2",
        operation="getRlstCltrList2",
        extra_required=frozenset({"prptDivCd", "pvctTrgtYn"}),
    ),
    # 물건 상세. pbctCdtnNo 는 옵션이며 생략 시 최신 회차가 반환된다.
    "realestate_detail": Endpoint(
        service="OnbidRlstDtlSrvc2",
        operation="getRlstDtlInf2",
        extra_required=frozenset({"cltrMngNo"}),
    ),
    # 회차별 유찰 이력(prcnBidClgList) 제공. 물건당 1회 호출이라 전량 순회가 불가능하다 (F1.11).
    "bid_detail": Endpoint(
        service="OnbidCltrBidDtlSrvc2",
        operation="getCltrBidInf2",
        extra_required=frozenset({"cltrMngNo", "pbctCdtnNo"}),
        daily_traffic=1000,
    ),
    # 공고목록만 개찰일 구간이 필수다. 수집 본체인 물건목록은 기간이 옵션이다.
    "pbanc_list": Endpoint(
        service="OnbidPbancListSrvc2",
        operation="getPbancList2",
        extra_required=frozenset({"cltrTypeCd", "prptDivCd", "opbdDtStart", "opbdDtEnd"}),
    ),
    # 용도 코드 트리. upCtgrId 를 옮겨가며 재귀 순회해 대/중/소 3단을 구성한다.
    "usage_code": Endpoint(
        service="OnbidCodeSrvc",
        operation="getOnbidUsgCodeInfo",
    ),
    # 시도/시군구/읍면동 문자열 목록. 온비드는 법정동코드를 쓰지 않는다.
    "address": Endpoint(
        service="OnbidCodeSrvc",
        operation="getOnbidDtlAddrInfo",
    ),
}
"""호출 이름 → 엔드포인트 정의."""
