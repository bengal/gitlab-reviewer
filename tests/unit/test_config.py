"""Settings loading from the environment."""

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings

_ALL_VARS = (
    "DATABASE_URL",
    "SESSION_SECRET",
    "APP_PASSWORD_HASH",
    "SECRET_ENC_KEY",
    "LLAMA_BASE_URL",
    "ANTHROPIC_API_KEY",
    "REVIEW_IMAGE",
    "ORCHESTRATOR",
)


def _clean_env(monkeypatch) -> None:
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")


def test_defaults(monkeypatch):
    _clean_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./dev.sqlite3"
    assert settings.orchestrator == "podman"
    assert settings.anthropic_api_key is None
    assert settings.llama_base_url is None
    assert settings.app_password_hash == ""
    assert settings.secret_enc_key == ""
    assert settings.review_image == "gitlab-mr-review/review-runner:latest"


def test_env_override(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./other.sqlite3")
    monkeypatch.setenv("ORCHESTRATOR", "fake")
    monkeypatch.setenv("LLAMA_BASE_URL", "http://llama:8080")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./other.sqlite3"
    assert settings.orchestrator == "fake"
    assert settings.llama_base_url == "http://llama:8080"


def test_session_secret_required(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_invalid_orchestrator_rejected(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRATOR", "bogus")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_get_settings_cached(monkeypatch):
    _clean_env(monkeypatch)
    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()
    assert first is second
    get_settings.cache_clear()


def test_app_fixture_applies_env(app):
    assert app.state.settings.orchestrator == "fake"
    assert app.state.settings.database_url.endswith("test.sqlite3")
