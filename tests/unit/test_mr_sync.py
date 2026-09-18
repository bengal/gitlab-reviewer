"""MR sync: upsert semantics against a real tmp SQLite DB, fake GitLab client.

Covers: new rows created, changed fields updated, no duplicates on resync,
closed/unknown rows never deleted, and the error path returning a message
instead of raising.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.db import get_settings_row
from app.models import MergeRequest
from app.schemas.mr import MrSnapshot
from app.security import encrypt_secret
from app.services import mr_sync
from app.services.gitlab_client import GitLabError

UPDATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_snapshots(sha: str | None = None) -> list[MrSnapshot]:
    return [
        MrSnapshot(
            iid=1,
            title="First MR",
            author="alice",
            source_branch="feature-1",
            target_branch="main",
            sha=sha or "a" * 40,
            web_url="https://gitlab.example.com/group/proj/-/merge_requests/1",
            state="opened",
            updated_at=UPDATED_AT,
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
            updated_at=UPDATED_AT,
        ),
    ]


def install_fake_client(monkeypatch, snapshots=None, exc=None, default_branch="main", single=None):
    """Fake client; ``single`` maps iid -> snapshot for get_merge_request (the
    individual refetch used for MRs that left the open list)."""

    class FakeClient:
        def __init__(self, settings_row):
            pass

        def list_open_merge_requests(self):
            if exc is not None:
                raise exc
            return list(snapshots if snapshots is not None else make_snapshots())

        def get_project_default_branch(self):
            return default_branch

        def get_merge_request(self, iid):
            if single is not None and iid in single:
                return single[iid]
            for snap in (snapshots if snapshots is not None else make_snapshots()):
                if snap.iid == iid:
                    return snap
            raise GitLabError(f"HTTP 404 from https://gitlab.example.com: missing !{iid}")

    monkeypatch.setattr(mr_sync, "GitLabClient", FakeClient)


@pytest.fixture()
def configured(db, monkeypatch):
    row = get_settings_row(db)
    row.gitlab_url = "https://gitlab.example.com"
    row.gitlab_project = "group/proj"
    row.gitlab_token = encrypt_secret("glpat-x")
    db.commit()
    return row


def test_sync_creates_new_rows(db, configured, monkeypatch):
    install_fake_client(monkeypatch)

    added, updated, unchanged, error = mr_sync.sync_open_mrs(db)

    assert (added, updated, unchanged, error) == (2, 0, 0, None)
    rows = list(db.scalars(select(MergeRequest).order_by(MergeRequest.iid)))
    assert [r.iid for r in rows] == [1, 2]
    first = rows[0]
    assert first.project == "group/proj"
    assert first.title == "First MR"
    assert first.author == "alice"
    assert first.source_branch == "feature-1"
    assert first.target_branch == "main"
    assert first.sha == "a" * 40
    assert first.state == "opened"
    assert first.updated_at == UPDATED_AT.replace(tzinfo=None)
    assert first.last_seen_at is not None


def test_resync_is_idempotent(db, configured, monkeypatch):
    install_fake_client(monkeypatch)

    assert mr_sync.sync_open_mrs(db) == (2, 0, 0, None)
    assert mr_sync.sync_open_mrs(db) == (0, 0, 2, None)
    count = db.scalar(select(func.count()).select_from(MergeRequest))
    assert count == 2


def test_sync_updates_changed_fields(db, configured, monkeypatch):
    install_fake_client(monkeypatch)
    mr_sync.sync_open_mrs(db)

    changed = make_snapshots()
    changed[0].sha = "c" * 40
    changed[0].title = "First MR (revised)"
    install_fake_client(monkeypatch, snapshots=changed)

    added, updated, unchanged, error = mr_sync.sync_open_mrs(db)

    assert (added, updated, unchanged, error) == (0, 1, 1, None)
    first = db.scalar(select(MergeRequest).where(MergeRequest.iid == 1))
    assert first.sha == "c" * 40
    assert first.title == "First MR (revised)"
    count = db.scalar(select(func.count()).select_from(MergeRequest))
    assert count == 2


def test_sync_keeps_closed_rows(db, configured, monkeypatch):
    db.add(MergeRequest(project="group/proj", iid=99, title="old closed MR", state="closed"))
    db.commit()
    install_fake_client(monkeypatch)

    added, updated, unchanged, error = mr_sync.sync_open_mrs(db)

    assert (added, updated, unchanged, error) == (2, 0, 0, None)
    assert db.scalar(select(MergeRequest).where(MergeRequest.iid == 99)) is not None
    assert db.scalar(select(func.count()).select_from(MergeRequest)) == 3


def test_sync_updates_merged_mrs(db, configured, monkeypatch):
    # First sync caches !1 and !2 as opened.
    install_fake_client(monkeypatch)
    assert mr_sync.sync_open_mrs(db) == (2, 0, 0, None)

    # Upstream !2 gets merged, so it drops out of the open listing.
    merged = make_snapshots()[1]
    merged.state = "merged"
    merged.sha = "d" * 40
    merged.updated_at = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    install_fake_client(monkeypatch, snapshots=[make_snapshots()[0]], single={2: merged})

    added, updated, unchanged, error = mr_sync.sync_open_mrs(db)

    assert (added, updated, unchanged, error) == (0, 1, 1, None)
    second = db.scalar(select(MergeRequest).where(MergeRequest.iid == 2))
    assert second.state == "merged"
    assert second.sha == "d" * 40
    assert second.updated_at == datetime(2026, 2, 3, 4, 5, 6)
    # The still-open MR !1 is untouched.
    assert db.scalar(select(MergeRequest).where(MergeRequest.iid == 1)).state == "opened"


def test_sync_updates_closed_mrs(db, configured, monkeypatch):
    install_fake_client(monkeypatch)
    assert mr_sync.sync_open_mrs(db) == (2, 0, 0, None)

    # Upstream !1 gets closed and vanishes from the open listing.
    closed = make_snapshots()[0]
    closed.state = "closed"
    install_fake_client(monkeypatch, snapshots=[make_snapshots()[1]], single={1: closed})

    added, updated, unchanged, error = mr_sync.sync_open_mrs(db)

    assert (added, updated, unchanged, error) == (0, 1, 1, None)
    assert db.scalar(select(MergeRequest).where(MergeRequest.iid == 1)).state == "closed"


def test_sync_keeps_opened_state_when_refetch_fails(db, configured, monkeypatch):
    install_fake_client(monkeypatch)
    assert mr_sync.sync_open_mrs(db) == (2, 0, 0, None)
    first_seen = db.scalar(select(MergeRequest).where(MergeRequest.iid == 2)).last_seen_at

    # !2 is no longer in the open listing but its individual refetch fails;
    # the last-known snapshot (and state) must be kept, never failed the sync.
    install_fake_client(monkeypatch, snapshots=[make_snapshots()[0]], single={})
    added, updated, unchanged, error = mr_sync.sync_open_mrs(db)

    assert (added, updated, unchanged, error) == (0, 0, 1, None)
    second = db.scalar(select(MergeRequest).where(MergeRequest.iid == 2))
    assert second.state == "opened"
    assert second.last_seen_at == first_seen


def test_sync_error_returns_message_without_raising(db, configured, monkeypatch):
    install_fake_client(monkeypatch, exc=GitLabError("HTTP 401 from https://gitlab.example.com: bad token"))

    added, updated, unchanged, error = mr_sync.sync_open_mrs(db)

    assert (added, updated, unchanged) == (0, 0, 0)
    assert error is not None
    assert "401" in error
    assert db.scalar(select(func.count()).select_from(MergeRequest)) == 0


def test_sync_caches_project_default_branch(db, configured, monkeypatch):
    install_fake_client(monkeypatch, default_branch="trunk")

    mr_sync.sync_open_mrs(db)

    assert configured.gitlab_default_branch == "trunk"


def test_sync_keeps_default_branch_when_project_fetch_fails(db, configured, monkeypatch):
    configured.gitlab_default_branch = "main"
    db.commit()

    class FakeClient:
        def __init__(self, settings_row):
            pass

        def list_open_merge_requests(self):
            return make_snapshots()

        def get_project_default_branch(self):
            raise GitLabError("HTTP 500 from https://gitlab.example.com: boom")

    monkeypatch.setattr(mr_sync, "GitLabClient", FakeClient)

    added, updated, unchanged, error = mr_sync.sync_open_mrs(db)

    assert (added, updated, unchanged, error) == (2, 0, 0, None)
    assert configured.gitlab_default_branch == "main"  # stale value kept


def test_sync_not_configured_returns_hint(db, monkeypatch):
    install_fake_client(monkeypatch, exc=AssertionError("client must not be constructed"))

    added, updated, unchanged, error = mr_sync.sync_open_mrs(db)

    assert (added, updated, unchanged) == (0, 0, 0)
    assert error is not None
    assert "not configured" in error
