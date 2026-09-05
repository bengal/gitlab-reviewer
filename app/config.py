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
    # Comma-separated image name/prefix allowlist the podman orchestrator may
    # run; empty means "only REVIEW_IMAGE itself is allowed".
    review_image_allowlist: str = ""
    # Network for review containers (host by default so they reach GitLab,
    # the llama-server and the internet for clones).
    podman_network: str = "host"
    # Hard per-review timeout in seconds (30 minutes).
    review_timeout_seconds: int = 1800
    orchestrator: Literal["podman", "fake"] = "podman"
    # Set to disable the background scheduler entirely (tests/dev): no queue
    # pump, no nightly cron. Pumps can still be driven manually (worker.pump_once).
    disable_scheduler: bool = False


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor; call .cache_clear() after changing the env in tests."""
    return Settings()
