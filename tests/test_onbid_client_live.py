"""클라이언트가 실제 온비드와 통하는지 확인한다 (`pytest -m live`).

단위 테스트는 respx 로 응답을 흉내 내므로 **서비스키가 실제로 인증되는지**는 검증하지 못한다.
Encoding 키의 이중 인코딩 함정이 정확히 그런 종류라, 여기서 한 번 실호출로 확인한다.

온비드 쿼터를 소모하므로 기본 실행에서 제외된다.
"""

import os
import pathlib

import pytest

from core.onbid.client import OnbidClient

pytestmark = pytest.mark.live


@pytest.fixture
def service_key() -> str:
    """`.env` 또는 환경변수에서 서비스키를 읽는다. 없으면 테스트를 건너뛴다."""
    raw = os.environ.get("ONBID_SERVICE_KEY", "")
    if not raw:
        env = pathlib.Path(__file__).resolve().parents[1] / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("ONBID_SERVICE_KEY="):
                    raw = line.split("=", 1)[1].strip()
                    break
    if not raw:
        pytest.skip("ONBID_SERVICE_KEY 가 없어 live 테스트를 건너뛴다")
    return raw


async def test_client_authenticates_with_env_key(service_key: str) -> None:
    """`.env` 의 키가 그대로 인증에 통해야 한다.

    실패하면 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`(30)로 `OnbidAuthError` 가 난다 —
    이중 인코딩 회귀를 잡는 지점이다.
    """
    async with OnbidClient(service_key=service_key) as client:
        resp = await client.call(
            "realestate_list",
            pageNo=1, numOfRows=1, prptDivCd="0007", pvctTrgtYn="N",
            dspsMthodCd="0001", lctnSdnm="서울특별시",
        )

    assert resp.result_code == "00"
    assert resp.payload["body"]["totalCount"] > 0


async def test_client_reads_bid_history_of_a_failed_item(service_key: str) -> None:
    """유찰 물건 하나를 골라 회차별 이력이 실제로 딸려 오는지 확인한다 (F1.7)."""
    async with OnbidClient(service_key=service_key) as client:
        listing = await client.call(
            "realestate_list",
            pageNo=1, numOfRows=1, prptDivCd="0007", pvctTrgtYn="N",
            dspsMthodCd="0001", lctnSdnm="서울특별시", usbdNftStart=3,
        )
        item = listing.payload["body"]["items"]["item"]
        item = item[0] if isinstance(item, list) else item

        detail = await client.call(
            "bid_detail",
            pageNo=1, numOfRows=10,
            cltrMngNo=item["cltrMngNo"], pbctCdtnNo=item["pbctCdtnNo"],
        )

    bid_item = detail.payload["body"]["items"]["item"]
    bid_item = bid_item[0] if isinstance(bid_item, list) else bid_item
    assert bid_item["prcnBidClgList"], "유찰 3회 이상인데 회차 이력이 비어 있다"


async def test_client_rejects_bad_key_without_retrying(service_key: str) -> None:
    """잘못된 키는 재시도 대상이 아니다 — 즉시 OnbidAuthError 로 끝나야 한다."""
    from core.onbid.client import OnbidAuthError

    async with OnbidClient(service_key="not-a-real-key", max_attempts=3) as client:
        with pytest.raises(OnbidAuthError):
            await client.call(
                "realestate_list",
                pageNo=1, numOfRows=1, prptDivCd="0007", pvctTrgtYn="N",
            )
