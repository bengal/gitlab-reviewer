"""Cached snapshot of GitLab merge requests."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MergeRequest(Base):
    """Last-seen snapshot of an MR; upserted by MR sync, never deleted
    (archived review runs may reference closed MRs)."""

    __tablename__ = "merge_request"
    __table_args__ = (UniqueConstraint("project", "iid", name="uq_merge_request_project_iid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project: Mapped[str] = mapped_column(String(512), nullable=False)
    iid: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(1000), default="")
    author: Mapped[str] = mapped_column(String(300), default="")
    source_branch: Mapped[str] = mapped_column(String(512), default="")
    target_branch: Mapped[str] = mapped_column(String(512), default="")
    sha: Mapped[str] = mapped_column(String(64), default="")
    web_url: Mapped[str] = mapped_column(String(1000), default="")
    state: Mapped[str] = mapped_column(String(50), default="opened")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
