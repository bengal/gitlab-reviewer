"""The reorderable review queue."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ScheduleType(StrEnum):
    immediate = "immediate"
    nightly = "nightly"


class JobStatus(StrEnum):
    queued = "queued"
    claimed = "claimed"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class ScheduledJob(Base):
    """A review enqueued against an MR.

    position orders queued jobs within their schedule_type set (1 = first);
    the queue pump always claims the lowest-position queued immediate job.
    post_to_gitlab=None means "fall back to the settings default".
    """

    __tablename__ = "scheduled_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    merge_request_id: Mapped[int] = mapped_column(
        ForeignKey("merge_request.id"), nullable=False, index=True
    )
    model_profile_id: Mapped[int] = mapped_column(
        ForeignKey("model_profile.id"), nullable=False, index=True
    )
    prompt_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_projects: Mapped[list] = mapped_column(JSON, default=list)
    schedule_type: Mapped[str] = mapped_column(
        SAEnum(ScheduleType, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        default=ScheduleType.immediate.value,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        SAEnum(JobStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        default=JobStatus.queued.value,
    )
    post_to_gitlab: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
