"""Queue pump end-to-end (M6a): enqueue -> pump_once -> fake runs -> done.

The ``app`` fixture runs with ORCHESTRATOR=fake and DISABLE_SCHEDULER=1, so
no background scheduler interferes; the pump is driven manually via
``worker.pump_once()`` and the daemon worker threads are joined with
``worker.drain()``. The FakeOrchestrator uses a small delay so concurrent
runs overlap and the concurrency cap is observable (``max_seen``).
"""

from sqlalchemy import select

from app.db import get_settings_row
from app.models import (
    JobStatus,
    MergeRequest,
    ModelProfile,
    ReviewRun,
    RunStatus,
    ScheduledJob,
    ScheduleType,
)
from app.orchestrator.fake_orchestrator import CANNED_RESULT_JSON, FakeOrchestrator
from app.scheduler import worker
from app.security import encrypt_secret
from app.services import scheduling

FAKE_DELAY = 0.5


def _seed(db, *, cap: int = 2) -> tuple[ModelProfile, MergeRequest]:
    row = get_settings_row(db)
    row.gitlab_url = "https://gitlab.example.com"
    row.gitlab_project = "group/project"
    row.gitlab_token = encrypt_secret("test-gitlab-token-123456")
    row.default_review_prompt = "Review this merge request."
    row.max_concurrent_reviews = cap
    row.poll_interval_seconds = 5
    row.nightly_time = "02:30"
    db.commit()

    profile = ModelProfile(
        name="local-test-model",
        provider="local",
        model_id="qwen3:test",
        base_url="http://llama.test:8080/v1",
    )
    mr = MergeRequest(
        project="group/project",
        iid=1,
        title="Add feature",
        author="dev@example.com",
        source_branch="feature",
        target_branch="main",
        sha="a" * 40,
        web_url="https://gitlab.example.com/group/project/-/merge_requests/1",
        state="opened",
    )
    db.add_all([profile, mr])
    db.commit()
    return profile, mr


def test_pump_runs_all_jobs_within_concurrency_cap(app, db):
    profile, mr = _seed(db, cap=2)
    jobs = [
        scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=ScheduleType.immediate)
        for _ in range(3)
    ]

    orchestrator = FakeOrchestrator(delay=FAKE_DELAY)
    app.state.orchestrator = orchestrator

    started = worker.pump_once()
    worker.drain()

    assert len(started) == 3  # one pump drained the whole queue
    assert orchestrator.run_count == 3
    assert orchestrator.max_seen == 2  # cap=2 actually constrained 3 jobs

    db.expire_all()
    assert {job.status for job in jobs} == {JobStatus.done}

    runs = list(db.scalars(select(ReviewRun).order_by(ReviewRun.id)))
    assert len(runs) == 3
    assert {run.scheduled_job_id for run in runs} == {job.id for job in jobs}
    for run in runs:
        assert run.status == RunStatus.success
        assert run.exit_code == 0
        assert run.result_json == CANNED_RESULT_JSON
        assert run.started_at is not None
        assert run.finished_at is not None
        assert run.container_id is None  # the fake orchestrator prints no id
        assert "[fake] starting review run" in run.log
        assert "[fake] finished: exit_code=0" in run.log


def test_cancel_during_queue_compacts_positions(app, db):
    profile, mr = _seed(db, cap=2)
    first = scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=ScheduleType.immediate)
    second = scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=ScheduleType.immediate)
    assert (first.position, second.position) == (1, 2)

    scheduling.cancel(db, first)
    survivor = db.get(ScheduledJob, second.id)
    assert survivor.position == 1  # compacted after the cancel

    app.state.orchestrator = FakeOrchestrator(delay=FAKE_DELAY)
    started = worker.pump_once()
    worker.drain()

    assert len(started) == 1
    db.expire_all()
    assert first.status == JobStatus.cancelled
    assert survivor.status == JobStatus.done
    queued = list(
        db.scalars(select(ScheduledJob).where(ScheduledJob.status == JobStatus.queued.value))
    )
    assert queued == []
