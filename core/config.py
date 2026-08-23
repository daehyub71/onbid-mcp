"""설정 로딩 (SPEC §6.3).

`.env` 에서 키와 접속 문자열을 읽는다. **값을 로그나 예외 메시지에 싣지 않는다** —
서비스키가 대화·스크린샷에 노출된 전례가 있다 (N4.2).
"""

import os
import pathlib
from dataclasses import dataclass
from typing import ClassVar, Final
from urllib.parse import unquote

ROOT: Final = pathlib.Path(__file__).resolve().parents[1]
ENV_FILE: Final = ROOT / ".env"


def _read_env_file(path: pathlib.Path) -> dict[str, str]:
    """`.env` 를 파싱한다. 주석과 빈 줄은 건너뛴다."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    """실행에 필요한 외부 자원 설정.

    Attributes:
        onbid_service_key: 공공데이터포털 일반 인증키. **정규화된 원본**이다 —
            포털이 Encoding/Decoding 으로 나눠 보여주지만 같은 키이며, `unquote` 로
            맞춰 둔다 (URL 을 손으로 조립하지 않으므로 이중 인코딩 위험이 없다).
        kakao_rest_api_key: 카카오 로컬 REST 키 (지오코딩).
        vworld_api_key: VWorld 인증키. 지오코딩 폴백용이라 없을 수 있다.
        database_url: Supabase 접속 문자열.
        log_level: 로깅 레벨.
    """

    onbid_service_key: str = ""
    kakao_rest_api_key: str = ""
    vworld_api_key: str = ""
    database_url: str = ""
    log_level: str = "INFO"

    #: `repr` 에 값을 넣지 않을 필드. 나머지(로그 레벨)는 그대로 보여준다.
    SECRET_FIELDS: ClassVar[tuple[str, ...]] = (
        "onbid_service_key", "kakao_rest_api_key", "vworld_api_key", "database_url",
    )

    def __repr__(self) -> str:
        """값 대신 **설정 여부만** 보여준다.

        디버깅 중 설정 객체를 찍는 일은 흔하고, 공개 저장소의 Actions 로그는 누구나 볼 수
        있다. 기본 dataclass `repr` 은 키를 그대로 노출한다 (N4.1).
        """
        parts = [f"{name}={'설정됨' if getattr(self, name) else '없음'}"
                 for name in self.SECRET_FIELDS]
        parts.append(f"log_level={self.log_level!r}")
        return f"Settings({', '.join(parts)})"

    @classmethod
    def load(cls, path: pathlib.Path = ENV_FILE) -> "Settings":
        """환경변수를 우선하고 `.env` 로 보완한다.

        Args:
            path: `.env` 경로.

        Returns:
            설정. 없는 값은 빈 문자열로 둔다 — 필요한 시점에 각 모듈이 확인한다.
        """
        env = {**_read_env_file(path), **os.environ}
        return cls(
            onbid_service_key=unquote(env.get("ONBID_SERVICE_KEY", "").strip()),
            kakao_rest_api_key=env.get("KAKAO_REST_API_KEY", "").strip(),
            vworld_api_key=env.get("VWORLD_API_KEY", "").strip(),
            database_url=env.get("SUPABASE_DATABASE_URL", "").strip(),
            log_level=env.get("ONBID_LOG_LEVEL", "INFO").strip() or "INFO",
        )

    def require(self, field: str) -> str:
        """값이 있는지 확인하고 돌려준다.

        Args:
            field: 이 클래스의 속성명.

        Returns:
            설정값.

        Raises:
            RuntimeError: 값이 비었을 때. **값 자체는 메시지에 넣지 않는다.**
        """
        value = str(getattr(self, field, "") or "")
        if not value:
            raise RuntimeError(f".env 에 {field} 가 없다")
        return value
