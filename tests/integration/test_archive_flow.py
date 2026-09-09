"""Archive flow (M6b): archiving a run moves it out of the default /results
view and into /archive; the detail stays readable (read-only) under
/archive/{id}.

Runs are executed in-process via the app fixture's FakeOrchestrator
(ORCHESTRATOR=fake, DISABLE_SCHEDULER=1); no external services involved.
"""

from datetime import datetime

import pytest
from sqlalchemy import select

from app.db import get_settings_row
from app.models import MergeRequest, ModelProfile, ReviewRun, RunStatus, ScheduledJob
from app.orchestrator.fake_orchestrator import CANNED_SESSION_JSON, FakeOrchestrator
from app.scheduler import worker
from app.services import scheduling

PROJECT = "group/project"


@pytest.fixture()
def authed(client, test_password):
    client.post("/login", data={"password": test_password}, follow_redirects=False)
    return client


def _run_completed(db) -> ReviewRun:
    """Seed one MR + profile, run one review to completion, return the run."""
    row = get_settings_row(db)
    row.gitlab_project = PROJECT
    db.commit()
    profile = ModelProfile(
        name="claude",
        provider="anthropic",
        model_id="claude-sonnet-4-20250514",
    )
    mr = MergeRequest(
        project=PROJECT,
        iid=3,
        title="Tweak parser",
        author="dev@example.com",
        source_branch="tweak",
        target_branch="main",
        sha="c" * 40,
        web_url=f"https://gitlab.example.com/{PROJECT}/-/merge_requests/3",
        state="opened",
    )
    db.add_all([profile, mr])
    db.commit()
    scheduling.enqueue(db, mr=mr, profile=profile, schedule_type="immediate")
    worker.pump_once()
    worker.drain()
    db.expire_all()
    run = db.scalar(select(ReviewRun))
    assert run is not None
    assert run.status == "success"
    return run


def test_results_list_and_detail(authed, db):
    run = _run_completed(db)

    listing = authed.get("/results")
    assert listing.status_code == 200
    assert f"/results/{run.id}" in listing.text
    assert "!3" in listing.text
    assert "Tweak parser" in listing.text
    assert "claude" in listing.text  # model profile name column

    detail = authed.get(f"/results/{run.id}")
    assert detail.status_code == 200
    assert "success" in detail.text
    assert "Automated fake review: no issues found." in detail.text
    assert "[README.md]" in detail.text  # positive bucket finding locator
    assert f'action="/results/{run.id}/archive"' in detail.text  # archive button
    assert 'id="log-box"' in detail.text  # the run's log box
    assert "[fake] starting review run" in detail.text


def test_status_and_archive_filters(authed, db):
    run = _run_completed(db)

    only_errors = authed.get("/results?status=error")
    assert only_errors.status_code == 200
    assert f"/results/{run.id}" not in only_errors.text

    only_success = authed.get("/results?status=success")
    assert f"/results/{run.id}" in only_success.text

    bogus_status = authed.get("/results?status=bogus")
    assert bogus_status.status_code == 200  # unknown values are ignored
    assert f"/results/{run.id}" in bogus_status.text

    assert authed.get("/results?archive=all").status_code == 200


def test_archive_flow(authed, db):
    run = _run_completed(db)

    resp = authed.post(f"/results/{run.id}/archive")
    assert resp.status_code == 200
    assert "archived" in resp.text.lower()
    db.expire_all()
    assert db.get(ReviewRun, run.id).archived_at is not None

    # Hidden from the default /results view...
    listing = authed.get("/results")
    assert f"/results/{run.id}" not in listing.text
    # ...but still listed with archive=all.
    assert f"/results/{run.id}" in authed.get("/results?archive=all").text

    # Present in the archive table...
    archive = authed.get("/archive")
    assert archive.status_code == 200
    assert f"/archive/{run.id}" in archive.text
    assert "Tweak parser" in archive.text

    # ...and the detail is still readable, read-only (no archive button).
    detail = authed.get(f"/archive/{run.id}")
    assert detail.status_code == 200
    assert "Automated fake review: no issues found." in detail.text
    assert 'id="log-box"' in detail.text
    assert "[fake] starting review run" in detail.text
    assert f'action="/results/{run.id}/archive"' not in detail.text

    # The results detail remains reachable for archived runs too.
    assert authed.get(f"/results/{run.id}").status_code == 200


def test_archive_unknown_or_unarchived_run_404(authed, db):
    run = _run_completed(db)
    assert authed.get("/archive/999").status_code == 404
    assert authed.get(f"/archive/{run.id}").status_code == 404  # not archived yet
    assert authed.get("/results/999").status_code == 404


def test_session_download_and_detail_link(authed, app, db):
    """The model's session (thinking + transcript) is stored on the run,
    linked from the detail page, and downloadable as a JSON attachment."""
    app.state.orchestrator = FakeOrchestrator.success(
        result_json={"summary": "ok", "findings": {"critical": []}},
        session_json=CANNED_SESSION_JSON,
    )
    run = _run_completed(db)

    detail = authed.get(f"/results/{run.id}")
    assert detail.status_code == 200
    assert "Model session" in detail.text
    assert f'/results/{run.id}/session' in detail.text

    resp = authed.get(f"/results/{run.id}/session")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert f'run-{run.id}-session.json' in resp.headers.get("content-disposition", "")
    payload = resp.json()
    assert payload["info"]["id"] == CANNED_SESSION_JSON["info"]["id"]
    # the model's thinking blocks are present in the download
    reasoning = [
        part
        for message in payload["messages"]
        for part in message.get("parts", [])
        if part.get("type") == "reasoning"
    ]
    assert reasoning and "Thinking:" in reasoning[0]["text"]


def test_session_download_404_without_session(authed, app, db):
    """A run with no session capture (e.g. a failed run) offers no download
    and the detail page says so."""
    row = get_settings_row(db)
    row.gitlab_project = PROJECT
    db.commit()
    profile = ModelProfile(name="claude", provider="anthropic", model_id="claude-sonnet-4")
    mr = MergeRequest(
        project=PROJECT,
        iid=5,
        title="Failing review",
        author="dev@example.com",
        source_branch="failing",
        target_branch="main",
        sha="e" * 40,
        web_url=f"https://gitlab.example.com/{PROJECT}/-/merge_requests/5",
        state="opened",
    )
    db.add_all([profile, mr])
    db.commit()

    app.state.orchestrator = FakeOrchestrator.failure()
    scheduling.enqueue(db, mr=mr, profile=profile, schedule_type="immediate")
    worker.pump_once()
    worker.drain()
    db.expire_all()

    run = db.scalar(select(ReviewRun).where(ReviewRun.merge_request_id == mr.id))
    assert run is not None
    assert run.status == "error"
    assert run.session_json is None

    assert authed.get(f"/results/{run.id}/session").status_code == 404
    detail = authed.get(f"/results/{run.id}")
    assert detail.status_code == 200
    assert "No session was captured for this run." in detail.text


def _seed_running_run(db, log: str) -> ReviewRun:
    """A run row left in ``running`` with a partial in-flight log (as the
    worker's in-flight flushes would leave one) plus its job/MR/profile."""
    row = get_settings_row(db)
    row.gitlab_project = PROJECT
    db.commit()
    profile = ModelProfile(name="claude", provider="anthropic", model_id="claude-sonnet-4")
    mr = MergeRequest(
        project=PROJECT,
        iid=11,
        title="In flight",
        author="dev@example.com",
        source_branch="inflight",
        target_branch="main",
        sha="1" * 40,
        web_url=f"https://gitlab.example.com/{PROJECT}/-/merge_requests/11",
        state="opened",
    )
    db.add_all([profile, mr])
    db.flush()
    job = ScheduledJob(merge_request_id=mr.id, model_profile_id=profile.id)
    db.add(job)
    db.flush()
    run = ReviewRun(
        scheduled_job_id=job.id,
        merge_request_id=mr.id,
        model_profile_id=profile.id,
        status=RunStatus.running.value,
        started_at=datetime(2026, 9, 9, 10, 0, 0),
        log=log,
    )
    db.add(run)
    db.commit()
    return run


def test_running_detail_page_shows_live_log_box(authed, db):
    """While a run is running, the detail page renders the log in a fixed,
    read-only textarea with an (on-by-default) Auto-update checkbox and the
    live hint, and the JSON endpoint serves the growing in-flight log."""
    run = _seed_running_run(db, "cloning...\nThinking: checking the diff\n")

    detail = authed.get(f"/results/{run.id}")
    assert detail.status_code == 200
    assert 'id="run-log"' in detail.text
    assert 'id="log-box"' in detail.text
    assert 'id="log-autoupdate"' in detail.text
    assert "readonly" in detail.text
    # Auto-update is on by default
    assert 'id="log-autoupdate" checked' in detail.text
    assert "Live — updates every 2 s" in detail.text
    # the in-flight log content (with the thinking block) is shown
    assert "Thinking: checking the diff" in detail.text
    # the client polls the JSON endpoint while running
    assert f"/results/{run.id}/log.json" in detail.text

    # the JSON endpoint serves the in-flight log + a running status
    resp = authed.get(f"/results/{run.id}/log.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert "Thinking: checking the diff" in data["log"]


def test_finished_detail_page_does_not_poll(authed, db):
    """A finished run's log card has no auto-update checkbox and no client
    poll loop, so the box is static and safe to copy."""
    run = _run_completed(db)

    detail = authed.get(f"/results/{run.id}")
    assert detail.status_code == 200
    assert 'id="run-log"' in detail.text
    assert 'id="log-box"' in detail.text
    assert "log-autoupdate" not in detail.text
    assert "log.json" not in detail.text
    assert "Live — updates every 2 s" not in detail.text

    resp = authed.get(f"/results/{run.id}/log.json")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_log_endpoint_404_for_unknown_run(authed, db):
    assert authed.get("/results/999/log.json").status_code == 404
