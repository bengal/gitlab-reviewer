"""A single review execution (one ephemeral container run)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RunStatus(StrEnum):
    running = "running"
    success = "success"
    error = "error"
    timeout = "timeout"


class ReviewRun(Base):
    """Result of executing a scheduled job.

    Archiving is non-destructive: archived_at is set to a timestamp and the
    archive view filters on archived_at IS NOT NULL.
    """

    __tablename__ = "review_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    scheduled_job_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_job.id"), nullable=False, index=True
    )
    merge_request_id: Mapped[int] = mapped_column(
        ForeignKey("merge_request.id"), nullable=False, index=True
    )
    model_profile_id: Mapped[int] = mapped_column(
        ForeignKey("model_profile.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        SAEnum(RunStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        default=RunStatus.running.value,
    )
    container_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # opencode's full session export (opencode export <sessionID>): the model's
    # reasoning ("thinking") blocks, tool calls and transcript — captured by
    # the review-runner entrypoint and downloadable from the run detail page.
    session_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    gitlab_note_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
