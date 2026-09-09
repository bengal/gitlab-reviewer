"""Results list UI: the runs table's duration column (replaces Finished)."""

from datetime import datetime, timedelta

import pytest

from app.models import MergeRequest, ModelProfile, ReviewRun, RunStatus, ScheduledJob
from app.routers.results import format_run_duration


@pytest.fixture()
def authed(client, test_password):
    client.post("/login", data={"password": test_password}, follow_redirects=False)
    return client


def test_format_run_duration_units():
    t0 = datetime(2026, 9, 6, 12, 0, 0)
    assert format_run_duration(t0, t0) == "0s"
    assert format_run_duration(t0, t0 + timedelta(seconds=45)) == "45s"
    assert format_run_duration(t0, t0 + timedelta(minutes=7, seconds=12)) == "7m 12s"
    assert format_run_duration(t0, t0 + timedelta(hours=1, minutes=5, seconds=59)) == "1h 5m"


def test_format_run_duration_incomplete():
    assert format_run_duration(None, None) == ""
    assert format_run_duration(datetime(2026, 9, 6), None) == ""
    assert format_run_duration(None, datetime(2026, 9, 6)) == ""


def _seed_run(
    db, iid: int, status: RunStatus, started: datetime | None, finished: datetime | None
) -> ReviewRun:
    mr = MergeRequest(project="group/proj", iid=iid, title="MR")
    profile = ModelProfile(name=f"model-{iid}", provider="local", model_id="m", base_url="http://x/v1")
    db.add_all([mr, profile])
    db.flush()
    job = ScheduledJob(merge_request_id=mr.id, model_profile_id=profile.id)
    db.add(job)
    db.flush()
    run = ReviewRun(
        scheduled_job_id=job.id,
        merge_request_id=mr.id,
        model_profile_id=profile.id,
        status=status,
        started_at=started,
        finished_at=finished,
    )
    db.add(run)
    db.commit()
    return run


def test_results_table_shows_duration_not_finished(authed, db):
    t0 = datetime(2026, 9, 6, 12, 0, 0)
    _seed_run(db, 1, RunStatus.success, t0, t0 + timedelta(minutes=7, seconds=12))
    _seed_run(db, 2, RunStatus.running, t0, None)

    page = authed.get("/results")

    assert page.status_code == 200
    assert "<th>Duration</th>" in page.text
    assert "<th>Finished</th>" not in page.text
    assert "7m 12s" in page.text
    assert "in progress" in page.text
