"""run_review env building + secret scrubbing, and PodmanOrchestrator argv,
allowlist, timeout and result capture (subprocess fully faked)."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from app.db import get_settings_row
from app.models import MergeRequest, ModelProfile, ReviewRun, ScheduleType
from app.orchestrator.podman_client import PodmanOrchestrator
from app.orchestrator.run_review import MIN_SCRUB_LEN, build_env, scrub_json, scrub_secrets
from app.security import encrypt_secret
from app.services import scheduling

GITLAB_TOKEN = "glpat-abcdef123456"


@pytest.fixture()
def mr(db):
    row = MergeRequest(
        project="group/proj",
        iid=1,
        title="First MR",
        author="alice",
        source_branch="feature-1",
        target_branch="main",
        sha="a" * 40,
        web_url="https://gitlab.example.com/group/proj/-/merge_requests/1",
        state="opened",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def profile(db):
    row = ModelProfile(name="claude", provider="anthropic", model_id="claude-sonnet-4")
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def settings_row(db):
    row = get_settings_row(db)
    row.gitlab_url = "https://gitlab.example.com"
    row.gitlab_project = "group/proj"
    row.default_review_prompt = "Review the diff for correctness, security and tests."
    db.commit()
    return row


def _enqueue(db, mr, profile, **kwargs):
    return scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=ScheduleType.immediate, **kwargs)


# -- build_env ---------------------------------------------------------------


def test_build_env_core_vars(app, db, mr, profile, settings_row):
    settings_row.gitlab_token = encrypt_secret(GITLAB_TOKEN)
    db.commit()
    job = _enqueue(db, mr, profile)
    env = build_env(job, profile, settings_row)

    assert env["GITLAB_URL"] == "https://gitlab.example.com"
    assert env["GITLAB_TOKEN"] == GITLAB_TOKEN  # decrypted
    assert env["TARGET_PROJECT"] == "group/proj"
    assert env["MR_IID"] == "1"
    assert env["MR_SHA"] == "a" * 40
    assert env["SOURCE_BRANCH"] == "feature-1"
    assert env["TARGET_BRANCH"] == "main"
    assert env["OPENCODE_PROVIDER"] == "anthropic"
    assert env["OPENCODE_MODEL"] == "claude-sonnet-4"
    assert env["RESULT_PATH"] == "/out/result.json"
    assert env["LOG_PATH"] == "/out/review.log"
    assert env["SESSION_PATH"] == "/out/session.json"
    assert env["REVIEW_TIMEOUT_SECONDS"].isdigit()
    assert "Review the diff" in env["REVIEW_PROMPT"]
    assert "feature-1" in env["REVIEW_PROMPT"]  # context appended
    assert "```json" in env["REVIEW_PROMPT"]  # output contract appended


def test_build_env_base_url_local_only(app, db, mr, settings_row, monkeypatch):
    local = ModelProfile(
        name="local-m", provider="local", model_id="qwen3-32b", base_url="http://llama-server:8080/v1"
    )
    db.add(local)
    db.commit()
    env_local = build_env(_enqueue(db, mr, local), local, settings_row)
    assert env_local["OPENCODE_BASE_URL"] == "http://llama-server:8080/v1"
    assert env_local["OPENCODE_PROVIDER"] == "local"

    # fallback to the process LLAMA_BASE_URL when the profile sets none
    monkeypatch.setenv("LLAMA_BASE_URL", "http://llama-fallback:8080/v1")
    from app.config import get_settings

    get_settings.cache_clear()
    no_url = ModelProfile(name="local-nourl", provider="local", model_id="qwen3-32b")
    db.add(no_url)
    db.commit()
    env_fallback = build_env(_enqueue(db, mr, no_url), no_url, settings_row)
    assert env_fallback["OPENCODE_BASE_URL"] == "http://llama-fallback:8080/v1"


def test_build_env_model_context_only_when_set(app, db, mr, settings_row):
    with_window = ModelProfile(
        name="local-ctx",
        provider="local",
        model_id="qwen3.8",
        base_url="http://llama-server:8080/v1",
        context_window=220000,
    )
    db.add(with_window)
    db.commit()
    env_with = build_env(_enqueue(db, mr, with_window), with_window, settings_row)
    assert env_with["OPENCODE_MODEL_CONTEXT"] == "220000"

    without = ModelProfile(
        name="local-noctx", provider="local", model_id="qwen3.8", base_url="http://llama-server:8080/v1"
    )
    db.add(without)
    db.commit()
    env_without = build_env(_enqueue(db, mr, without), without, settings_row)
    assert "OPENCODE_MODEL_CONTEXT" not in env_without


def test_build_env_local_and_anthropic_variants(app, db, mr, settings_row):
    anth = ModelProfile(
        name="anth-k",
        provider="anthropic",
        model_id="claude-sonnet-4",
        api_key=encrypt_secret("ant-key-123456789"),
    )
    local = ModelProfile(
        name="local-k", provider="local", model_id="qwen3-32b", base_url="http://llama:8080/v1"
    )
    db.add_all([anth, local])
    db.commit()

    env_anth = build_env(_enqueue(db, mr, anth), anth, settings_row)
    assert "OPENCODE_BASE_URL" not in env_anth
    assert env_anth["OPENCODE_API_KEY"] == "ant-key-123456789"

    env_local = build_env(_enqueue(db, mr, local), local, settings_row)
    assert env_local["OPENCODE_BASE_URL"] == "http://llama:8080/v1"
    assert "OPENCODE_API_KEY" not in env_local  # no key configured


def test_build_env_api_key_from_env_var(app, db, mr, settings_row, monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "env-only-key-123")
    profile = ModelProfile(name="envkey", provider="anthropic", model_id="m", api_key_env="TEST_MODEL_KEY")
    db.add(profile)
    db.commit()
    env = build_env(_enqueue(db, mr, profile), profile, settings_row)
    assert env["OPENCODE_API_KEY"] == "env-only-key-123"


def test_build_env_extra_projects_json_round_trip(app, db, mr, profile, settings_row):
    extras = [
        {"url": "https://gitlab.example.com/group/liba.git", "ref": "v1.2", "path": "liba"},
        {"url": "https://gitlab.example.com/group/libb.git", "ref": "main", "path": "libb"},
    ]
    job = _enqueue(db, mr, profile, extra_projects=extras)
    env = build_env(job, profile, settings_row)
    assert json.loads(env["EXTRA_PROJECTS"]) == extras
    assert "/work/lib/liba" in env["REVIEW_PROMPT"]  # mount paths in the prompt
    assert "/work/lib/libb" in env["REVIEW_PROMPT"]


def test_build_env_extra_projects_pathless_get_derived_path(app, db, mr, profile, settings_row):
    """A URL-only extra project gets its /work/lib path derived from the URL
    in both the EXTRA_PROJECTS env and the prompt (the stored job keeps the
    raw entry)."""
    extras = [
        {"url": "https://gitlab.example.com/group/systemd.git"},
        {"url": "https://gitlab.example.com/group/libb.git", "ref": "main", "path": "libb"},
    ]
    job = _enqueue(db, mr, profile, extra_projects=extras)
    assert job.extra_projects == extras  # stored raw
    env = build_env(job, profile, settings_row)
    parsed = json.loads(env["EXTRA_PROJECTS"])
    assert parsed[0] == {"url": "https://gitlab.example.com/group/systemd.git", "path": "systemd"}
    assert parsed[1] == extras[1]
    assert "/work/lib/systemd" in env["REVIEW_PROMPT"]  # derived mount path in the prompt
    assert "/work/lib/libb" in env["REVIEW_PROMPT"]


def test_build_env_prompt_override_wins(app, db, mr, profile, settings_row):
    job = _enqueue(db, mr, profile, prompt_override="Focus on the TLS code only.")
    env = build_env(job, profile, settings_row)
    assert "Focus on the TLS code only." in env["REVIEW_PROMPT"]
    assert "Review the diff" not in env["REVIEW_PROMPT"]


def test_build_env_token_never_in_prompt(app, db, mr, profile, settings_row):
    settings_row.gitlab_token = encrypt_secret(GITLAB_TOKEN)
    db.commit()
    job = _enqueue(db, mr, profile)
    env = build_env(job, profile, settings_row)
    assert GITLAB_TOKEN not in env["REVIEW_PROMPT"]
    assert env["GITLAB_TOKEN"] == GITLAB_TOKEN


# -- scrub_secrets -----------------------------------------------------------


def test_scrub_secrets_multiple_values():
    text = "token=glpat-abc123def456 and key=sk-ant-secret789, keep=1"
    out = scrub_secrets(text, ["glpat-abc123def456", "sk-ant-secret789", "1"])
    assert out == "token=*** and key=***, keep=1"
    assert "glpat-abc123def456" not in out
    assert "sk-ant-secret789" not in out


def test_scrub_secrets_short_values_ignored():
    # below MIN_SCRUB_LEN, replacement would mangle the log
    assert scrub_secrets("a x b", ["x"]) == "a x b"
    assert scrub_secrets(f"a {'1' * (MIN_SCRUB_LEN - 1)} b", ["1" * (MIN_SCRUB_LEN - 1)]) == (
        f"a {'1' * (MIN_SCRUB_LEN - 1)} b"
    )
    assert scrub_secrets(f"a {'1' * MIN_SCRUB_LEN} b", ["1" * MIN_SCRUB_LEN]) == "a *** b"
    assert scrub_secrets("", ["whatever-long-enough"]) == ""
    assert scrub_secrets("nothing here", []) == "nothing here"
    assert scrub_secrets("x", [None, ""]) == "x"


def test_scrub_json_recurses_through_structure():
    secrets = ["glpat-abc123def456", "sk-ant-secret789"]
    value = {
        "summary": "token glpat-abc123def456 here",
        "findings": {"important": [{"description": "key sk-ant-secret789 quoted"}]},
        "glpat-abc123def456": "even keys are scrubbed",
        "questions": ["sk-ant-secret789", "plain", 3],
        "count": 7,
    }
    out = scrub_json(value, secrets)
    assert out == {
        "summary": "token *** here",
        "findings": {"important": [{"description": "key *** quoted"}]},
        "***": "even keys are scrubbed",
        "questions": ["***", "plain", 3],
        "count": 7,
    }
    # non-container values pass through untouched
    assert scrub_json(5, secrets) == 5
    assert scrub_json(None, secrets) is None
    assert scrub_json([], secrets) == []
    assert scrub_json("short", ["x"]) == "short"  # below MIN_SCRUB_LEN


# -- PodmanOrchestrator argv -------------------------------------------------


def test_podman_argv_shape_and_caps(app):
    orch = PodmanOrchestrator()  # image/network from the app fixture env
    argv = orch.build_argv(
        env={"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": GITLAB_TOKEN},
        out_dir="/tmp/out-123",
        name="mr-review-1-abc123",
    )
    joined = " ".join(argv)

    assert argv[:2] == ["podman", "run"]
    assert "--rm" not in argv  # failed runs keep their container for post-mortems
    assert "--privileged" not in argv
    assert "--memory=2g" in argv
    assert "--cpus=2" in argv
    assert "--pids-limit=256" in argv
    assert "--security-opt" in argv and "no-new-privileges" in argv
    # /out must be writable by the container's non-root user: keep-id maps
    # the app user's uid through, Z relabels the mount for SELinux hosts
    assert "--userns=keep-id" in argv
    assert "--network=host" in argv
    assert "--name=mr-review-1-abc123" in argv
    assert "--mount" in argv and "type=bind,src=/tmp/out-123,dst=/out,Z" in argv
    assert argv[-1] == "gitlab-mr-review/review-runner:test"
    # env: names in argv, values never
    assert "GITLAB_TOKEN" in argv and "-e" in argv
    assert GITLAB_TOKEN not in joined
    assert "GITLAB_TOKEN=" not in joined


def test_podman_argv_lib_mounts_read_only(app):
    orch = PodmanOrchestrator()
    argv = orch.build_argv(
        env={"GITLAB_URL": "https://gitlab.example.com"},
        out_dir="/tmp/out-123",
        name="mr-review-1-abc123",
        lib_mounts=[("/srv/libs/liba", "liba"), ("/srv/libs/group/libb", "group/libb")],
    )
    # library checkouts are bound read-only; /out stays writable
    assert "type=bind,src=/tmp/out-123,dst=/out,Z" in argv
    assert "type=bind,src=/srv/libs/liba,dst=/work/lib/liba,ro,Z" in argv
    assert "type=bind,src=/srv/libs/group/libb,dst=/work/lib/group/libb,ro,Z" in argv


def test_podman_image_allowlist_rejects_bad_image(app):
    with pytest.raises(ValueError, match="allowlist"):
        PodmanOrchestrator(image="docker.io/evil/runner:latest")


def test_podman_image_allowlist_prefix_entry(app, monkeypatch):
    monkeypatch.setenv("REVIEW_IMAGE_ALLOWLIST", "gitlab-mr-review/review-runner")
    from app.config import get_settings

    get_settings.cache_clear()
    assert PodmanOrchestrator(image="gitlab-mr-review/review-runner:dev") is not None
    with pytest.raises(ValueError, match="allowlist"):
        PodmanOrchestrator(image="other/vendor/image:1.0")


# -- PodmanOrchestrator run (faked subprocess) -------------------------------


class _FakeProc:
    """stdout is any iterable of lines (or a generator that blocks mid-stream).

    Also implements the context-manager/communicate surface so it stands in
    for the ``subprocess.run``-based ``podman ps``/``podman rm`` lifecycle
    calls (which must not consume the fake's stdout)."""

    def __init__(self, stdout, exit_code=0):
        self._stdout = stdout
        self._exit_code = exit_code
        self.killed = False
        self.args = []

    @property
    def stdout(self):
        return self._stdout

    @property
    def returncode(self):
        return self._exit_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, *args, **kwargs):
        # list stdout is consumed whole (subprocess.run callers); the
        # generator stdout of streaming runs is left for the reader thread
        if isinstance(self._stdout, list):
            text = "".join(line if line.endswith("\n") else line + "\n" for line in self._stdout)
            return (text, None)
        return ("", None)

    def kill(self):
        self.killed = True

    def poll(self):
        return self._exit_code

    def wait(self):
        return self._exit_code


def _blocking_lines():
    yield "line one"
    time.sleep(0.5)  # hangs past the run's hard timeout
    yield "line two"


def test_podman_success_path_streams_and_reads_results(app, db, mr, profile, settings_row, monkeypatch):
    settings_row.gitlab_token = encrypt_secret(GITLAB_TOKEN)
    db.commit()
    job = _enqueue(db, mr, profile)
    run = ReviewRun(scheduled_job_id=job.id, merge_request_id=mr.id, model_profile_id=profile.id)
    db.add(run)
    db.commit()

    captured = {}

    def fake_popen(argv, **kwargs):
        if argv[1] in ("ps", "rm"):  # container lifecycle housekeeping
            return _FakeProc([], exit_code=0)
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        mount = next(a for a in argv if a.startswith("type=bind,src="))
        src = mount.split("src=")[1].split(",")[0]
        with open(f"{src}/result.json", "w", encoding="utf-8") as fh:
            json.dump({"summary": "ok", "findings": {"critical": []}}, fh)
        with open(f"{src}/result.md", "w", encoding="utf-8") as fh:
            fh.write("# review\nlooks fine\n")
        with open(f"{src}/session.json", "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "info": {"id": "ses_1", "title": "review"},
                    "messages": [
                        {
                            "role": "assistant",
                            "parts": [
                                {"type": "reasoning", "text": f"checking diff with {GITLAB_TOKEN}"},
                                {"type": "text", "text": "ok"},
                            ],
                        }
                    ],
                },
                fh,
            )
        return _FakeProc(["cid-abc123", f"cloning repo with {GITLAB_TOKEN}", "done"], exit_code=0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    logs: list[str] = []
    outcome = PodmanOrchestrator(timeout_seconds=5).run_review(run, job, log_chunk=logs.append)

    assert outcome.exit_code == 0
    assert outcome.timed_out is False
    assert outcome.error is None
    assert outcome.result_json == {"summary": "ok", "findings": {"critical": []}}
    assert outcome.result_markdown == "# review\nlooks fine\n"
    # the session export (model thinking + transcript) is captured from /out
    # (raw here; the service layer scrubs it of the run's secrets before
    # storage, like result_json — see test_session_json_is_scrubbed_before_storage)
    assert outcome.session_json is not None
    assert outcome.session_json["info"]["id"] == "ses_1"
    assert outcome.session_json["messages"][0]["parts"][0]["type"] == "reasoning"
    assert "cid-abc123" in logs  # first line = container id, streamed
    # secret streamed by the container is scrubbed before log_chunk
    joined_log = "".join(logs)
    assert GITLAB_TOKEN not in joined_log
    assert "***" in joined_log
    # secret reaches the container via subprocess env only, never argv
    assert captured["env"]["GITLAB_TOKEN"] == GITLAB_TOKEN
    assert GITLAB_TOKEN not in " ".join(captured["argv"])
    assert "OPENCODE_API_KEY" not in captured["env"]  # profile has no key


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_lib_remote(tmp_path) -> Path:
    """A local bare repo (file://) standing in for a library remote."""
    remote = tmp_path / "libremote"
    remote.mkdir()
    bare = remote / "liba.git"
    _git(["init", "--bare", "-b", "main", str(bare)], cwd=remote)
    push = remote / "push"
    push.mkdir()
    _git(["init", "-b", "main", str(push)], cwd=push)
    _git(["config", "user.email", "lib@example.com"], cwd=push)
    _git(["config", "user.name", "Lib"], cwd=push)
    (push / "lib.txt").write_text("library\n", encoding="utf-8")
    _git(["add", "lib.txt"], cwd=push)
    _git(["commit", "-m", "lib"], cwd=push)
    _git(["remote", "add", "origin", str(bare)], cwd=push)
    _git(["push", "origin", "main"], cwd=push)
    return bare


def test_podman_run_clones_and_mounts_library_checkouts(
    app, db, mr, profile, settings_row, tmp_path, monkeypatch
):
    bare = _make_lib_remote(tmp_path)
    lib_root = tmp_path / "libs"
    monkeypatch.setenv("LIBRARY_CHECKOUT_DIR", str(lib_root))
    from app.config import get_settings

    get_settings.cache_clear()

    settings_row.gitlab_token = encrypt_secret(GITLAB_TOKEN)
    db.commit()
    job = _enqueue(
        db, mr, profile, extra_projects=[{"url": bare.as_uri(), "ref": "main", "path": "liba"}]
    )
    run = ReviewRun(scheduled_job_id=job.id, merge_request_id=mr.id, model_profile_id=profile.id)
    db.add(run)
    db.commit()

    captured = {}
    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        if argv[0] == "git":  # the host-side checkout prep runs for real
            return real_popen(argv, **kwargs)
        if argv[1] in ("ps", "rm"):  # container lifecycle housekeeping
            return _FakeProc([], exit_code=0)
        captured["argv"] = argv
        out_src = next(a for a in argv if a.startswith("type=bind,src=")).split("src=")[1].split(",")[0]
        with open(f"{out_src}/result.json", "w", encoding="utf-8") as fh:
            json.dump({"summary": "ok", "findings": {"critical": []}}, fh)
        return _FakeProc(["cid-abc123", "done"], exit_code=0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    logs: list[str] = []
    outcome = PodmanOrchestrator(timeout_seconds=5).run_review(run, job, log_chunk=logs.append)

    assert outcome.exit_code == 0
    # the checkout is bind-mounted read-only into /work/lib/liba
    assert f"type=bind,src={lib_root / 'liba'},dst=/work/lib/liba,ro,Z" in captured["argv"]
    # the host-side checkout was created for the run
    assert (lib_root / "liba" / ".git").is_dir()
    assert (lib_root / "liba" / "lib.txt").read_text(encoding="utf-8") == "library\n"
    # the token never reaches any argv (git gets it via the credential-helper env)
    assert GITLAB_TOKEN not in " ".join(captured["argv"])
    # the checkout prep is streamed (scrubbed) into the run log
    assert any("liba" in line for line in logs)


def test_podman_run_fails_when_initial_library_clone_fails(
    app, db, mr, profile, settings_row, tmp_path, monkeypatch
):
    lib_root = tmp_path / "libs"
    monkeypatch.setenv("LIBRARY_CHECKOUT_DIR", str(lib_root))
    from app.config import get_settings

    get_settings.cache_clear()

    job = _enqueue(
        db,
        mr,
        profile,
        extra_projects=[{"url": (tmp_path / "no-such-repo.git").as_uri(), "path": "liba"}],
    )
    run = ReviewRun(scheduled_job_id=job.id, merge_request_id=mr.id, model_profile_id=profile.id)
    db.add(run)
    db.commit()

    real_popen = subprocess.Popen
    popens: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        popens.append(list(argv))
        if argv[0] == "podman" and argv[1] in ("ps", "rm"):
            return _FakeProc([], exit_code=0)
        if argv[0] == "podman":
            pytest.fail("podman run must not start when the library checkout fails")
        return real_popen(argv, **kwargs)  # git clone runs for real (and fails)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    outcome = PodmanOrchestrator(timeout_seconds=5).run_review(run, job)

    assert outcome.exit_code == 1
    assert "library checkout failed" in outcome.error
    assert outcome.result_json is None


def test_podman_timeout_sets_timed_out_and_kills(app, db, mr, profile, settings_row, monkeypatch):
    job = _enqueue(db, mr, profile)
    run = ReviewRun(scheduled_job_id=job.id, merge_request_id=mr.id, model_profile_id=profile.id)
    db.add(run)
    db.commit()

    fake = _FakeProc(_blocking_lines(), exit_code=137)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)

    logs: list[str] = []
    started = time.monotonic()
    outcome = PodmanOrchestrator(timeout_seconds=0.2).run_review(run, job, log_chunk=logs.append)
    elapsed = time.monotonic() - started

    assert outcome.timed_out is True
    assert "timed out" in outcome.error
    assert outcome.exit_code == 137
    assert outcome.result_json is None
    assert fake.killed is True
    assert elapsed < 2.0  # hard timeout enforced, did not wait for the fake
    assert "line one" in logs  # pre-timeout output still streamed


# -- Container lifecycle: kept on failure, removed on success, purged when stale --


def _lifecycle_setup(db, mr, profile, settings_row):
    settings_row.gitlab_token = encrypt_secret(GITLAB_TOKEN)
    db.commit()
    job = _enqueue(db, mr, profile)
    run = ReviewRun(scheduled_job_id=job.id, merge_request_id=mr.id, model_profile_id=profile.id)
    db.add(run)
    db.commit()
    return job, run


def _lifecycle_fake_popen(removed, ps_lines, run_exit_code, write_result):
    def fake_popen(argv, **kwargs):
        if argv[1] == "ps":
            return _FakeProc(ps_lines, exit_code=0)
        if argv[1] == "rm":
            removed.append(argv[2])
            return _FakeProc([], exit_code=0)
        if write_result:
            src = next(a for a in argv if a.startswith("type=bind,src=")).split("src=")[1].split(",")[0]
            with open(f"{src}/result.json", "w", encoding="utf-8") as fh:
                json.dump({"summary": "ok", "findings": {"critical": []}}, fh)
        return _FakeProc(["cid-abc123", "some output"], exit_code=run_exit_code)

    return fake_popen


def test_podman_success_removes_its_container(app, db, mr, profile, settings_row, monkeypatch):
    job, run = _lifecycle_setup(db, mr, profile, settings_row)
    removed: list[str] = []
    monkeypatch.setattr(
        subprocess, "Popen", _lifecycle_fake_popen(removed, ps_lines=[], run_exit_code=0, write_result=True)
    )

    outcome = PodmanOrchestrator(timeout_seconds=5).run_review(run, job)

    assert outcome.exit_code == 0
    # exactly one removal: the successful run's own container
    assert len(removed) == 1
    assert removed[0].startswith(f"mr-review-{run.id}-")


def test_podman_failure_keeps_its_container(app, db, mr, profile, settings_row, monkeypatch):
    job, run = _lifecycle_setup(db, mr, profile, settings_row)
    removed: list[str] = []
    monkeypatch.setattr(
        subprocess, "Popen", _lifecycle_fake_popen(removed, ps_lines=[], run_exit_code=1, write_result=False)
    )

    outcome = PodmanOrchestrator(timeout_seconds=5).run_review(run, job)

    assert outcome.exit_code == 1
    assert removed == []  # failed container kept for post-mortems


def test_podman_stop_run_container_stops_by_name_prefix(app, monkeypatch):
    orch = PodmanOrchestrator()
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(list(argv))
        return _FakeProc([], exit_code=0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    orch.stop_run_container(13)

    # name prefix locates the live container (run ids stay unambiguous via
    # the trailing dash); exited/missing containers are a silent no-op
    assert calls == [["podman", "stop", "mr-review-13-"]]


def test_podman_stop_run_container_swallows_errors(app, monkeypatch):
    orch = PodmanOrchestrator()

    def fake_popen(argv, **kwargs):
        raise FileNotFoundError("no podman")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    orch.stop_run_container(7)  # must not raise (recovery/cancel stay robust)


def test_podman_purge_removes_only_stale_containers(app, db, mr, profile, settings_row, monkeypatch):
    from datetime import UTC, datetime, timedelta

    job, run = _lifecycle_setup(db, mr, profile, settings_row)
    now = datetime.now(UTC)
    stale = (now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S.123456789 +0000 UTC")
    fresh = now.strftime("%Y-%m-%d %H:%M:%S.123456789 +0000 UTC")
    ps_lines = [f"aa11bb22 {stale}\n", f"cc33dd44 {fresh}\n"]
    removed: list[str] = []
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _lifecycle_fake_popen(removed, ps_lines=ps_lines, run_exit_code=1, write_result=False),
    )

    outcome = PodmanOrchestrator(timeout_seconds=5).run_review(run, job)

    assert outcome.exit_code == 1
    assert removed == ["aa11bb22"]  # stale purged, fresh kept, failed run kept
