"""GitLab post-back (M6b): on a successful run the rendered review is posted
as an MR note and the note id is stored; a failed post is logged but keeps
the run successful.

The GitLab client is mocked at the service boundary — the tests monkeypatch
``app.services.review_service.GitLabClient`` — so no network, podman, or
real GitLab is involved. Runs are executed in-process with the
app fixture's FakeOrchestrator (ORCHESTRATOR=fake, DISABLE_SCHEDULER=1).
"""

from sqlalchemy import select

from app.db import get_settings_row
from app.models import JobStatus, MergeRequest, ModelProfile, ReviewRun, RunStatus, ScheduledJob
from app.orchestrator.fake_orchestrator import FakeOrchestrator
from app.scheduler import worker
from app.security import encrypt_secret
from app.services import review_service, scheduling
from app.services.gitlab_client import GitLabError
from app.services.result_render import render_result_markdown

PROJECT = "group/project"


class RecordingClient:
    """Stand-in for app.services.gitlab_client.GitLabClient.

    Records every post_note(iid, body) call; raise an exception instead of
    posting by setting ``fail_with``.
    """

    def __init__(self, settings):
        self.settings = settings
        self.notes: list[tuple[int, str]] = []
        self.fail_with: Exception | None = None

    def post_note(self, iid: int, body: str) -> int:
        if self.fail_with is not None:
            raise self.fail_with
        self.notes.append((iid, body))
        return 4242


def _seed(db, *, post: bool) -> tuple[ModelProfile, MergeRequest]:
    row = get_settings_row(db)
    row.gitlab_url = "https://gitlab.example.com"
    row.gitlab_project = PROJECT
    row.gitlab_token = encrypt_secret("test-gitlab-token-123456")
    row.post_results_to_gitlab = post
    db.commit()
    profile = ModelProfile(
        name="claude",
        provider="anthropic",
        model_id="claude-sonnet-4-20250514",
    )
    mr = MergeRequest(
        project=PROJECT,
        iid=7,
        title="Add feature",
        author="dev@example.com",
        source_branch="feature",
        target_branch="main",
        sha="b" * 40,
        web_url=f"https://gitlab.example.com/{PROJECT}/-/merge_requests/7",
        state="opened",
    )
    db.add_all([profile, mr])
    db.commit()
    return profile, mr


def _run_one(db, profile, mr, *, post_to_gitlab: bool | None) -> tuple[ReviewRun, ScheduledJob]:
    """Enqueue one job, pump it, and wait for the worker to finish."""
    scheduling.enqueue(
        db, mr=mr, profile=profile, schedule_type="immediate", post_to_gitlab=post_to_gitlab
    )
    worker.pump_once()
    worker.drain()
    db.expire_all()
    return db.scalar(select(ReviewRun)), db.scalar(select(ScheduledJob))


def test_postback_posts_rendered_note_and_stores_id(app, db, monkeypatch):
    profile, mr = _seed(db, post=True)
    clients: list[RecordingClient] = []

    class Client(RecordingClient):
        def __init__(self, settings):
            super().__init__(settings)
            clients.append(self)

    monkeypatch.setattr(review_service, "GitLabClient", Client)
    run, job = _run_one(db, profile, mr, post_to_gitlab=None)  # falls back to settings

    assert run.status == RunStatus.success.value
    assert job.status == JobStatus.done.value
    assert run.gitlab_note_id == 4242
    assert len(clients) == 1
    (iid, body), = clients[0].notes
    assert iid == mr.iid
    # The note body is exactly the shared renderer's markdown.
    assert body == render_result_markdown(run.result_json, run.result_markdown, run)
    assert "## Summary" in body
    assert "Automated fake review: no issues found." in body
    assert "## Positive" in body
    assert "- [README.md] Documentation present." in body
    assert "Posted review to GitLab" in run.log


def test_postback_failure_keeps_run_success(app, db, monkeypatch, caplog):
    profile, mr = _seed(db, post=True)
    clients: list[RecordingClient] = []

    class Client(RecordingClient):
        def __init__(self, settings):
            super().__init__(settings)
            self.fail_with = GitLabError("HTTP 500 from gitlab: boom")
            clients.append(self)

    monkeypatch.setattr(review_service, "GitLabClient", Client)
    with caplog.at_level("ERROR"):
        run, job = _run_one(db, profile, mr, post_to_gitlab=None)

    assert len(clients) == 1
    assert run.status == RunStatus.success.value  # the review itself succeeded
    assert job.status == JobStatus.done.value
    assert run.gitlab_note_id is None
    assert clients[0].notes == []
    assert "GitLab post-back failed" in run.log
    assert "HTTP 500 from gitlab: boom" in run.log
    assert any("post-back failed" in record.getMessage() for record in caplog.records)


def test_job_flag_false_disables_postback_when_settings_true(app, db, monkeypatch):
    profile, mr = _seed(db, post=True)
    clients: list[RecordingClient] = []

    class Client(RecordingClient):
        def __init__(self, settings):
            super().__init__(settings)
            clients.append(self)

    monkeypatch.setattr(review_service, "GitLabClient", Client)
    run, job = _run_one(db, profile, mr, post_to_gitlab=False)

    assert run.status == RunStatus.success.value
    assert clients == []  # no client built, no post attempted
    assert run.gitlab_note_id is None
    assert "post-back" not in run.log


def test_job_flag_true_enables_postback_when_settings_false(app, db, monkeypatch):
    profile, mr = _seed(db, post=False)
    clients: list[RecordingClient] = []

    class Client(RecordingClient):
        def __init__(self, settings):
            super().__init__(settings)
            clients.append(self)

    monkeypatch.setattr(review_service, "GitLabClient", Client)
    run, job = _run_one(db, profile, mr, post_to_gitlab=True)

    assert run.gitlab_note_id == 4242
    assert len(clients) == 1
    assert clients[0].notes[0][0] == mr.iid


def test_failed_review_is_not_posted(app, db, monkeypatch):
    profile, mr = _seed(db, post=True)
    clients: list[RecordingClient] = []

    class Client(RecordingClient):
        def __init__(self, settings):
            super().__init__(settings)
            clients.append(self)

    monkeypatch.setattr(review_service, "GitLabClient", Client)
    app.state.orchestrator = FakeOrchestrator.failure(error="simulated failure")
    run, job = _run_one(db, profile, mr, post_to_gitlab=None)

    assert run.status == RunStatus.error.value
    assert job.status == JobStatus.failed.value
    assert clients == []  # post-back only happens for successful runs
    assert run.gitlab_note_id is None
