"""Background scheduling: the interval queue pump and the nightly cron.

``init_scheduler(app)`` creates an APScheduler ``BackgroundScheduler`` and
stores it on ``app.state.scheduler`` (or None when DISABLE_SCHEDULER is
set). Start/stop is driven by the FastAPI lifespan in ``create_app``.

Jobs:

- ``review-pump`` — interval job, period = ``settings.poll_interval_seconds``.
  The setting is re-read on *every* tick and the job reschedules itself
  when the value changes, so edits from the Settings UI apply without a
  restart. Each tick calls ``worker.pump_once()``.
- ``nightly-promote`` — cron job at ``settings.nightly_time`` (HH:MM),
  re-read by the pump tick and rescheduled on change. Each fire calls
  ``scheduling.promote_nightly()``.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from app.db import get_db_session, get_settings_row
from app.scheduler import worker
from app.services import scheduling

log = logging.getLogger(__name__)

PUMP_JOB_ID = "review-pump"
NIGHTLY_JOB_ID = "nightly-promote"


def init_scheduler(app: FastAPI) -> BackgroundScheduler | None:
    """Create the background scheduler on the app (jobs added, not started).

    Returns the scheduler, or None when DISABLE_SCHEDULER is set. The
    caller (the lifespan in create_app) starts it and shuts it down.
    """
    from app.config import get_settings

    if get_settings().disable_scheduler:
        log.info("DISABLE_SCHEDULER set; background scheduler not created")
        app.state.scheduler = None
        return None

    db = get_db_session()
    try:
        row = get_settings_row(db)
    finally:
        db.close()

    scheduler = BackgroundScheduler(daemon=True)
    app.state.scheduler = scheduler
    # Last-seen settings values, so the ticks know when to reschedule.
    # (CronTrigger has no reliable equality, so the nightly time is tracked
    # as its raw string.)
    scheduler.nm_state = {
        "poll_interval": _safe_interval(row.poll_interval_seconds),
        "nightly_time": row.nightly_time,
    }
    scheduler.add_job(
        _pump_tick,
        IntervalTrigger(seconds=scheduler.nm_state["poll_interval"]),
        id=PUMP_JOB_ID,
        kwargs={"app": app},
        max_instances=1,
        coalesce=True,
    )
    _sync_nightly_job(scheduler, row.nightly_time, app)
    return scheduler


def _pump_tick(app: FastAPI) -> None:
    """One interval tick: re-read settings (reschedule on change), then pump."""
    scheduler = app.state.scheduler
    db = get_db_session()
    try:
        row = get_settings_row(db)
    finally:
        db.close()

    state = scheduler.nm_state
    interval = _safe_interval(row.poll_interval_seconds)
    if interval != state["poll_interval"]:
        state["poll_interval"] = interval
        scheduler.reschedule_job(PUMP_JOB_ID, trigger=IntervalTrigger(seconds=interval))
    if row.nightly_time != state["nightly_time"]:
        state["nightly_time"] = row.nightly_time
        _sync_nightly_job(scheduler, row.nightly_time, app)

    try:
        worker.pump_once()
    except Exception:
        log.exception("queue pump tick failed")


def _nightly_tick(app: FastAPI) -> None:
    """Cron fire: promote the nightly set into the immediate queue."""
    db = get_db_session()
    try:
        promoted = scheduling.promote_nightly(db)
    finally:
        db.close()
    if promoted:
        log.info("nightly promotion moved %d job(s) into the immediate queue", len(promoted))


def _sync_nightly_job(scheduler: BackgroundScheduler, nightly_time: str, app: FastAPI) -> None:
    """Add or reschedule the nightly cron job to match ``nightly_time``.

    Only called when the value changed (or at startup), so an existing job
    is always rescheduled rather than compared.
    """
    trigger = _nightly_trigger(nightly_time)
    existing = scheduler.get_job(NIGHTLY_JOB_ID)
    if trigger is None:
        if existing is not None:
            scheduler.remove_job(NIGHTLY_JOB_ID)
        log.warning("invalid nightly_time %r; nightly promotion disabled", nightly_time)
        return
    if existing is None:
        scheduler.add_job(
            _nightly_tick,
            trigger,
            id=NIGHTLY_JOB_ID,
            kwargs={"app": app},
            max_instances=1,
            coalesce=True,
        )
    else:
        scheduler.reschedule_job(NIGHTLY_JOB_ID, trigger=trigger)


def _nightly_trigger(nightly_time: str | None) -> CronTrigger | None:
    """CronTrigger for an ``HH:MM`` string, or None when the value is invalid."""
    try:
        hours, minutes = str(nightly_time).strip().split(":")
        hour, minute = int(hours), int(minutes)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return CronTrigger(hour=hour, minute=minute)


def _safe_interval(value: object) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 30
