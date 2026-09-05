"""Merge request snapshot carried between the GitLab client, the sync service
and the UI."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def parse_gitlab_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from GitLab (``...Z`` suffix) to UTC; None on junk."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass
class MrSnapshot:
    """Normalized view of a GitLab merge request as returned by the API."""

    iid: int
    title: str
    author: str
    source_branch: str
    target_branch: str
    sha: str
    web_url: str
    state: str
    updated_at: datetime | None

    @classmethod
    def from_gitlab(cls, item: dict[str, Any]) -> "MrSnapshot":
        """Map a GitLab MR API object onto a snapshot; missing keys degrade gracefully."""
        author = item.get("author") or {}
        diff_refs = item.get("diff_refs") or {}
        return cls(
            iid=int(item.get("iid") or 0),
            title=item.get("title") or "",
            author=author.get("username") or author.get("name") or "",
            source_branch=item.get("source_branch") or "",
            target_branch=item.get("target_branch") or "",
            sha=item.get("sha") or diff_refs.get("head_sha") or "",
            web_url=item.get("web_url") or "",
            state=item.get("state") or "opened",
            updated_at=parse_gitlab_datetime(item.get("updated_at")),
        )
