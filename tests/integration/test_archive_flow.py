"""Archive flow (M6b): archiving a run moves it out of the default /results
view and into /archive; the detail stays readable (read-only) under
/archive/{id}.

Runs are executed in-process via the app fixture's FakeOrchestrator
(ORCHESTRATOR=fake, DISABLE_SCHEDULER=1); no external services involved.
"""

import pytest
from sqlalchemy import select

from app.db import get_settings_row
from app.models import MergeRequest, ModelProfile, ReviewRun
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
    assert "Full log" in detail.text


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
    assert "Full log" in detail.text
    assert f'action="/results/{run.id}/archive"' not in detail.text

    # The results detail remains reachable for archived runs too.
    assert authed.get(f"/results/{run.id}").status_code == 200


def test_archive_unknown_or_unarchived_run_404(authed, db):
    run = _run_completed(db)
    assert authed.get("/archive/999").status_code == 404
    assert authed.get(f"/archive/{run.id}").status_code == 404  # not archived yet
    assert authed.get("/results/999").status_code == 404
