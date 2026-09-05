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


def sync_open_mrs(db: Session) -> tuple[int, int, int, str | None]:
    """Fetch open MRs and upsert merge_request rows.

    Returns ``(added, updated, unchanged, error_message)`` — error_message is
    None on success; GitLab or transport failures are reported here, never
    raised, so the UI can show them inline.
    """
    row = get_settings_row(db)
    if not (row.gitlab_url.strip() and row.gitlab_project.strip() and row.gitlab_token.strip()):
        return (0, 0, 0, "GitLab is not configured yet — set the URL, project and token in Settings.")

    client = GitLabClient(row)
    try:
        snapshots = client.list_open_merge_requests()
    except (GitLabError, requests.RequestException) as exc:
        return (0, 0, 0, str(exc))

    added = updated = unchanged = 0
    now = _utcnow_naive()
    for snap in snapshots:
        values = {field: _store_value(field, getattr(snap, field)) for field in _SYNCED_FIELDS}
        existing = db.scalar(
            select(MergeRequest).where(
                MergeRequest.project == row.gitlab_project,
                MergeRequest.iid == snap.iid,
            )
        )
        if existing is None:
            db.add(
                MergeRequest(
                    project=row.gitlab_project,
                    iid=snap.iid,
                    last_seen_at=now,
                    **values,
                )
            )
            added += 1
            continue
        changed = any(getattr(existing, field) != value for field, value in values.items())
        if changed:
            for field, value in values.items():
                setattr(existing, field, value)
            updated += 1
        else:
            unchanged += 1
        existing.last_seen_at = now
    db.commit()
    return (added, updated, unchanged, None)
