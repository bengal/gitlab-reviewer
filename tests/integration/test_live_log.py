"""Live log persistence (the in-flight half of the live review log).

While a review is still running, the run's log must be visible in the DB
(the UI polls it to show the model's live output, including its thinking).
``review_service.execute_job`` therefore flushes the accumulated, scrubbed
log lines to the run row while the run is in flight; these tests run a
slow, chunk-emitting fake orchestrator and assert from another session that
``run.log`` grows while the run's status is still ``running``.
"""

import time

from sqlalchemy import select

from app.db import get_db_session, get_settings_row
from app.models import MergeRequest, ModelProfile, ReviewRun, RunStatus
from app.orchestrator import RunOutcome
from app.orchestrator.fake_orchestrator import FakeOrchestrator
from app.scheduler import worker
from app.security import encrypt_secret
from app.services import scheduling

PROJECT = "group/project"


class SlowStreamingFake(FakeOrchestrator):
    """FakeOrchestrator that streams log chunks over a couple of seconds
    before finishing — simulates a long review whose output arrives in
    pieces (like the podman orchestrator's streamed container output)."""

    def __init__(self, chunks: list[str], *, gap: float = 0.25):
        super().__init__()
        self._chunks = chunks
        self._gap = gap

    def run_review(self, run, job, *, log_chunk=None) -> RunOutcome:
        for chunk in self._chunks:
            if log_chunk is not None:
                log_chunk(chunk)
            time.sleep(self._gap)
        return super().run_review(run, job, log_chunk=log_chunk)


def _seed(db, *, gitlab_token: str | None = None) -> None:
    row = get_settings_row(db)
    row.gitlab_project = PROJECT
    if gitlab_token is not None:
        row.gitlab_token = encrypt_secret(gitlab_token)
    db.commit()
    profile = ModelProfile(name="claude", provider="anthropic", model_id="claude-sonnet-4")
    mr = MergeRequest(
        project=PROJECT,
        iid=9,
        title="Slow review",
        author="dev@example.com",
        source_branch="slow",
        target_branch="main",
        sha="f" * 40,
        web_url=f"https://gitlab.example.com/{PROJECT}/-/merge_requests/9",
        state="opened",
    )
    db.add_all([profile, mr])
    db.commit()
    scheduling.enqueue(db, mr=mr, profile=profile, schedule_type="immediate")


def test_run_log_is_persisted_while_in_flight(app, db):
    _seed(db)
    # The thinking chunks come first (so they land in an in-flight flush
    # before the ~2s gate) and the steps stretch the run past it.
    chunks = [
        "Thinking: the change is small",
        "Thinking: check the edge case",
    ] + [f"[fake] step {i}" for i in range(10)]
    app.state.orchestrator = SlowStreamingFake(chunks)

    worker.pump_once()
    # Sample the run from a fresh session while the worker thread is still
    # running: its log must grow before the run is finalized (that is the
    # property the live UI relies on).
    saw_inflight = False
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.02)
        with get_db_session() as fresh:
            row = fresh.scalar(select(ReviewRun))
            status, log_text = row.status, row.log
        if status == RunStatus.running.value and "Thinking: check the edge case" in log_text:
            saw_inflight = True
            break
    worker.drain()

    assert saw_inflight, "the in-flight log never reached the DB while the run was running"
    db.expire_all()
    run = db.scalar(select(ReviewRun))
    assert run is not None
    assert run.status == RunStatus.success.value
    # the final commit kept the complete log
    for chunk in chunks:
        assert chunk in run.log


def test_inflight_log_flush_is_scrubbed(app, db):
    """A run secret echoed into the live stream must never reach the DB, not
    even in the in-flight snapshots (the final-commit path is covered by
    the security suite)."""
    secret = "glpat-livelog-abcdef123456"
    _seed(db, gitlab_token=secret)  # plant the real run secret in the env
    # The leak comes first; the steps stretch the run past the flush gate so
    # an in-flight snapshot (which must carry the scrubbed leak) happens.
    chunks = [f"leak {secret}", "second line", "third line"] + [
        f"[fake] step {i}" for i in range(8)
    ]
    app.state.orchestrator = SlowStreamingFake(chunks)

    worker.pump_once()
    saw_scrubbed_inflight = False
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.02)
        with get_db_session() as fresh:
            row = fresh.scalar(select(ReviewRun))
            status, log_text = row.status, row.log
        assert secret not in log_text  # never raw, in-flight or final
        if status == RunStatus.running.value and "***" in log_text:
            saw_scrubbed_inflight = True
            break
    worker.drain()

    assert saw_scrubbed_inflight, "no in-flight log snapshot was observed while running"
    db.expire_all()
    run = db.scalar(select(ReviewRun))
    assert secret not in run.log
    assert "***" in run.log
