"""LLM model profiles used to run reviews."""

from enum import StrEnum

from sqlalchemy import JSON, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Provider(StrEnum):
    anthropic = "anthropic"
    local = "local"


class ModelProfile(Base):
    """A named model configuration rendered into opencode.json per run.

    api_key is stored encrypted at rest; api_key_env names an environment
    variable to read the key from instead (key stays out of the DB).
    context_window (tokens) is rendered as the model's ``limit`` so opencode
    can auto-compact before a local server's context limit is hit.
    """

    __tablename__ = "model_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default=Provider.anthropic.value)
    model_id: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    api_key_env: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_opencode_json: Mapped[dict] = mapped_column(JSON, default=dict)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
