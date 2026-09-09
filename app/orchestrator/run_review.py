"""Environment for review containers and log secret-scrubbing.

The container receives everything it needs via environment variables only —
secrets are decrypted at the last moment here and never appear in argv or
logs (streamed output is run through ``scrub_secrets``).
"""

import json
import os

from app.config import get_settings
from app.models import MergeRequest, ModelProfile, Provider, ScheduledJob, SettingsRow
from app.security import decrypt_secret
from app.services.prompt import _load_mr, build_review_prompt

RESULT_PATH = "/out/result.json"
LOG_PATH = "/out/review.log"

# Secrets shorter than this are not worth scrubbing for: replacing them would
# mangle the log (e.g. a single-digit value) for little gain.
MIN_SCRUB_LEN = 8

_SECRET_ENV_KEYS = ("GITLAB_TOKEN", "OPENCODE_API_KEY")


def resolve_api_key(profile: ModelProfile) -> str:
    """Raw API key for a profile, highest priority first:

    1. the named environment variable (``profile.api_key_env``) — key stays
       out of the database entirely;
    2. the Fernet-encrypted ``profile.api_key`` column;
    3. for anthropic profiles only, the process ``ANTHROPIC_API_KEY``.
    """
    cfg = get_settings()
    if profile.api_key_env:
        return os.environ.get(profile.api_key_env, "")
    if profile.api_key:
        return decrypt_secret(profile.api_key)
    if profile.provider == Provider.anthropic and cfg.anthropic_api_key:
        return cfg.anthropic_api_key
    return ""


def build_env(
    job: ScheduledJob,
    profile: ModelProfile,
    settings: SettingsRow,
    *,
    mr: MergeRequest | None = None,
) -> dict[str, str]:
    """Environment for the review container (see MILESTONES M5 for the list).

    ``settings`` is the DB settings singleton row (GitLab URL/project/token,
    default prompt); process-level config (llama URL, timeout, fallback key)
    comes from ``get_settings()``. ``OPENCODE_BASE_URL`` is only set for local
    providers; ``OPENCODE_API_KEY`` is only set when a key resolves;
    ``OPENCODE_MODEL_CONTEXT`` is only set when the profile knows the model's
    context window (it drives opencode's auto-compaction for local models).
    """
    if mr is None:
        mr = _load_mr(job)
    cfg = get_settings()
    env: dict[str, str] = {
        "GITLAB_URL": settings.gitlab_url,
        "GITLAB_TOKEN": decrypt_secret(settings.gitlab_token) if settings.gitlab_token else "",
        "TARGET_PROJECT": settings.gitlab_project,
        "MR_IID": str(mr.iid),
        "MR_SHA": mr.sha,
        "SOURCE_BRANCH": mr.source_branch,
        "TARGET_BRANCH": mr.target_branch,
        "EXTRA_PROJECTS": json.dumps(job.extra_projects or []),
        "OPENCODE_PROVIDER": profile.provider,
        "OPENCODE_MODEL": profile.model_id,
        "REVIEW_PROMPT": build_review_prompt(job, settings, mr=mr),
        "REVIEW_TIMEOUT_SECONDS": str(cfg.review_timeout_seconds),
        "RESULT_PATH": RESULT_PATH,
        "LOG_PATH": LOG_PATH,
    }
    api_key = resolve_api_key(profile)
    if api_key:
        env["OPENCODE_API_KEY"] = api_key
    if profile.provider == Provider.local:
        env["OPENCODE_BASE_URL"] = profile.base_url or cfg.llama_base_url or ""
    if profile.context_window:
        env["OPENCODE_MODEL_CONTEXT"] = str(profile.context_window)
    return env


def secret_env_values(env: dict[str, str]) -> list[str]:
    """The secret values out of a run env, for scrubbing streamed output."""
    return [env[key] for key in _SECRET_ENV_KEYS if env.get(key)]


def scrub_secrets(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    """Replace each non-trivial (len >= MIN_SCRUB_LEN) secret value with ***."""
    if not text:
        return text
    for secret in secrets:
        if secret and len(secret) >= MIN_SCRUB_LEN:
            text = text.replace(secret, "***")
    return text


def scrub_json(value: object, secrets: list[str] | tuple[str, ...]) -> object:
    """Recursively scrub secret values out of a parsed JSON structure.

    result_json comes from untrusted LLM output (the container can echo a
    secret it saw in the diff or its own streamed logs), so every string in
    the structure — values and keys alike — is scrubbed before the result is
    stored in the DB, rendered in the UI, or posted to GitLab.
    """
    if isinstance(value, str):
        return scrub_secrets(value, secrets)
    if isinstance(value, dict):
        return {scrub_secrets(str(key), secrets): scrub_json(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_json(item, secrets) for item in value]
    return value
