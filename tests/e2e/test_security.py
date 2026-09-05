"""Security verification (MILESTONES M7) — five checks:

1. unauthenticated requests to every major route redirect to /login;
2. the gitlab token / api key never appear in the podman argv (they appear
   in the run env dict only, which is expected and correct);
3. podman argv never has --privileged, always has the resource caps
   (--memory/--cpus/--pids-limit/--security-opt no-new-privileges), and a
   non-allowlisted image is rejected;
4. secrets at rest: settings.gitlab_token and model_profile.api_key are
   Fernet ciphertext in the DB, not plaintext, and decrypt back to it;
5. log scrubbing: a fake orchestrator that echoes the gitlab token in its
   log output leaves run.log with *** and never the raw token.

Plus one regression test for a hardening fix: an LLM result_json that
echoes a secret is scrubbed before it is stored on the run.
"""

import json

import pytest
from sqlalchemy import select, text

from app.db import get_settings_row
from app.models import MergeRequest, ModelProfile, ReviewRun, RunStatus, ScheduleType
from app.orchestrator import RunOutcome
from app.orchestrator.fake_orchestrator import FakeOrchestrator
from app.orchestrator.podman_client import PodmanOrchestrator
from app.orchestrator.run_review import build_env
from app.scheduler import worker
from app.security import decrypt_secret, encrypt_secret
from app.services import scheduling

GITLAB_TOKEN = "glpat-sec-abcdef123456"
API_KEY = "sk-ant-sec-abcdef123456"


def _seed(db) -> tuple[object, ModelProfile, MergeRequest]:
    """Settings row (encrypted token) + one profile (encrypted key) + one MR."""
    row = get_settings_row(db)
    row.gitlab_url = "https://gitlab.example.com"
    row.gitlab_project = "group/project"
    row.gitlab_token = encrypt_secret(GITLAB_TOKEN)
    row.default_review_prompt = "Review this merge request."
    db.commit()
    profile = ModelProfile(
        name="sec-model",
        provider="anthropic",
        model_id="claude-sonnet-4",
        api_key=encrypt_secret(API_KEY),
    )
    mr = MergeRequest(
        project="group/project",
        iid=42,
        title="Sec smoke MR",
        author="dev@example.com",
        source_branch="feature",
        target_branch="main",
        sha="d" * 40,
        web_url="https://gitlab.example.com/group/project/-/merge_requests/42",
        state="opened",
    )
    db.add_all([profile, mr])
    db.commit()
    return row, profile, mr


def test_unauthenticated_requests_redirect_to_login(client):
    """(1) Every major route bounces to /login without a session cookie.

    The ``client`` fixture is a fresh TestClient that never logs in.
    """
    for path in ("/mrs", "/queue", "/results", "/archive", "/settings"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login", path


def test_secrets_never_in_podman_argv(app, db):
    """(2) build_env carries the raw secrets; the podman argv never does."""
    row, profile, mr = _seed(db)
    job = scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=ScheduleType.immediate)

    env = build_env(job, profile, row)
    # the env dict is the one place the values may appear (podman picks them
    # up from the subprocess environment via `-e KEY`)
    assert env["GITLAB_TOKEN"] == GITLAB_TOKEN
    assert env["OPENCODE_API_KEY"] == API_KEY

    argv = PodmanOrchestrator().build_argv(env=env, out_dir="/tmp/mr-review-out-1", name="mr-review-1-abc")
    joined = " ".join(argv)
    assert GITLAB_TOKEN not in joined
    assert API_KEY not in joined
    # key names are passed, so the container can read them from its env
    assert "GITLAB_TOKEN" in argv
    assert "OPENCODE_API_KEY" in argv


def test_podman_argv_hardening_flags_and_image_allowlist(app, db):
    """(3) No --privileged, resource caps always present, allowlist enforced."""
    row, profile, mr = _seed(db)
    job = scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=ScheduleType.immediate)
    env = build_env(job, profile, row)

    argv = PodmanOrchestrator().build_argv(env=env, out_dir="/tmp/mr-review-out-1", name="mr-review-1-abc")
    assert "--privileged" not in argv
    assert "--memory=2g" in argv
    assert "--cpus=2" in argv
    assert "--pids-limit=256" in argv
    assert "--security-opt" in argv
    assert "no-new-privileges" in argv

    with pytest.raises(ValueError, match="allowlist"):
        PodmanOrchestrator(image="docker.io/evil/runner:latest")


def test_secrets_at_rest_are_fernet_ciphertext(app, db):
    """(4) The raw DB columns hold Fernet ciphertext, not plaintext."""
    row, profile, _mr = _seed(db)

    raw_token = db.execute(text("SELECT gitlab_token FROM settings WHERE id = 1")).scalar_one()
    raw_key = db.execute(
        text("SELECT api_key FROM model_profile WHERE id = :id"), {"id": profile.id}
    ).scalar_one()

    assert raw_token != GITLAB_TOKEN
    assert raw_key != API_KEY
    assert raw_token.startswith("gAAAAA")  # Fernet token prefix
    assert raw_key.startswith("gAAAAA")
    assert decrypt_secret(raw_token) == GITLAB_TOKEN
    assert decrypt_secret(raw_key) == API_KEY
    assert row.gitlab_token == raw_token  # ORM view matches the raw column


class LeakingFake(FakeOrchestrator):
    """FakeOrchestrator whose streamed log output echoes the gitlab token —
    simulates a container/LLM that leaks secrets into its output."""

    def __init__(self, secret: str, **kwargs):
        super().__init__(**kwargs)
        self._secret = secret

    def run_review(self, run, job, *, log_chunk=None) -> RunOutcome:
        if log_chunk is not None:
            log_chunk(f"[leak] GITLAB_TOKEN={self._secret}")
        return super().run_review(run, job, log_chunk=log_chunk)


def test_run_log_is_scrubbed_of_echoed_secrets(app, db):
    """(5) A run whose log output contains the token stores *** instead."""
    _row, profile, mr = _seed(db)
    app.state.orchestrator = LeakingFake(GITLAB_TOKEN)

    scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=ScheduleType.immediate)
    worker.pump_once()
    worker.drain()
    db.expire_all()

    run = db.scalar(select(ReviewRun))
    assert run is not None
    assert run.status == RunStatus.success.value
    assert GITLAB_TOKEN not in run.log
    assert "***" in run.log


class LeakyResultFake(FakeOrchestrator):
    """FakeOrchestrator returning a result_json that echoes both secrets —
    simulates untrusted LLM output quoting material it saw in the run."""

    def run_review(self, run, job, *, log_chunk=None) -> RunOutcome:
        return RunOutcome(
            exit_code=0,
            result_json={
                "summary": f"leaked {GITLAB_TOKEN} and {API_KEY} in the summary",
                "findings": {
                    "critical": [],
                    "important": [
                        {"file": "src/leak.py", "line": 1, "description": f"hardcoded {GITLAB_TOKEN}"}
                    ],
                    "minor": [],
                    "positive": [],
                },
                "commit_message_review": API_KEY,
                "questions": [f"why is {API_KEY} here?"],
            },
        )


def test_result_json_is_scrubbed_before_storage(app, db):
    """Hardening regression: a secret echoed into the LLM's result_json must
    not reach the DB (it is rendered in the UI and posted to GitLab)."""
    _row, profile, mr = _seed(db)
    app.state.orchestrator = LeakyResultFake()

    scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=ScheduleType.immediate)
    worker.pump_once()
    worker.drain()
    db.expire_all()

    run = db.scalar(select(ReviewRun))
    assert run is not None
    assert run.status == RunStatus.success.value
    flat = json.dumps(run.result_json)
    assert GITLAB_TOKEN not in flat
    assert API_KEY not in flat
    assert "***" in flat
    # the scrub kept the structure intact
    assert run.result_json["findings"]["important"][0]["file"] == "src/leak.py"
