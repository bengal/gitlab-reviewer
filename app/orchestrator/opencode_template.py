"""Render the opencode.json config for a model profile (app-side renderer).

Used by tests and mirrors what the review-runner entrypoint produces from its
env, so the two can be asserted to agree. Shape (per provider):

    {
      "provider": {"<provider>": {
          "npm": "<ai-sdk package>",
          "options": {...},
          "models": {"<model_id>": {"name": "<model_id>"}}   # custom providers only
      }},
      "model": "<provider>/<model_id>",
      "permission": {"bash": "allow", "edit": "allow", "webfetch": "allow",
                      "external_directory": "allow"}
    }

Custom (openai-compatible) providers carry no built-in model list, so the
profile's model must be declared in ``models`` or opencode fails with
ProviderModelNotFoundError; built-in providers (anthropic) know their models.

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
# external_directory defaults to "ask" in opencode and aborts a headless run
# the moment a tool touches a path outside the working dir (e.g. /tmp); the
# container itself is the sandbox, so allow it.
DEFAULT_PERMISSIONS: dict[str, str] = {
    "bash": "allow",
    "edit": "allow",
    "webfetch": "allow",
    "external_directory": "allow",
}


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
    block: dict[str, Any] = {"npm": npm, "options": options}
    if npm == OPENAI_COMPATIBLE_NPM:
        # Custom npm providers ship no model list; declare the model or
        # opencode raises ProviderModelNotFoundError for <provider>/<model_id>.
        block["models"] = {profile.model_id: {"name": profile.model_id}}
    return block


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
