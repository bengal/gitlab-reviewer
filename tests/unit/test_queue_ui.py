"""Queue UI and model-profile CRUD via TestClient (M4 acceptance).

Covers the M4 acceptance criteria: the reorder round-trip works through
TestClient (POST /queue/reorder, verify DB positions and re-rendered order),
plus cancel/top/bottom, nightly remove, the per-MR schedule form (immediate,
nightly, extra projects, error states), and model-profile create/mask/delete.
"""

import re

import pytest
from sqlalchemy import select

from app.db import get_settings_row
from app.models import JobStatus, MergeRequest, ModelProfile, ReviewRun, RunStatus, ScheduledJob, ScheduleType
from app.security import decrypt_secret
from app.services import scheduling

PROJECT = "group/proj"


@pytest.fixture()
def authed(client, test_password):
    client.post("/login", data={"password": test_password}, follow_redirects=False)
    return client


def add_profile(db, name="claude", **kwargs) -> ModelProfile:
    profile = ModelProfile(
        name=name,
        provider=kwargs.pop("provider", "anthropic"),
        model_id=kwargs.pop("model_id", "claude-sonnet-4-20250514"),
        **kwargs,
    )
    db.add(profile)
    db.commit()
    return profile


def add_mr(db, iid: int, title: str) -> MergeRequest:
    mr = MergeRequest(
        project=PROJECT,
        iid=iid,
        title=title,
        author="alice",
        source_branch=f"feature-{iid}",
        target_branch="main",
        sha=str(iid) * 40,
        web_url=f"https://gitlab.example.com/{PROJECT}/-/merge_requests/{iid}",
        state="opened",
    )
    db.add(mr)
    db.commit()
    return mr


def configure(db):
    row = get_settings_row(db)
    row.gitlab_project = PROJECT
    db.commit()
    return row


def queue_job(db, mr, profile, schedule_type=ScheduleType.immediate) -> ScheduledJob:
    return scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=schedule_type)


def db_positions(db) -> dict[int, int]:
    db.expire_all()
    return {
        job.id: job.position
        for job in db.scalars(
            select(ScheduledJob).where(ScheduledJob.status == JobStatus.queued.value)
        )
    }


_ROW_RE = re.compile(r'data-job-id="(\d+)".*?class="queue-pos">(\d+)\.', re.S)


def rendered_queue(html: str) -> list[tuple[int, int]]:
    """[(job_id, position_number)] in the order rendered in the queue section."""
    return [(int(job_id), int(pos)) for job_id, pos in _ROW_RE.findall(html)]


# -- GET /queue ----------------------------------------------------------------


def test_queue_page_renders_three_sections(authed, db):
    profile = add_profile(db)
    mr_a = add_mr(db, 1, "Alpha")
    a = queue_job(db, mr_a, profile)
    b = queue_job(db, add_mr(db, 2, "Beta"), profile)
    n = queue_job(db, add_mr(db, 3, "Gamma"), profile, schedule_type=ScheduleType.nightly)
    r = queue_job(db, mr_a, profile)
    r.status = JobStatus.claimed.value
    db.commit()

    resp = authed.get("/queue")
    assert resp.status_code == 200
    assert "Running" in resp.text
    assert "Queue" in resp.text
    assert "Scheduled nightly" in resp.text
    assert "claimed" in resp.text  # running row shows its status
    assert rendered_queue(resp.text) == [(a.id, 1), (b.id, 2)]
    assert f"/queue/nightly/{n.id}/remove" in resp.text


def test_running_row_links_to_live_log(authed, db):
    """An in-flight job's row links to its run's live log; a job with no
    running run row yet (between claim and run creation) still shows, just
    without the link."""
    profile = add_profile(db)
    mr_a = add_mr(db, 1, "Alpha")
    mr_b = add_mr(db, 2, "Beta")

    # Job with a running run row -> gets the Live log link.
    a = queue_job(db, mr_a, profile)
    a.status = JobStatus.running.value
    run_a = ReviewRun(
        scheduled_job_id=a.id,
        merge_request_id=mr_a.id,
        model_profile_id=profile.id,
        status=RunStatus.running.value,
        log="",
    )
    db.add(run_a)
    # Job claimed but no run row yet -> still shows, no link.
    b = queue_job(db, mr_b, profile)
    b.status = JobStatus.claimed.value
    db.commit()

    resp = authed.get("/queue")
    assert resp.status_code == 200
    assert "running" in resp.text  # both in-flight rows present
    assert "claimed" in resp.text
    assert f'/results/{run_a.id}' in resp.text  # Live log link -> run a
    assert "Live log" in resp.text


# -- POST /queue/reorder -------------------------------------------------------


def test_reorder_roundtrip_through_testclient(authed, db):
    """M4 acceptance: reorder rewrites DB positions and the re-rendered queue
    shows the new order with dense 1..n numbers."""
    profile = add_profile(db)
    a = queue_job(db, add_mr(db, 1, "Alpha"), profile)
    b = queue_job(db, add_mr(db, 2, "Beta"), profile)
    c = queue_job(db, add_mr(db, 3, "Gamma"), profile)

    resp = authed.post("/queue/reorder", data={"ids": [c.id, a.id, b.id]})
    assert resp.status_code == 200
    assert rendered_queue(resp.text) == [(c.id, 1), (a.id, 2), (b.id, 3)]
    assert db_positions(db) == {c.id: 1, a.id: 2, b.id: 3}


def test_reorder_stale_set_shows_message_and_keeps_positions(authed, db):
    profile = add_profile(db)
    a = queue_job(db, add_mr(db, 1, "Alpha"), profile)
    b = queue_job(db, add_mr(db, 2, "Beta"), profile)
    c = queue_job(db, add_mr(db, 3, "Gamma"), profile)
    # A new job lands after the user rendered the page (stale set).
    d = queue_job(db, add_mr(db, 4, "Delta"), profile)

    resp = authed.post("/queue/reorder", data={"ids": [c.id, a.id, b.id]})
    assert resp.status_code == 200
    assert "reorder failed" in resp.text.lower()
    assert db_positions(db) == {a.id: 1, b.id: 2, c.id: 3, d.id: 4}


# -- cancel / top / bottom -----------------------------------------------------


def test_queue_cancel_compacts(authed, db):
    profile = add_profile(db)
    a = queue_job(db, add_mr(db, 1, "Alpha"), profile)
    b = queue_job(db, add_mr(db, 2, "Beta"), profile)
    c = queue_job(db, add_mr(db, 3, "Gamma"), profile)

    resp = authed.post(f"/queue/{b.id}/cancel")
    assert resp.status_code == 200
    db.expire_all()
    assert db.get(ScheduledJob, b.id).status == JobStatus.cancelled.value
    assert rendered_queue(resp.text) == [(a.id, 1), (c.id, 2)]
    assert db_positions(db) == {a.id: 1, c.id: 2}


def test_cancel_inflight_job_finalizes_run_and_job(authed, db, client):
    """A claimed/running job can be cancelled from the UI: the container stop
    is attempted, the run is finalized as error, the job as cancelled."""
    stopped: list[int] = []

    class _Stub:
        def stop_run_container(self, run_id: int) -> None:
            stopped.append(run_id)

    client.app.state.orchestrator = _Stub()
    profile = add_profile(db)
    mr = add_mr(db, 7, "In flight")
    job = queue_job(db, mr, profile)
    job.status = JobStatus.running.value
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

    resp = authed.post(f"/queue/{job.id}/cancel")

    assert resp.status_code == 200
    assert stopped == [run.id]
    db.expire_all()
    assert db.get(ScheduledJob, job.id).status == JobStatus.cancelled.value
    assert run.status == RunStatus.error.value
    assert run.error_message == "cancelled by user"
    assert run.finished_at is not None
    assert "Nothing running right now." in resp.text  # row left the Running section
    assert "cancelled" in resp.text.lower()  # status message rendered


def test_queue_move_top_and_bottom(authed, db):
    profile = add_profile(db)
    a = queue_job(db, add_mr(db, 1, "Alpha"), profile)
    b = queue_job(db, add_mr(db, 2, "Beta"), profile)
    c = queue_job(db, add_mr(db, 3, "Gamma"), profile)

    resp = authed.post(f"/queue/{c.id}/top")
    assert rendered_queue(resp.text) == [(c.id, 1), (a.id, 2), (b.id, 3)]
    assert db_positions(db) == {c.id: 1, a.id: 2, b.id: 3}

    resp = authed.post(f"/queue/{a.id}/bottom")
    assert rendered_queue(resp.text) == [(c.id, 1), (b.id, 2), (a.id, 3)]
    assert db_positions(db) == {c.id: 1, b.id: 2, a.id: 3}


def test_nightly_remove_requeues_at_end(authed, db):
    profile = add_profile(db)
    a = queue_job(db, add_mr(db, 1, "Alpha"), profile)
    n = queue_job(db, add_mr(db, 2, "Beta"), profile, schedule_type=ScheduleType.nightly)

    resp = authed.post(f"/queue/nightly/{n.id}/remove")
    assert resp.status_code == 200
    db.expire_all()
    assert db.get(ScheduledJob, n.id).schedule_type == ScheduleType.immediate.value
    assert rendered_queue(resp.text) == [(a.id, 1), (n.id, 2)]
    assert "No nightly reviews enrolled." in resp.text


# -- POST /mrs/{iid}/schedule ----------------------------------------------------


def test_schedule_immediate_with_extras(authed, db):
    configure(db)
    profile = add_profile(db)
    add_mr(db, 1, "Alpha")

    resp = authed.post(
        "/mrs/1/schedule",
        data={
            "model_profile_id": profile.id,
            "schedule_type": "immediate",
            "prompt_override": "focus on error handling",
            "post_to_gitlab": "true",
            "extra_url_0": "https://gitlab.example.com/group/lib",
            "extra_ref_0": "v1",
            "extra_path_0": "lib",
        },
    )
    assert resp.status_code == 200
    assert "review scheduled" in resp.text.lower()
    assert "queue position 1" in resp.text

    db.expire_all()
    job = db.scalar(select(ScheduledJob))
    assert job.status == JobStatus.queued.value
    assert job.schedule_type == ScheduleType.immediate.value
    assert job.position == 1
    assert job.prompt_override == "focus on error handling"
    assert job.extra_projects == [
        {"url": "https://gitlab.example.com/group/lib", "ref": "v1", "path": "lib"}
    ]
    assert job.post_to_gitlab is True


def test_schedule_nightly(authed, db):
    configure(db)
    profile = add_profile(db)
    add_mr(db, 1, "Alpha")

    resp = authed.post(
        "/mrs/1/schedule",
        data={"model_profile_id": profile.id, "schedule_type": "nightly"},
    )
    assert resp.status_code == 200
    assert "nightly" in resp.text.lower()

    db.expire_all()
    job = db.scalar(select(ScheduledJob))
    assert job.schedule_type == ScheduleType.nightly.value
    assert job.position == 1


def test_schedule_unknown_profile_shows_error(authed, db):
    configure(db)
    add_mr(db, 1, "Alpha")

    resp = authed.post("/mrs/1/schedule", data={"model_profile_id": 999})
    assert resp.status_code == 200
    assert "no model profile" in resp.text.lower()
    db.expire_all()
    assert db.scalar(select(ScheduledJob)) is None


def test_schedule_extra_row_partial(authed):
    resp = authed.post("/mrs/1/schedule-extra-row")
    assert resp.status_code == 200
    assert 'name="extra_url_' in resp.text
    assert 'name="extra_ref_' in resp.text
    assert 'name="extra_path_' in resp.text


def test_schedule_form_shows_profiles_and_library_suggestions(authed, db):
    row = configure(db)
    row.known_libraries = [
        {"url": "https://gitlab.example.com/group/lib", "ref": "v1", "path": "lib"}
    ]
    db.commit()
    profile = add_profile(db, name="claude")
    add_mr(db, 1, "Alpha")

    resp = authed.get("/mrs/1")
    assert resp.status_code == 200
    assert "Schedule review" in resp.text
    assert f'value="{profile.id}"' in resp.text
    assert "claude" in resp.text
    assert "Review nightly" in resp.text
    assert 'value="https://gitlab.example.com/group/lib"' in resp.text


# -- model profiles -------------------------------------------------------------


def test_model_profile_create_encrypts_and_masks(authed, db):
    resp = authed.post(
        "/settings/models",
        data={
            "name": "claude",
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-20250514",
            "api_key": "sk-ant-test123",
            "is_default": "true",
        },
    )
    assert resp.status_code == 200
    assert "added" in resp.text.lower()

    db.expire_all()
    profile = db.scalar(select(ModelProfile).where(ModelProfile.name == "claude"))
    assert profile.api_key is not None
    assert profile.api_key != "sk-ant-test123"  # encrypted at rest
    assert decrypt_secret(profile.api_key) == "sk-ant-test123"
    assert profile.is_default is True

    page = authed.get("/settings")
    assert "claude" in page.text
    assert "••• (set)" in page.text
    assert "sk-ant-test123" not in page.text


def test_model_profile_duplicate_name_rejected(authed, db):
    data = {"name": "claude", "provider": "anthropic", "model_id": "m1"}
    assert authed.post("/settings/models", data=data).status_code == 200
    resp = authed.post("/settings/models", data={**data, "model_id": "m2"})
    assert resp.status_code == 200
    assert "already exists" in resp.text
    db.expire_all()
    assert len(list(db.scalars(select(ModelProfile)))) == 1


def test_model_profile_local_requires_base_url(authed, db):
    resp = authed.post(
        "/settings/models",
        data={"name": "llama", "provider": "local", "model_id": "llama-3"},
    )
    assert resp.status_code == 200
    assert "required for local" in resp.text
    db.expire_all()
    assert db.scalar(select(ModelProfile)) is None


def test_model_profile_delete_refused_while_jobs_reference_it(authed, db):
    profile = add_profile(db)
    profile_id = profile.id
    job = queue_job(db, add_mr(db, 1, "Alpha"), profile)

    resp = authed.post(f"/settings/models/{profile_id}/delete")
    assert resp.status_code == 200
    assert "used by 1 job" in resp.text
    db.expire_all()
    assert db.scalar(select(ModelProfile).where(ModelProfile.id == profile_id)) is not None

    # Cancelling doesn't free the reference: job rows are kept as history.
    scheduling.cancel(db, job)
    resp = authed.post(f"/settings/models/{profile_id}/delete")
    assert resp.status_code == 200
    assert "used by 1 job" in resp.text
    db.expire_all()
    assert db.scalar(select(ModelProfile).where(ModelProfile.id == profile_id)) is not None


def test_model_profile_delete_unused(authed, db):
    profile = add_profile(db)
    profile_id = profile.id

    resp = authed.post(f"/settings/models/{profile_id}/delete")
    assert resp.status_code == 200
    db.expire_all()
    assert db.scalar(select(ModelProfile).where(ModelProfile.id == profile_id)) is None
