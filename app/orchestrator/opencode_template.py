"""Render the opencode.json config for a model profile (app-side renderer).

Used by tests and mirrors what the review-runner entrypoint produces from its
env, so the two can be asserted to agree. Shape (per provider):

    {
      "provider": {"<provider>": {"npm": "<ai-sdk package>", "options": {...}}},
      "model": "<provider>/<model_id>",
      "permission": {"bash": "allow", "edit": "allow", "webfetch": "allow"}
    }

``profile.extra_opencode_json`` is deep-merged over the rendered config (so it
can add keys or extend provider options without clobbering the base).
"""

import json
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models import ModelProfile, Provider
from app.orchestrator.run_review import resolve_api_key

ANTHROPIC_NPM = "@ai-sdk/anthropic"
OPENAI_COMPATIBLE_NPM = "@ai-sdk/openai-compatible"

# Headless opencode must never hang waiting for an interactive permission
# answer, so these are always allowed (see PLAN "Key risks").
DEFAULT_PERMISSIONS: dict[str, str] = {"bash": "allow", "edit": "allow", "webfetch": "allow"}


def _provider_block(profile: ModelProfile) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if profile.provider == Provider.anthropic:
        npm = ANTHROPIC_NPM
    else:
        npm = OPENAI_COMPATIBLE_NPM
        options["baseURL"] = profile.base_url or get_settings().llama_base_url or ""
    api_key = resolve_api_key(profile)
    if api_key:
        options["apiKey"] = api_key
    return {"npm": npm, "options": options}


def _deep_merge(base: Any, extra: Any) -> Any:
    """Recursively merge dicts; non-dict extras replace the base value."""
    if isinstance(base, dict) and isinstance(extra, dict):
        merged = dict(base)
        for key, value in extra.items():
            merged[key] = _deep_merge(merged[key], value) if key in merged else value
        return merged
    return extra


def render_opencode_json(profile: ModelProfile) -> dict[str, Any]:
    """The opencode.json document for ``profile``."""
    provider_id = profile.provider
    config: dict[str, Any] = {
        "provider": {provider_id: _provider_block(profile)},
        "model": f"{provider_id}/{profile.model_id}",
        "permission": dict(DEFAULT_PERMISSIONS),
    }
    extra = profile.extra_opencode_json
    if isinstance(extra, dict) and extra:
        config = _deep_merge(config, extra)
    return config


def render_opencode_json_file(path: str | Path, profile: ModelProfile) -> dict[str, Any]:
    """Write the rendered config to ``path`` (pretty JSON) and return it."""
    config = render_opencode_json(profile)
    Path(path).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config
