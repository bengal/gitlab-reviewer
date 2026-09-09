"""opencode.json rendering: anthropic vs local, apiKey placement, permissions,
context-window limit, extra_opencode_json merging, file writer."""

import importlib.util
import json
from pathlib import Path

from app.models import ModelProfile
from app.orchestrator.opencode_template import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    render_opencode_json,
    render_opencode_json_file,
)
from app.security import encrypt_secret

REPO_ROOT = Path(__file__).resolve().parents[2]

PERMISSIONS = {
    "bash": "allow",
    "edit": "allow",
    "webfetch": "allow",
    "external_directory": "allow",
}


def _profile(**kwargs) -> ModelProfile:
    base = dict(name="t", provider="anthropic", model_id="claude-sonnet-4-20250514", extra_opencode_json={})
    base.update(kwargs)
    return ModelProfile(**base)


def test_anthropic_rendering(app, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = _profile(api_key=encrypt_secret("sk-ant-testkey-123"))
    config = render_opencode_json(profile)

    assert set(config["provider"]) == {"anthropic"}
    block = config["provider"]["anthropic"]
    assert block["npm"] == "@ai-sdk/anthropic"
    assert block["options"]["apiKey"] == "sk-ant-testkey-123"  # decrypted, not Fernet ciphertext
    assert "baseURL" not in block["options"]
    assert "models" not in block  # built-in provider knows its own models
    assert config["model"] == "anthropic/claude-sonnet-4-20250514"
    assert config["permission"] == PERMISSIONS


def test_local_rendering_has_base_url_no_api_key(app, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = _profile(provider="local", model_id="qwen3-32b", base_url="http://llama-server:8080/v1")
    config = render_opencode_json(profile)

    assert set(config["provider"]) == {"local"}
    block = config["provider"]["local"]
    assert block["npm"] == "@ai-sdk/openai-compatible"
    assert block["options"]["baseURL"] == "http://llama-server:8080/v1"
    assert "apiKey" not in block["options"]
    # custom providers need the model declared or opencode raises
    # ProviderModelNotFoundError
    assert block["models"] == {"qwen3-32b": {"name": "qwen3-32b"}}
    assert config["model"] == "local/qwen3-32b"
    assert config["permission"] == PERMISSIONS


def test_local_context_window_renders_model_limit(app, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = _profile(
        provider="local",
        model_id="qwen3.8",
        base_url="http://llama-server:8080/v1",
        context_window=220000,
    )
    block = render_opencode_json(profile)["provider"]["local"]
    assert block["models"] == {
        "qwen3.8": {
            "name": "qwen3.8",
            "limit": {"context": 220000, "output": DEFAULT_MAX_OUTPUT_TOKENS},
        }
    }


def test_anthropic_context_window_is_not_rendered(app, monkeypatch):
    """Built-in providers know their models; a context_window there would
    override models.dev data, so it is only honored for declared (local)
    models."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = _profile(context_window=220000)
    block = render_opencode_json(profile)["provider"]["anthropic"]
    assert "models" not in block


def test_default_max_output_matches_container_entrypoint():
    """The app-side renderer and the container entrypoint must agree on the
    limit.output headroom (each hardcodes it; this keeps them in sync)."""
    spec = importlib.util.spec_from_file_location(
        "review_runner_entrypoint", REPO_ROOT / "containers/review-runner/entrypoint.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DEFAULT_MAX_OUTPUT_TOKENS == DEFAULT_MAX_OUTPUT_TOKENS


def test_local_api_key_placement(app, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = _profile(
        provider="local",
        model_id="qwen3-32b",
        base_url="http://llama-server:8080/v1",
        api_key=encrypt_secret("llama-secret-1"),
    )
    block = render_opencode_json(profile)["provider"]["local"]
    assert block["options"]["apiKey"] == "llama-secret-1"
    assert block["options"]["baseURL"] == "http://llama-server:8080/v1"


def test_api_key_from_named_env_var(app, monkeypatch):
    monkeypatch.setenv("MR_REVIEW_KEY", "env-key-12345678")
    profile = _profile(api_key_env="MR_REVIEW_KEY")
    assert render_opencode_json(profile)["provider"]["anthropic"]["options"]["apiKey"] == "env-key-12345678"


def test_anthropic_falls_back_to_process_key(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cfg-key-12345678")
    from app.config import get_settings

    get_settings.cache_clear()
    config = render_opencode_json(_profile())
    assert config["provider"]["anthropic"]["options"]["apiKey"] == "cfg-key-12345678"


def test_local_base_url_falls_back_to_llama_env(app, monkeypatch):
    monkeypatch.setenv("LLAMA_BASE_URL", "http://llama-server:8080/v1")
    from app.config import get_settings

    get_settings.cache_clear()
    config = render_opencode_json(_profile(provider="local", model_id="m"))
    assert config["provider"]["local"]["options"]["baseURL"] == "http://llama-server:8080/v1"


def test_extra_opencode_json_merged_without_clobbering(app, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = _profile(
        extra_opencode_json={
            "experimental": {"share_mcp_tools": True},
            "provider": {"anthropic": {"options": {"timeout": 5000}}},
            "instructions": "prefer terse findings",
        }
    )
    config = render_opencode_json(profile)

    assert config["experimental"] == {"share_mcp_tools": True}
    assert config["instructions"] == "prefer terse findings"
    block = config["provider"]["anthropic"]
    assert block["options"]["timeout"] == 5000  # extra options merged in
    assert block["npm"] == "@ai-sdk/anthropic"  # base block not clobbered
    assert "apiKey" not in block["options"]  # no key configured
    assert config["permission"] == PERMISSIONS  # permissions survive the merge


def test_render_opencode_json_file(app, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = _profile(api_key=encrypt_secret("sk-file-12345678"))
    path = tmp_path / "opencode.json"

    config = render_opencode_json_file(str(path), profile)

    assert json.loads(path.read_text(encoding="utf-8")) == config
    assert config["provider"]["anthropic"]["options"]["apiKey"] == "sk-file-12345678"
    assert path.read_text(encoding="utf-8").endswith("\n")
