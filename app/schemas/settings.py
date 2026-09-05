"""Validation for the /settings form.

The form posts plain strings; this model normalizes and rejects bad values with
per-field error messages the router can re-render. An empty ``gitlab_token``
means "keep the stored token" (the token is masked on read, so the form always
posts an empty value unless the user pastes a new one).
"""

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

_NIGHTLY_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class KnownLibrary(BaseModel):
    """One entry of settings.known_libraries: a repo to suggest as extra context."""

    url: str = Field(min_length=1)
    ref: str | None = None
    path: str | None = None

    @field_validator("url")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        return value

    @field_validator("ref", "path", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ValueError("ref and path must be strings")
        return value


class SettingsForm(BaseModel):
    """Validated /settings form payload (raw strings in, clean values out)."""

    gitlab_url: str
    gitlab_project: str
    gitlab_token: str = ""
    default_review_prompt: str
    known_libraries: list[KnownLibrary] = []
    nightly_time: str
    max_concurrent_reviews: int = Field(ge=1, le=64)
    poll_interval_seconds: int = Field(ge=1, le=86400)
    post_results_to_gitlab: bool = False

    @field_validator("nightly_time")
    @classmethod
    def _nightly(cls, value: str) -> str:
        value = value.strip()
        if not _NIGHTLY_RE.match(value):
            raise ValueError("must look like HH:MM (24h), e.g. 02:30")
        return value

    @field_validator("known_libraries", mode="before")
    @classmethod
    def _parse_json(cls, value: Any) -> Any:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError("must be valid JSON") from None
        if not isinstance(value, list):
            raise ValueError("must be a JSON list of {url, ref, path} objects")
        return value
