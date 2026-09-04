"""Application settings loaded from the environment."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    All values come from environment variables (or a local .env file); see
    .env.example for the full list.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./dev.sqlite3"
    session_secret: str = "insecure-dev-session-secret"
    app_password_hash: str = ""
    secret_enc_key: str = ""
    llama_base_url: str | None = None
    anthropic_api_key: str | None = None
    review_image: str = "gitlab-mr-review/review-runner:latest"
    orchestrator: Literal["podman", "fake"] = "podman"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor; call .cache_clear() after changing the env in tests."""
    return Settings()
