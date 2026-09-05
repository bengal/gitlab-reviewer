"""Queue pump: claim queued jobs and execute them on daemon worker threads.

``pump_once()`` is what the scheduler's interval job calls (and what tests
call directly). It re-reads ``settings.max_concurrent_reviews`` on every
pump, then claims jobs one at a time: a ``BoundedSemaphore`` slot is
acquired *before* each claim, and the worker thread releases it when the
run finishes — so at most ``max_concurrent_reviews`` reviews run
concurrently. The pump blocks on the semaphore while the cap is saturated
and returns once the immediate queue is drained.

For each claimed job a ``review_run`` row is created (status ``running``,
``started_at``, no container id yet) and
``review_service.execute_job`` runs in a daemon thread; it persists the
outcome plus the job's final ``done``/``failed`` status and compacts queue
positions. A crashed worker thread is best-effort marked failed so a
single bad run cannot stall the queue.

``drain()`` joins all worker threads; it exists for tests (the production
scheduler never joins — workers are daemons).
"""

import logging
import threading
import time
from datetime import UTC, datetime

from app.db import get_db_session, get_settings_row
from app.models import JobStatus, ReviewRun, RunStatus, ScheduledJob
from app.services import review_service, scheduling

log = logging.getLogger(__name__)

_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def pump_once() -> list[int]:
    """Claim queued immediate jobs up to the concurrency cap and start one
    daemon worker thread per claimed job.

    Blocks while the cap is saturated (so a single pump drains the queue)
    and returns the ids of the review runs it started.
    """
    db = get_db_session()
    try:
        cap = max(1, int(get_settings_row(db).max_concurrent_reviews or 1))
    finally:
        db.close()
    semaphore = threading.BoundedSemaphore(cap)
    started: list[int] = []
    while True:
        semaphore.acquire()
        try:
            claimed = _claim_and_create_run()
        except Exception:
            log.exception("claim failed; stopping this pump")
            claimed = None
        if claimed is None:
            semaphore.release()
            break
        job_id, run_id = claimed
        thread = threading.Thread(
            target=_worker,
            args=(run_id, job_id, semaphore),
            name=f"review-worker-{run_id}",
            daemon=True,
        )
        with _threads_lock:
            _threads.append(thread)
        thread.start()
        started.append(run_id)
    return started


def _claim_and_create_run() -> tuple[int, int] | None:
    """Claim the lowest-position queued job and create its review_run row.

    Returns ``(job_id, run_id)`` or None when the queue is empty.
    """
    db = get_db_session()
    job: ScheduledJob | None = None
    try:
        job = scheduling.claim_next(db)
        if job is None:
            return None
        run = ReviewRun(
            scheduled_job_id=job.id,
            merge_request_id=job.merge_request_id,
            model_profile_id=job.model_profile_id,
            status=RunStatus.running.value,
            started_at=_now(),
            log="",
        )
        db.add(run)
        db.commit()
        return job.id, run.id
    except Exception:
        db.rollback()
        if job is not None:
            # the claim already flipped the job to `claimed`; abandon it so
            # the queue cannot get stuck on a half-started run
            _abandon_claim(job.id)
        raise
    finally:
        db.close()


def _abandon_claim(job_id: int) -> None:
    db = get_db_session()
    try:
        job = db.get(ScheduledJob, job_id)
        if job is not None and job.status == JobStatus.claimed.value:
            job.status = JobStatus.failed.value
            job.updated_at = _now()
            db.commit()
            scheduling.compact_positions(db)
    except Exception:
        log.exception("failed to abandon claim for job %s", job_id)
    finally:
        db.close()


def _worker(run_id: int, job_id: int, semaphore: threading.BoundedSemaphore) -> None:
    try:
        db = get_db_session()
        try:
            run = db.get(ReviewRun, run_id)
            job = db.get(ScheduledJob, job_id)
            if run is None or job is None:
                log.error("run %s or job %s vanished; nothing to execute", run_id, job_id)
                return
            review_service.execute_job(db, run, job)
        finally:
            db.close()
    except Exception:
        log.exception("review worker for run %s crashed", run_id)
        _finalize_crashed_run(run_id, job_id)
    finally:
        semaphore.release()
        _forget_thread()


def _finalize_crashed_run(run_id: int, job_id: int) -> None:
    """Best-effort cleanup so a crashed worker cannot stall the queue."""
    db = get_db_session()
    try:
        now = _now()
        run = db.get(ReviewRun, run_id)
        if run is not None and run.status == RunStatus.running.value:
            run.status = RunStatus.error.value
            run.error_message = "worker thread crashed; see app log"
            run.finished_at = now
        job = db.get(ScheduledJob, job_id)
        if job is not None and job.status in (
            JobStatus.claimed.value,
            JobStatus.running.value,
        ):
            job.status = JobStatus.failed.value
            job.updated_at = now
        db.commit()
        scheduling.compact_positions(db)
    except Exception:
        log.exception("failed to finalize crashed run %s", run_id)
    finally:
        db.close()


def drain(timeout: float | None = None) -> None:
    """Join every worker thread (test helper)."""
    with _threads_lock:
        threads = list(_threads)
    deadline = None if timeout is None else time.monotonic() + timeout
    for thread in threads:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        thread.join(remaining)


def _forget_thread() -> None:
    with _threads_lock:
        try:
            _threads.remove(threading.current_thread())
        except ValueError:
            pass
