"""Execute one claimed review job (PLAN steps 6-8, M6a).

``execute_job`` is called by the worker thread with a session plus the
``review_run`` and ``scheduled_job`` rows. It builds the container env via
``app.orchestrator.run_review.build_env``, runs
``app.state.orchestrator.run_review`` with a ``log_chunk`` that scrubs
secrets (the env's secret values) before appending to ``run.log``, maps the
``RunOutcome`` onto the run's final state, and finalizes the job.

The GitLab post-back (PLAN step 7) is deliberately NOT implemented here —
see the clearly marked hook point in ``execute_job`` (M6b).
"""

import logging
import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db import get_settings_row
from app.models import JobStatus, MergeRequest, ModelProfile, ReviewRun, RunStatus, ScheduledJob
from app.orchestrator import RunOutcome
from app.orchestrator.run_review import build_env, scrub_secrets, secret_env_values
from app.scheduler.state import get_app
from app.services.scheduling import compact_positions

log = logging.getLogger(__name__)

# podman prints the container id as the first stdout line of `podman run`;
# anything else (warning banners, log output) is skipped.
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _extract_container_id(lines: list[str]) -> str | None:
    """First log line that is a 64-hex container id, or None."""
    for line in lines:
        candidate = line.strip()
        if _CONTAINER_ID.match(candidate):
            return candidate
    return None


def execute_job(db: Session, run: ReviewRun, job: ScheduledJob) -> None:
    """Run one claimed job to completion and persist the outcome.

    Persists the run result and the job's final status in a single commit,
    then compacts queue positions. Never raises for a failed review — only
    for unexpected infrastructure errors (the worker turns those into a
    ``failed`` job too).
    """
    orchestrator = get_app().state.orchestrator

    run.status = RunStatus.running.value
    run.started_at = run.started_at or _now()
    job.status = JobStatus.running.value
    job.updated_at = _now()
    db.commit()

    settings_row = get_settings_row(db)
    profile = db.get(ModelProfile, job.model_profile_id)
    mr = db.get(MergeRequest, job.merge_request_id)
    if profile is None or mr is None:
        _apply_outcome(
            db,
            run,
            job,
            RunOutcome(exit_code=1, error="merge request or model profile missing"),
        )
        return

    env = build_env(job, profile, settings_row, mr=mr)
    secrets = secret_env_values(env)
    log_lines: list[str] = []

    def log_chunk(chunk: str) -> None:
        log_lines.append(scrub_secrets(chunk, secrets))

    try:
        outcome = orchestrator.run_review(run, job, log_chunk=log_chunk)
    except Exception as exc:
        log.exception("orchestrator raised for run %s", run.id)
        outcome = RunOutcome(exit_code=1, error=f"orchestrator crashed: {exc!r}")

    # Defense in depth: orchestrators may stream un-scrubbed content
    # (e.g. the fake), so scrub every stored text field again.
    if outcome.error:
        outcome.error = scrub_secrets(outcome.error, secrets)
    if outcome.result_markdown:
        outcome.result_markdown = scrub_secrets(outcome.result_markdown, secrets)
    container_id = _extract_container_id(log_lines)
    if container_id:
        run.container_id = container_id
    if log_lines:
        run.log = "\n".join(log_lines) + "\n"

    _apply_outcome(db, run, job, outcome)


def _apply_outcome(db: Session, run: ReviewRun, job: ScheduledJob, outcome: RunOutcome) -> None:
    """Map a RunOutcome onto the run/job rows and commit."""
    if outcome.timed_out:
        status = RunStatus.timeout.value
    elif outcome.exit_code == 0:
        # success even when result_json is null (raw markdown fallback, or
        # neither — the run itself completed)
        status = RunStatus.success.value
    else:
        status = RunStatus.error.value

    run.status = status
    run.exit_code = outcome.exit_code
    run.result_json = outcome.result_json
    run.result_markdown = outcome.result_markdown
    run.error_message = outcome.error
    run.finished_at = _now()
    job.status = JobStatus.done.value if status == RunStatus.success.value else JobStatus.failed.value
    job.updated_at = run.finished_at

    # ------------------------------------------------------------------
    # GITLAB POST-BACK HOOK POINT (M6b — implement here, before the commit
    # below so the note id lands in the same transaction):
    # when `job.post_to_gitlab` is not None, else
    # `settings_row.post_results_to_gitlab` — render the outcome
    # (result_json or result_markdown) into markdown, post it as an MR note
    # via gitlab_client.post_note, and store the returned id on
    # `run.gitlab_note_id`. A failed post must log, not fail the run.
    # ------------------------------------------------------------------

    db.commit()
    compact_positions(db)
