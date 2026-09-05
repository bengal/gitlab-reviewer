"""Queue scheduling: enqueue, reorder, cancel, nightly set, claim, compact.

Position semantics: ``position`` orders *queued* jobs within their
``schedule_type`` set (1 = first, unique among queued jobs of that type).
The pump always claims the lowest-position queued immediate job, so
reordering takes effect for everything still queued. Cancels leave gaps;
``compact_positions`` renumbers.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import JobStatus, ScheduledJob, ScheduleType


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _queued_positions(db: Session, schedule_type: ScheduleType) -> list[int]:
    return list(
        db.scalars(
            select(ScheduledJob.id)
            .where(
                ScheduledJob.schedule_type == schedule_type,
                ScheduledJob.status == JobStatus.queued.value,
            )
            .order_by(ScheduledJob.position, ScheduledJob.id)
        )
    )


def _next_position(db: Session, schedule_type: ScheduleType) -> int:
    top = db.scalar(
        select(func.max(ScheduledJob.position)).where(
            ScheduledJob.schedule_type == schedule_type,
            ScheduledJob.status == JobStatus.queued.value,
        )
    )
    return (top or 0) + 1


def enqueue(
    db: Session,
    *,
    mr,
    profile,
    schedule_type: ScheduleType,
    prompt_override: str | None = None,
    extra_projects: list[dict] | None = None,
    post_to_gitlab: bool | None = None,
) -> ScheduledJob:
    """Create a queued job at the end of its schedule_type set."""
    schedule_type = ScheduleType(schedule_type)
    prompt_override = prompt_override.strip() if isinstance(prompt_override, str) else None
    job = ScheduledJob(
        merge_request_id=mr.id,
        model_profile_id=profile.id,
        prompt_override=prompt_override or None,
        extra_projects=list(extra_projects or []),
        schedule_type=schedule_type,
        position=_next_position(db, schedule_type),
        status=JobStatus.queued.value,
        post_to_gitlab=post_to_gitlab,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(job)
    db.commit()
    return job


def _rewrite_positions(db: Session, ordered_job_ids: list[int]) -> None:
    """Assign position 1..n to exactly the queued immediate jobs, in the given order.

    Raises ValueError when an id is unknown, not queued, not immediate, or when
    the list is not exactly the current set of queued immediate jobs (the queue
    changed between render and submit).
    """
    ordered = list(dict.fromkeys(ordered_job_ids))
    if not ordered:
        raise ValueError("no job ids to reorder")
    jobs = {
        job.id: job
        for job in db.scalars(
            select(ScheduledJob).where(
                ScheduledJob.id.in_(ordered),
                ScheduledJob.status == JobStatus.queued.value,
                ScheduledJob.schedule_type == ScheduleType.immediate.value,
            )
        )
    }
    if len(jobs) != len(ordered):
        raise ValueError("unknown job id, or a job is no longer queued")
    if set(jobs) != set(_queued_positions(db, ScheduleType.immediate)):
        raise ValueError("the queue changed while you were reordering — reload and try again")
    for position, job_id in enumerate(ordered, start=1):
        jobs[job_id].position = position
    db.commit()


def reorder(db: Session, ordered_job_ids: list[int]) -> None:
    """Transactionally rewrite positions 1..n for queued immediate jobs."""
    _rewrite_positions(db, ordered_job_ids)


def _require_queued_immediate(job: ScheduledJob) -> None:
    if job.status != JobStatus.queued:
        raise ValueError(f"job {job.id} is not queued (status: {job.status})")
    if job.schedule_type != ScheduleType.immediate:
        raise ValueError(f"job {job.id} is not in the immediate queue")


def move_to_top(db: Session, job: ScheduledJob) -> None:
    """Move a queued immediate job to position 1 (others shift down)."""
    _require_queued_immediate(job)
    ordered = [job.id] + [i for i in _queued_positions(db, ScheduleType.immediate) if i != job.id]
    _rewrite_positions(db, ordered)


def move_to_bottom(db: Session, job: ScheduledJob) -> None:
    """Move a queued immediate job to the end of the queue."""
    _require_queued_immediate(job)
    ordered = [i for i in _queued_positions(db, ScheduleType.immediate) if i != job.id] + [job.id]
    _rewrite_positions(db, ordered)


def cancel(db: Session, job: ScheduledJob) -> None:
    """Cancel a queued job (any schedule type) and compact the immediate queue."""
    if job.status != JobStatus.queued:
        raise ValueError(f"job {job.id} is not queued (status: {job.status})")
    job.status = JobStatus.cancelled.value
    job.updated_at = _now()
    db.commit()
    compact_positions(db)


def enroll_nightly(db: Session, job: ScheduledJob) -> None:
    """Switch a queued job from the immediate queue into the nightly set."""
    if job.status != JobStatus.queued:
        raise ValueError(f"job {job.id} is not queued (status: {job.status})")
    if job.schedule_type == ScheduleType.nightly:
        return
    # position is computed before the flip so the job is not counted in its
    # new set
    position = _next_position(db, ScheduleType.nightly)
    job.schedule_type = ScheduleType.nightly
    job.position = position
    job.updated_at = _now()
    db.commit()


def remove_nightly(db: Session, job: ScheduledJob) -> None:
    """Switch a queued nightly job back into the immediate queue (at the end)."""
    if job.status != JobStatus.queued:
        raise ValueError(f"job {job.id} is not queued (status: {job.status})")
    if job.schedule_type != ScheduleType.nightly:
        raise ValueError(f"job {job.id} is not in the nightly set")
    position = _next_position(db, ScheduleType.immediate)
    job.schedule_type = ScheduleType.immediate
    job.position = position
    job.updated_at = _now()
    db.commit()


def list_nightly(db: Session) -> list[ScheduledJob]:
    """Queued nightly jobs, ordered by position."""
    return list(
        db.scalars(
            select(ScheduledJob)
            .where(
                ScheduledJob.schedule_type == ScheduleType.nightly,
                ScheduledJob.status == JobStatus.queued.value,
            )
            .order_by(ScheduledJob.position, ScheduledJob.id)
        )
    )


def promote_nightly(db: Session) -> list[ScheduledJob]:
    """Flip all queued nightly jobs into the immediate queue with fresh positions.

    The promoted jobs are appended after the current immediate queue, keeping
    their relative nightly order. Called by the nightly cron (M6).
    """
    nightlies = list_nightly(db)
    if not nightlies:
        return []
    base = db.scalar(
        select(func.max(ScheduledJob.position)).where(
            ScheduledJob.schedule_type == ScheduleType.immediate,
            ScheduledJob.status == JobStatus.queued.value,
        )
    ) or 0
    now = _now()
    for offset, job in enumerate(nightlies, start=1):
        job.schedule_type = ScheduleType.immediate
        job.position = base + offset
        job.updated_at = now
    db.commit()
    return nightlies


def compact_positions(db: Session) -> None:
    """Renumber queued immediate jobs to dense 1..n (after cancels/completions)."""
    jobs = list(
        db.scalars(
            select(ScheduledJob)
            .where(
                ScheduledJob.schedule_type == ScheduleType.immediate,
                ScheduledJob.status == JobStatus.queued.value,
            )
            .order_by(ScheduledJob.position, ScheduledJob.id)
        )
    )
    for position, job in enumerate(jobs, start=1):
        if job.position != position:
            job.position = position
    db.commit()


def claim_next(db: Session) -> ScheduledJob | None:
    """Atomically claim the lowest-position queued immediate job.

    Postgres: SELECT ... FOR UPDATE SKIP LOCKED inside the session transaction.
    SQLite: BEGIN IMMEDIATE on a raw connection so the select+update run as one
    write-locked transaction (no Postgres-specific SQL is sent to SQLite).

    Returns the job (status already flipped to ``claimed``) or None when the
    queue is empty. Call on a session that has not previously loaded the job
    (the worker opens a fresh session per claim).
    """
    if db.bind.dialect.name == "postgresql":
        return _claim_postgres(db)
    return _claim_sqlite(db)


def _claim_postgres(db: Session) -> ScheduledJob | None:
    job = db.scalars(
        select(ScheduledJob)
        .where(
            ScheduledJob.schedule_type == ScheduleType.immediate,
            ScheduledJob.status == JobStatus.queued.value,
        )
        .order_by(ScheduledJob.position, ScheduledJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        db.rollback()
        return None
    job.status = JobStatus.claimed.value
    job.updated_at = _now()
    db.commit()
    return job


def _claim_sqlite(db: Session) -> ScheduledJob | None:
    table = ScheduledJob.__table__
    engine = db.bind
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                select(table.c.id)
                .where(
                    table.c.schedule_type == ScheduleType.immediate,
                    table.c.status == JobStatus.queued.value,
                )
                .order_by(table.c.position, table.c.id)
                .limit(1)
            ).first()
            if row is None:
                conn.exec_driver_sql("COMMIT")
                return None
            claimed = conn.execute(
                update(table)
                .where(table.c.id == row.id, table.c.status == JobStatus.queued.value)
                .values(status=JobStatus.claimed.value, updated_at=_now())
            ).rowcount
            conn.exec_driver_sql("COMMIT")
        except Exception:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:
                pass
            raise
    if claimed != 1:
        return None
    return db.get(ScheduledJob, row.id, populate_existing=True)
