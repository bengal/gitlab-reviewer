"""Execute one claimed review job (PLAN steps 6-8, M6a/M6b).

``execute_job`` is called by the worker thread with a session plus the
``review_run`` and ``scheduled_job`` rows. It builds the container env via
``app.orchestrator.run_review.build_env``, runs
``app.state.orchestrator.run_review`` with a ``log_chunk`` that scrubs
secrets (the env's secret values) before appending to ``run.log``, maps the
``RunOutcome`` onto the run's final state, and finalizes the job.

GitLab post-back (PLAN step 7, M6b): when the run succeeded and the job's
``post_to_gitlab`` flag (falling back to ``settings.post_results_to_gitlab``
when the flag is None) is true, the rendered review is posted as an MR note
via ``GitLabClient.post_note`` and the returned note id is stored on
``run.gitlab_note_id`` in the same transaction as the outcome. A failed
post is logged and appended to ``run.log`` but never fails the run.
"""

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_settings_row
from app.models import JobStatus, MergeRequest, ModelProfile, ReviewRun, RunStatus, ScheduledJob
from app.orchestrator import RunOutcome
from app.orchestrator.run_review import build_env, scrub_json, scrub_secrets, secret_env_values
from app.scheduler.state import get_app
from app.services.gitlab_client import GitLabClient
from app.services.result_render import render_result_markdown
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
            settings_row=settings_row,
            mr=None,
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

    # Defense in depth: orchestrators may deliver un-scrubbed content
    # (e.g. the fake, or an LLM that echoes a secret into its JSON result),
    # so scrub every stored text field again.
    if outcome.error:
        outcome.error = scrub_secrets(outcome.error, secrets)
    if outcome.result_markdown:
        outcome.result_markdown = scrub_secrets(outcome.result_markdown, secrets)
    if outcome.result_json is not None:
        outcome.result_json = scrub_json(outcome.result_json, secrets)
    container_id = _extract_container_id(log_lines)
    if container_id:
        run.container_id = container_id
    if log_lines:
        run.log = "\n".join(log_lines) + "\n"

    _apply_outcome(db, run, job, outcome, settings_row=settings_row, mr=mr)


def cancel_inflight(db: Session, job: ScheduledJob) -> str:
    """Cancel a claimed/running job: stop its container (best effort) and
    finalize its in-flight run and the job row.

    If the worker thread for this run is still alive in this process it will
    shortly persist the container's own exit (an error, since we just stopped
    it); both outcomes are terminal, so a cancel can never leave the job
    stuck. Returns a short status line for the UI.
    """
    if job.status not in (JobStatus.claimed.value, JobStatus.running.value):
        raise ValueError(f"job {job.id} is not in flight (status: {job.status})")
    run = db.scalar(
        select(ReviewRun)
        .where(
            ReviewRun.scheduled_job_id == job.id,
            ReviewRun.status == RunStatus.running.value,
        )
        .order_by(ReviewRun.id.desc())
        .limit(1)
    )
    if run is not None:
        try:
            get_app().state.orchestrator.stop_run_container(run.id)
        except Exception:
            log.exception("could not stop container for run %s on cancel", run.id)
        run.status = RunStatus.error.value
        run.error_message = "cancelled by user"
        run.finished_at = _now()
    job.status = JobStatus.cancelled.value
    job.updated_at = _now()
    db.commit()
    compact_positions(db)
    if run is not None:
        return "Running review cancelled."
    return "Job cancelled."


def _apply_outcome(
    db: Session,
    run: ReviewRun,
    job: ScheduledJob,
    outcome: RunOutcome,
    *,
    settings_row,
    mr: MergeRequest | None,
) -> None:
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

    # GitLab post-back (PLAN step 7): optional and best-effort — it happens
    # before the commit so the note id lands in the same transaction as the
    # outcome.
    if status == RunStatus.success.value:
        post = (
            job.post_to_gitlab
            if job.post_to_gitlab is not None
            else settings_row.post_results_to_gitlab
        )
        if post:
            _post_to_gitlab(run, outcome, settings_row, mr)

    db.commit()
    compact_positions(db)


def _post_to_gitlab(run: ReviewRun, outcome: RunOutcome, settings_row, mr) -> None:
    """Post the rendered review as an MR note and store the note id on the run.

    Any failure (GitLab not configured, HTTP error, ...) is logged and
    appended to ``run.log`` — it must never fail a run that succeeded. The
    mutated run row is committed by the caller's ``db.commit()``.
    """
    if mr is None:
        log.error("run %s: cannot post to GitLab (merge request row missing)", run.id)
        run.log = _append_log(run.log, "GitLab post-back skipped: merge request row missing")
        return
    body = render_result_markdown(outcome.result_json, outcome.result_markdown, run)
    try:
        note_id = GitLabClient(settings_row).post_note(mr.iid, body)
    except Exception as exc:
        log.exception("run %s: GitLab post-back failed for !%s", run.id, mr.iid)
        run.log = _append_log(run.log, f"GitLab post-back failed: {exc}")
        return
    run.gitlab_note_id = note_id
    run.log = _append_log(run.log, f"Posted review to GitLab !{mr.iid} as note {note_id}")


def _append_log(log_text: str | None, line: str) -> str:
    """Append one line to the run log, keeping it newline-terminated."""
    base = (log_text or "").rstrip("\n")
    return f"{base}\n{line}\n" if base else f"{line}\n"
