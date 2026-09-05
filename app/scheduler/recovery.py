"""Startup recovery: finalize reviews orphaned by a previous process.

Reviews execute on in-process daemon worker threads (``worker.pump_once``),
so a restart or crash leaves ``review_run`` rows stuck in ``running`` and
their jobs in ``claimed``/``running``: the pump only ever claims ``queued``
jobs and the per-run timeout lives in the dead worker thread, so nothing
else finalizes them and the queue UI shows a phantom running job forever.

``recover_orphans`` runs once at startup (the FastAPI lifespan, before the
scheduler starts). It stops any still-alive container for each orphaned run
(best effort), marks the run ``error``, fails its job, fails any leftover
stuck job without a run row, and compacts queue positions. The app is
single-instance by design — a run cannot outlive its process.
"""

import logging
from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy import select

from app.db import get_db_session
from app.models import JobStatus, ReviewRun, RunStatus, ScheduledJob
from app.services import scheduling

log = logging.getLogger(__name__)


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def recover_orphans(app: FastAPI) -> int:
    """Finalize in-flight review runs and jobs left behind by a previous
    process. Returns the number of rows fixed (0 on a clean start).
    """
    db = get_db_session()
    fixed = 0
    try:
        now = _now()
        orphans = list(
            db.scalars(select(ReviewRun).where(ReviewRun.status == RunStatus.running.value)).all()
        )
        for run in orphans:
            try:
                app.state.orchestrator.stop_run_container(run.id)
            except Exception:
                log.exception("could not stop container for orphaned run %s", run.id)
            run.status = RunStatus.error.value
            run.error_message = "interrupted: the app restarted while the review was running"
            run.finished_at = now
            job = db.get(ScheduledJob, run.scheduled_job_id)
            if job is not None and job.status in (
                JobStatus.claimed.value,
                JobStatus.running.value,
            ):
                job.status = JobStatus.failed.value
                job.updated_at = now
            fixed += 1
        # A crash between the job claim and the run-row commit can leave a
        # stuck job with no run at all; fail whatever is still claimed/running.
        db.flush()
        stuck = list(
            db.scalars(
                select(ScheduledJob).where(
                    ScheduledJob.status.in_(
                        (JobStatus.claimed.value, JobStatus.running.value)
                    )
                )
            ).all()
        )
        for job in stuck:
            job.status = JobStatus.failed.value
            job.updated_at = now
            fixed += 1
        if fixed:
            db.commit()
            scheduling.compact_positions(db)
            log.warning(
                "recovered %d row(s) left in-flight by a previous process "
                "(%d run(s), %d job(s))",
                fixed,
                len(orphans),
                len(stuck),
            )
        return fixed
    except Exception:
        db.rollback()
        log.exception("startup recovery failed")
        raise
    finally:
        db.close()
