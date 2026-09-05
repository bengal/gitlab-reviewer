"""Startup recovery: review runs/jobs left in-flight by a previous process
(restart, crash) are finalized — run -> error, job -> failed, container stop
attempted, queue compacted — and the app lifespan triggers it."""

import pytest
from sqlalchemy import select

from app.models import JobStatus, MergeRequest, ModelProfile, ReviewRun, RunStatus, ScheduledJob, ScheduleType
from app.scheduler import recovery
from app.services import scheduling

PROJECT = "group/proj"


@pytest.fixture()
def mr(db):
    row = MergeRequest(
        project=PROJECT,
        iid=1,
        title="First MR",
        author="alice",
        source_branch="feature-1",
        target_branch="main",
        sha="a" * 40,
        web_url=f"https://gitlab.example.com/{PROJECT}/-/merge_requests/1",
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


def _enqueue(db, mr, profile) -> ScheduledJob:
    return scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=ScheduleType.immediate)


def _make_orphan(db, mr, profile, job_status=JobStatus.running.value) -> tuple[ScheduledJob, ReviewRun]:
    """The DB state a restart leaves behind: claimed/running job + running run."""
    job = _enqueue(db, mr, profile)
    job.status = job_status
    db.commit()
    run = ReviewRun(
        scheduled_job_id=job.id,
        merge_request_id=mr.id,
        model_profile_id=profile.id,
        status=RunStatus.running.value,
        log="",
    )
    db.add(run)
    db.commit()
    return job, run


class _StubOrchestrator:
    def __init__(self, *, raise_on_stop=False):
        self.stopped: list[int] = []
        self._raise = raise_on_stop

    def stop_run_container(self, run_id: int) -> None:
        if self._raise:
            raise RuntimeError("podman socket gone")
        self.stopped.append(run_id)


def test_recover_orphans_finalizes_run_and_job(app, db, mr, profile):
    stub = _StubOrchestrator()
    app.state.orchestrator = stub
    job, run = _make_orphan(db, mr, profile)

    fixed = recovery.recover_orphans(app)

    db.expire_all()
    assert fixed == 1
    assert run.status == RunStatus.error.value
    assert "restarted" in run.error_message
    assert run.finished_at is not None
    assert job.status == JobStatus.failed.value
    assert stub.stopped == [run.id]
    # a second pass finds nothing left to fix
    assert recovery.recover_orphans(app) == 0


def test_recover_orphans_claimed_job_with_run(app, db, mr, profile):
    job, run = _make_orphan(db, mr, profile, job_status=JobStatus.claimed.value)

    fixed = recovery.recover_orphans(app)

    db.expire_all()
    assert fixed == 1
    assert job.status == JobStatus.failed.value
    assert run.status == RunStatus.error.value


def test_recover_orphans_stuck_job_without_run(app, db, mr, profile):
    # crash between the claim commit and the run-row commit
    job = _enqueue(db, mr, profile)
    job.status = JobStatus.claimed.value
    db.commit()

    fixed = recovery.recover_orphans(app)

    db.expire_all()
    assert fixed == 1
    assert job.status == JobStatus.failed.value


def test_recover_orphans_swallows_container_stop_errors(app, db, mr, profile):
    app.state.orchestrator = _StubOrchestrator(raise_on_stop=True)
    job, run = _make_orphan(db, mr, profile)

    fixed = recovery.recover_orphans(app)

    db.expire_all()
    assert fixed == 1
    assert run.status == RunStatus.error.value
    assert job.status == JobStatus.failed.value


def test_recover_orphans_noop_on_clean_db(app, db, mr, profile):
    queued = _enqueue(db, mr, profile)

    fixed = recovery.recover_orphans(app)

    db.expire_all()
    assert fixed == 0
    assert queued.status == JobStatus.queued.value  # queue left untouched


def test_lifespan_runs_recovery_on_startup(app, db, mr, profile):
    _make_orphan(db, mr, profile)
    db.expire_all()
    running = db.scalar(select(ReviewRun).where(ReviewRun.status == RunStatus.running.value))
    assert running is not None

    from fastapi.testclient import TestClient

    with TestClient(app):  # entering runs the lifespan (recovery)
        pass

    db.expire_all()
    running = db.scalar(select(ReviewRun).where(ReviewRun.status == RunStatus.running.value))
    assert running is None
