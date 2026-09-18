"""Sync open merge requests from GitLab into the merge_request cache table.

Rows are upserted keyed on (project, iid) and NEVER deleted: archived review
runs may reference MRs that have since been closed.
"""

from datetime import UTC, datetime

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_settings_row
from app.models import MergeRequest
from app.schemas.mr import MrSnapshot
from app.services.gitlab_client import GitLabClient, GitLabError

_SYNCED_FIELDS = (
    "title",
    "author",
    "source_branch",
    "target_branch",
    "sha",
    "web_url",
    "state",
    "updated_at",
)


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _store_value(field: str, value: object) -> object:
    """Normalize a snapshot field for storage (datetimes become naive UTC)."""
    if field == "updated_at" and isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _upsert_snapshot(db: Session, project: str, snap: MrSnapshot, now: datetime) -> str:
    """Upsert one snapshot onto the (project, iid) cache row.

    Returns "added", "updated" or "unchanged". The row's ``last_seen_at`` is
    stamped on every hit; rows are never deleted.
    """
    values = {field: _store_value(field, getattr(snap, field)) for field in _SYNCED_FIELDS}
    existing = db.scalar(
        select(MergeRequest).where(
            MergeRequest.project == project,
            MergeRequest.iid == snap.iid,
        )
    )
    if existing is None:
        db.add(
            MergeRequest(project=project, iid=snap.iid, last_seen_at=now, **values)
        )
        return "added"
    changed = any(getattr(existing, field) != value for field, value in values.items())
    if changed:
        for field, value in values.items():
            setattr(existing, field, value)
    existing.last_seen_at = now
    return "updated" if changed else "unchanged"


def sync_open_mrs(db: Session) -> tuple[int, int, int, str | None]:
    """Fetch open MRs and upsert merge_request rows.

    Returns ``(added, updated, unchanged, error_message)`` — error_message is
    None on success; GitLab or transport failures are reported here, never
    raised, so the UI can show them inline.

    The listing endpoint only returns *open* MRs, so an MR that was merged or
    closed upstream vanishes from that list and would otherwise keep its stale
    ``state="opened"`` snapshot forever. After upserting the open MRs we
    therefore re-fetch (by iid) any cached row still marked "opened" that was
    not in the listing, so merged/closed transitions are captured and reflected
    in the UI. A row whose individual refetch fails keeps its last-known
    snapshot; transient errors never fail the sync.
    """
    row = get_settings_row(db)
    if not (row.gitlab_url.strip() and row.gitlab_project.strip() and row.gitlab_token.strip()):
        return (0, 0, 0, "GitLab is not configured yet — set the URL, project and token in Settings.")

    client = GitLabClient(row)
    try:
        snapshots = client.list_open_merge_requests()
    except (GitLabError, requests.RequestException) as exc:
        return (0, 0, 0, str(exc))

    # Cache the project's default branch for the list view (it hides a
    # redundant "→ <default>" target); a failure here never fails the sync.
    try:
        row.gitlab_default_branch = client.get_project_default_branch()
    except (GitLabError, requests.RequestException):
        pass

    added = updated = unchanged = 0
    now = _utcnow_naive()
    seen_iids = set()
    for snap in snapshots:
        seen_iids.add(snap.iid)
        outcome = _upsert_snapshot(db, row.gitlab_project, snap, now)
        if outcome == "added":
            added += 1
        elif outcome == "updated":
            updated += 1
        else:
            unchanged += 1

    # Cached rows we last saw as opened but that are no longer in the open
    # listing: merged/closed upstream. Refetch each individually to learn its
    # actual state (bounded by how many MRs changed since the last sync).
    stale = list(
        db.scalars(
            select(MergeRequest).where(
                MergeRequest.project == row.gitlab_project,
                MergeRequest.state == "opened",
            )
        )
    )
    for mr in stale:
        if mr.iid in seen_iids:
            continue
        try:
            snap = client.get_merge_request(mr.iid)
        except (GitLabError, requests.RequestException):
            continue
        outcome = _upsert_snapshot(db, row.gitlab_project, snap, now)
        if outcome == "updated":
            updated += 1
        else:
            unchanged += 1

    db.commit()
    return (added, updated, unchanged, None)
