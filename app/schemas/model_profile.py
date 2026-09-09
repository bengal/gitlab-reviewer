"""Validation for the model-profile create form.

The form posts plain strings; this model normalizes them and rejects bad
values with per-field error messages the router can re-render. The API key is
never read back (it is masked on read and only ever written here).
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ModelProfileForm(BaseModel):
    """Validated /settings/models form payload (raw strings in, clean values out)."""

    name: str
    provider: Literal["anthropic", "local"]
    model_id: str
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    is_default: bool = False
    extra_opencode_json: dict[str, Any] = {}
    context_window: int | None = Field(default=None, ge=1)

    @field_validator("name", "model_id")
    @classmethod
    def _required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str, info: Any) -> str:
        value = value.strip()
        if info.data.get("provider") == "local" and not value:
            raise ValueError("required for local models (the OpenAI-compatible /v1 endpoint)")
        return value

    @field_validator("extra_opencode_json", mode="before")
    @classmethod
    def _parse_json(cls, value: Any) -> Any:
        if value in (None, ""):
            return {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError("must be valid JSON") from None
        if not isinstance(value, dict):
            raise ValueError("must be a JSON object")
        return value
