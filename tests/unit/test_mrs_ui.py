"""MR list/detail and settings UI via TestClient.

Covers the M3 acceptance criteria: with empty settings /mrs renders a
friendly not-configured state; with a (mocked) GitLab, sync populates the
list; the settings form saves, masks the token, validates, and can
test the connection.
"""

import json
import re

import pytest
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
from app.schemas.mr import MrSnapshot
from app.security import decrypt_secret, encrypt_secret
from app.services import mr_sync
from app.services.gitlab_client import GitLabError


@pytest.fixture()
def authed(client, test_password):
    client.post("/login", data={"password": test_password}, follow_redirects=False)
    return client


SNAPSHOTS = [
    MrSnapshot(
        iid=1,
        title="First MR",
        author="alice",
        source_branch="feature-1",
        target_branch="main",
        sha="a" * 40,
        web_url="https://gitlab.example.com/group/proj/-/merge_requests/1",
        state="opened",
        updated_at=None,
    ),
    MrSnapshot(
        iid=2,
        title="Second MR",
        author="bob",
        source_branch="feature-2",
        target_branch="main",
        sha="b" * 40,
        web_url="https://gitlab.example.com/group/proj/-/merge_requests/2",
        state="opened",
        updated_at=None,
    ),
]


def install_fake_client(monkeypatch, snapshots=None, exc=None):
    class FakeClient:
        def __init__(self, settings_row):
            pass

        def list_open_merge_requests(self):
            if exc is not None:
                raise exc
            return list(snapshots if snapshots is not None else SNAPSHOTS)

        def get_project_default_branch(self):
            return "main"

    monkeypatch.setattr(mr_sync, "GitLabClient", FakeClient)


def configure_row(db):
    row = get_settings_row(db)
    row.gitlab_url = "https://gitlab.example.com"
    row.gitlab_project = "group/proj"
    row.gitlab_token = encrypt_secret("glpat-x")
    db.commit()
    return row


# -- /mrs --------------------------------------------------------------------


def test_mrs_not_configured_renders_friendly_state(authed):
    resp = authed.get("/mrs")
    assert resp.status_code == 200
    assert "not configured" in resp.text.lower()
    assert "/settings" in resp.text


def test_mrs_sync_not_configured_renders_partial(authed):
    resp = authed.post("/mrs/sync")
    assert resp.status_code == 200
    assert "not configured" in resp.text.lower()


def test_mrs_sync_populates_list(authed, db, monkeypatch):
    configure_row(db)
    install_fake_client(monkeypatch)

    resp = authed.post("/mrs/sync")
    assert resp.status_code == 200
    assert "synced: 2 added" in resp.text.lower()
    assert "first mr" in resp.text.lower()
    assert "feature-1" in resp.text

    rows = list(db.scalars(select(MergeRequest)))
    assert sorted(r.iid for r in rows) == [1, 2]

    page = authed.get("/mrs")
    assert page.status_code == 200
    assert "first mr" in page.text.lower()


def test_mrs_list_hides_default_target_branch(authed, db, monkeypatch):
    configure_row(db)
    snapshots = [
        MrSnapshot(
            iid=1,
            title="To default",
            author="alice",
            source_branch="feature-1",
            target_branch="main",
            sha="a" * 40,
            web_url="https://gitlab.example.com/group/proj/-/merge_requests/1",
            state="opened",
            updated_at=None,
        ),
        MrSnapshot(
            iid=2,
            title="To other",
            author="bob",
            source_branch="feature-2",
            target_branch="legacy",
            sha="b" * 40,
            web_url="https://gitlab.example.com/group/proj/-/merge_requests/2",
            state="opened",
            updated_at=None,
        ),
    ]
    install_fake_client(monkeypatch, snapshots=snapshots)

    resp = authed.post("/mrs/sync")
    assert resp.status_code == 200

    # Only the non-default target gets the arrow suffix
    assert resp.text.count("&rarr;") == 1
    assert "legacy" in resp.text


def test_mrs_list_shows_target_when_default_unknown(authed, db, monkeypatch):
    configure_row(db)

    class FakeClient:
        def __init__(self, settings_row):
            pass

        def list_open_merge_requests(self):
            return [SNAPSHOTS[0]]

        def get_project_default_branch(self):
            return None

    monkeypatch.setattr(mr_sync, "GitLabClient", FakeClient)

    resp = authed.post("/mrs/sync")
    assert resp.status_code == 200
    assert resp.text.count("&rarr;") == 1  # no default known: target always shown


def test_mrs_sync_shows_error_message(authed, db, monkeypatch):
    configure_row(db)
    install_fake_client(monkeypatch, exc=GitLabError("HTTP 401 from https://gitlab.example.com: bad token"))

    resp = authed.post("/mrs/sync")
    assert resp.status_code == 200
    assert "sync failed" in resp.text.lower()
    assert "401" in resp.text


def test_mr_detail_renders_snapshot_and_schedule_hook(authed, db, monkeypatch):
    configure_row(db)
    install_fake_client(monkeypatch)
    authed.post("/mrs/sync")

    resp = authed.get("/mrs/1")
    assert resp.status_code == 200
    assert "First MR" in resp.text
    assert "alice" in resp.text
    assert "feature-1" in resp.text
    assert "a" * 8 in resp.text  # short sha in list, full sha on detail
    assert "https://gitlab.example.com/group/proj/-/merge_requests/1" in resp.text
    assert "Schedule review" in resp.text


def test_mr_detail_unknown_iid_404(authed, db):
    configure_row(db)
    resp = authed.get("/mrs/999")
    assert resp.status_code == 404


# -- review counts + batch scheduling -------------------------------------------


def _seed_history(db, mr, *, runs=0, queued=0):
    """Create `runs` finished review runs and `queued` queued jobs for `mr`."""
    profile = db.scalar(select(ModelProfile).where(ModelProfile.name == "history"))
    if profile is None:
        profile = ModelProfile(name="history", provider="anthropic", model_id="claude-sonnet-4")
        db.add(profile)
        db.flush()
    for position in range(1, runs + 1):
        job = ScheduledJob(
            merge_request_id=mr.id,
            model_profile_id=profile.id,
            schedule_type=ScheduleType.immediate.value,
            position=position,
            status=JobStatus.done.value,
        )
        db.add(job)
        db.flush()
        db.add(
            ReviewRun(
                scheduled_job_id=job.id,
                merge_request_id=mr.id,
                model_profile_id=profile.id,
                status=RunStatus.success.value,
            )
        )
    for position in range(1, queued + 1):
        db.add(
            ScheduledJob(
                merge_request_id=mr.id,
                model_profile_id=profile.id,
                schedule_type=ScheduleType.nightly.value,
                position=position,
                status=JobStatus.queued.value,
            )
        )
    db.commit()
    return profile


def _row_cells(resp, iid):
    """The <td> contents of the list row for MR !<iid> (in column order)."""
    row_html = re.search(rf'href="/mrs/{iid}">!{iid}.*?</tr>', resp.text, re.S).group(0)
    return re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)


def test_mrs_list_shows_review_and_scheduled_counts(authed, db, monkeypatch):
    configure_row(db)
    install_fake_client(monkeypatch)
    authed.post("/mrs/sync")
    by_iid = {mr.iid: mr for mr in db.scalars(select(MergeRequest))}
    _seed_history(db, by_iid[1], runs=3, queued=2)
    _seed_history(db, by_iid[2], runs=0, queued=1)

    resp = authed.get("/mrs")
    assert resp.status_code == 200
    assert _row_cells(resp, 1)[-2:] == ["3", "2"]
    assert _row_cells(resp, 2)[-2:] == ["0", "1"]


# -- /settings -----------------------------------------------------------------


def test_settings_page_prefills_default_prompt(authed):
    resp = authed.get("/settings")
    assert resp.status_code == 200
    assert "Default review prompt" in resp.text
    # The bundled prompt is pre-seeded into the empty textarea on first use.
    assert "Challenge the premise" in resp.text
    assert 'name="gitlab_token"' in resp.text


def test_settings_save_stores_encrypted_token_and_prompt(authed, db):
    resp = authed.post(
        "/settings",
        data={
            "gitlab_url": "https://gitlab.example.com",
            "gitlab_project": "group/proj",
            "gitlab_token": "glpat-newtoken",
            "default_review_prompt": "my custom prompt",
            "known_libraries": json.dumps(
                [{"url": "https://gitlab.example.com/group/lib", "ref": "v1", "path": "lib"}]
            ),
            "nightly_time": "03:15",
            "max_concurrent_reviews": "3",
            "poll_interval_seconds": "45",
            "post_results_to_gitlab": "true",
        },
    )
    assert resp.status_code == 200
    assert "settings saved" in resp.text.lower()

    row = get_settings_row(db)
    assert row.gitlab_url == "https://gitlab.example.com"
    assert row.gitlab_project == "group/proj"
    assert row.gitlab_token != "glpat-newtoken"  # encrypted at rest
    assert decrypt_secret(row.gitlab_token) == "glpat-newtoken"
    assert row.default_review_prompt == "my custom prompt"
    assert row.known_libraries == [
        {"url": "https://gitlab.example.com/group/lib", "ref": "v1", "path": "lib"}
    ]
    assert row.nightly_time == "03:15"
    assert row.max_concurrent_reviews == 3
    assert row.poll_interval_seconds == 45
    assert row.post_results_to_gitlab is True


def test_settings_save_empty_token_keeps_existing(authed, db):
    configure_row(db)
    resp = authed.post(
        "/settings",
        data={
            "gitlab_url": "https://gitlab.example.com",
            "gitlab_project": "group/proj",
            "gitlab_token": "",
            "default_review_prompt": "",
        },
    )
    assert resp.status_code == 200
    row = get_settings_row(db)
    assert decrypt_secret(row.gitlab_token) == "glpat-x"  # untouched


def test_settings_token_is_masked_on_read(authed, db):
    configure_row(db)
    resp = authed.get("/settings")
    assert "glpat-x" not in resp.text
    assert "leave empty to keep" in resp.text.lower()


def test_settings_validation_errors_re_render(authed, db):
    resp = authed.post(
        "/settings",
        data={
            "gitlab_url": "https://gitlab.example.com",
            "gitlab_project": "group/proj",
            "gitlab_token": "",
            "default_review_prompt": "",
            "known_libraries": "{not json",
            "nightly_time": "25:99",
            "max_concurrent_reviews": "2",
            "poll_interval_seconds": "30",
        },
    )
    assert resp.status_code == 200
    assert "valid json" in resp.text.lower()
    assert "hh:mm" in resp.text.lower()


def test_settings_bad_integer_re_renders(authed, db):
    resp = authed.post(
        "/settings",
        data={
            "gitlab_url": "",
            "gitlab_project": "",
            "gitlab_token": "",
            "default_review_prompt": "",
            "max_concurrent_reviews": "many",
            "poll_interval_seconds": "30",
        },
    )
    assert resp.status_code == 200
    assert "must be integers" in resp.text


def test_settings_test_connection_partial(authed, db, monkeypatch):
    configure_row(db)

    class FakeClient:
        def __init__(self, settings_row):
            self.row = settings_row

        def test_connection(self):
            if self.row.gitlab_url:
                return True, "Connected to group/proj on https://gitlab.example.com."
            return False, "nope"

    monkeypatch.setattr("app.routers.settings.GitLabClient", FakeClient)

    resp = authed.post("/settings/test-connection")
    assert resp.status_code == 200
    assert "Connected to group/proj" in resp.text

    # Submitted values take precedence over the saved row.
    resp = authed.post(
        "/settings/test-connection",
        data={"gitlab_url": "", "gitlab_project": ""},
    )
    # Both empty → fall back to the saved (configured) row → still OK.
    assert "Connected to group/proj" in resp.text
