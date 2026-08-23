"""설정 로딩 테스트 (§6.3·F8.4).

로컬에서는 `.env` 로 돌지만 **깃허브 Actions 에는 그 파일이 없다**. Secrets 는 환경변수로
주입되므로, `.env` 가 없을 때 환경변수만으로 동작하지 않으면 자동 배치가 첫 실행부터 실패한다.

값을 **로그·예외 메시지에 싣지 않는 것**도 여기서 지킨다 — 서비스키가 대화에 노출된 전례가
있고(§14 R1), 공개 저장소의 Actions 로그는 누구나 볼 수 있다.
"""

import pathlib

import pytest

from core.config import Settings

MISSING = pathlib.Path("/nonexistent/.env")


# ── CI 경로: .env 없이 환경변수만 ──────────────────────────────────────


def test_settings_load_without_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Actions 러너에는 `.env` 가 없다 — 환경변수만으로 채워져야 한다."""
    monkeypatch.setenv("ONBID_SERVICE_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DATABASE_URL", "postgresql://test")

    settings = Settings.load(MISSING)

    assert settings.onbid_service_key == "test-key"
    assert settings.database_url == "postgresql://test"


def test_settings_environment_wins_over_file(tmp_path: pathlib.Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """둘 다 있으면 환경변수가 이긴다 — 배포 환경이 로컬 파일에 좌우되면 안 된다."""
    env_file = tmp_path / ".env"
    env_file.write_text("ONBID_SERVICE_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("ONBID_SERVICE_KEY", "from-environment")

    assert Settings.load(env_file).onbid_service_key == "from-environment"


def test_settings_are_empty_without_any_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """없으면 빈 값이다 — import 시점에 터지지 않고, 쓰는 쪽에서 확인한다."""
    for name in ("ONBID_SERVICE_KEY", "KAKAO_REST_API_KEY",
                 "VWORLD_API_KEY", "SUPABASE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.load(MISSING)

    assert settings.onbid_service_key == ""
    assert settings.database_url == ""
    assert settings.log_level == "INFO"


# ── 키 표현 정규화 ─────────────────────────────────────────────────────


def test_settings_normalize_encoded_service_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """포털이 Encoding/Decoding 두 가지로 보여주지만 같은 키다 — 어느 쪽을 넣어도 된다."""
    monkeypatch.setenv("ONBID_SERVICE_KEY", "abc%2Bdef%3D%3D")

    assert Settings.load(MISSING).onbid_service_key == "abc+def=="


def test_settings_strip_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets 를 붙여 넣을 때 줄바꿈이 딸려 오면 인증이 실패한다."""
    monkeypatch.setenv("SUPABASE_DATABASE_URL", "  postgresql://test  ")

    assert Settings.load(MISSING).database_url == "postgresql://test"


# ── 값 비노출 (N4.1) ───────────────────────────────────────────────────


def test_require_does_not_leak_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """오류 메시지는 공개 로그에 남는다 — 이름만 말하고 값은 말하지 않는다."""
    monkeypatch.delenv("ONBID_SERVICE_KEY", raising=False)

    with pytest.raises(RuntimeError) as caught:
        Settings.load(MISSING).require("onbid_service_key")

    assert "onbid_service_key" in str(caught.value)


def test_require_returns_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_DATABASE_URL", "postgresql://test")

    assert Settings.load(MISSING).require("database_url") == "postgresql://test"


def test_settings_do_not_expose_values_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """디버깅 중 설정 객체를 찍는 일이 흔하다. 그 한 줄이 공개 로그에 남으면 끝이다."""
    monkeypatch.setenv("ONBID_SERVICE_KEY", "super-secret-value")

    assert "super-secret-value" not in repr(Settings.load(MISSING))
