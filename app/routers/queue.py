"""Queue routes: queue browser, reorder (HTMX/Sortable), cancel/top/bottom,
nightly management, and the per-MR schedule form.

All mutating routes re-render an HTML partial for HTMX; every action also
works as a plain form POST (full page response), so the UI stays usable
without JavaScript.
"""

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_settings_row
from app.deps import get_db
from app.models import JobStatus, MergeRequest, ModelProfile, ScheduledJob, ScheduleType
from app.routers.mrs import detail_context
from app.services import review_service, scheduling

router = APIRouter()

_EXTRA_RE = re.compile(r"^extra_(?P<field>url|ref|path)_(?P<index>.+)$")


def _templates(request: Request):
    return request.app.state.templates


def _jobs_with_refs(db: Session, *, status: JobStatus, schedule_type: ScheduleType | None) -> list[tuple]:
    """Jobs joined with their MR and model profile, ordered by position."""
    stmt = (
        select(ScheduledJob, MergeRequest, ModelProfile)
        .join(MergeRequest, MergeRequest.id == ScheduledJob.merge_request_id)
        .join(ModelProfile, ModelProfile.id == ScheduledJob.model_profile_id)
        .where(ScheduledJob.status == status)
    )
    if schedule_type is not None:
        stmt = stmt.where(ScheduledJob.schedule_type == schedule_type)
    return list(db.execute(stmt.order_by(ScheduledJob.position, ScheduledJob.id)).all())


def _queue_context(db: Session) -> dict[str, list[tuple]]:
    return {
        "running": _jobs_with_refs(db, status=JobStatus.claimed, schedule_type=None)
        + _jobs_with_refs(db, status=JobStatus.running, schedule_type=None),
        "queued": _jobs_with_refs(db, status=JobStatus.queued, schedule_type=ScheduleType.immediate),
        "nightly": _jobs_with_refs(db, status=JobStatus.queued, schedule_type=ScheduleType.nightly),
    }


def _render_page(request: Request, db: Session, message: str | None = None) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request, "queue/list.html", {"message": message, **_queue_context(db)}
    )


def _render_sections(request: Request, db: Session, message: str | None = None) -> HTMLResponse:
    """All three sections inside the #queue-page wrapper (swap target)."""
    return _templates(request).TemplateResponse(
        request, "queue/partials/sections.html", {"message": message, **_queue_context(db)}
    )


def _render_queue_section(request: Request, db: Session, message: str | None = None) -> HTMLResponse:
    """Just the sortable queue section (swap target for reorder/cancel/top/bottom)."""
    context = _queue_context(db)
    return _templates(request).TemplateResponse(
        request,
        "queue/partials/queue.html",
        {"queued": context["queued"], "section_message": message},
    )


@router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render_page(request, db)


@router.post("/queue/reorder", response_class=HTMLResponse)
def queue_reorder(
    request: Request,
    db: Session = Depends(get_db),
    ids: list[str] = Form(...),
) -> HTMLResponse:
    """Rewrite positions 1..n in the submitted order (HTMX, body ids=.. repeated)."""
    try:
        ordered = [int(value) for value in ids]
        scheduling.reorder(db, ordered)
        return _render_queue_section(request, db)
    except (ValueError, TypeError) as exc:
        return _render_queue_section(request, db, message=f"Reorder failed: {exc}")


@router.post("/queue/{job_id}/cancel", response_class=HTMLResponse)
def queue_cancel(request: Request, job_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(ScheduledJob, job_id)
    if job is None:
        return _render_queue_section(request, db, message=f"No job with id {job_id}.")
    if job.status in (JobStatus.claimed.value, JobStatus.running.value):
        # In flight: stop the container and finalize the run/job. The job
        # leaves the Running section, so the swap target is the whole page.
        try:
            message = review_service.cancel_inflight(db, job)
        except ValueError as exc:
            message = f"Cancel failed: {exc}"
        return _render_sections(request, db, message=message)
    try:
        scheduling.cancel(db, job)
    except ValueError as exc:
        return _render_queue_section(request, db, message=f"Cancel failed: {exc}")
    return _render_queue_section(request, db)


@router.post("/queue/{job_id}/top", response_class=HTMLResponse)
def queue_move_top(request: Request, job_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(ScheduledJob, job_id)
    if job is None:
        return _render_queue_section(request, db, message=f"No job with id {job_id}.")
    try:
        scheduling.move_to_top(db, job)
    except ValueError as exc:
        return _render_queue_section(request, db, message=f"Move failed: {exc}")
    return _render_queue_section(request, db)


@router.post("/queue/{job_id}/bottom", response_class=HTMLResponse)
def queue_move_bottom(request: Request, job_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(ScheduledJob, job_id)
    if job is None:
        return _render_queue_section(request, db, message=f"No job with id {job_id}.")
    try:
        scheduling.move_to_bottom(db, job)
    except ValueError as exc:
        return _render_queue_section(request, db, message=f"Move failed: {exc}")
    return _render_queue_section(request, db)


@router.post("/queue/nightly/{job_id}/remove", response_class=HTMLResponse)
def queue_nightly_remove(request: Request, job_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Remove from the nightly set; the review goes back to the live queue."""
    job = db.get(ScheduledJob, job_id)
    if job is None:
        return _render_sections(request, db)
    try:
        scheduling.remove_nightly(db, job)
    except ValueError:
        return _render_sections(request, db)
    return _render_sections(request, db)


@router.post("/mrs/{iid}/schedule", response_class=HTMLResponse)
async def schedule_review(
    request: Request,
    iid: int,
    db: Session = Depends(get_db),
    model_profile_id: int = Form(0),
    schedule_type: str = Form("immediate"),
    prompt_override: str = Form(""),
    post_to_gitlab: bool | None = Form(None),
) -> HTMLResponse:
    """Enqueue a review for an MR; re-renders the MR detail with a confirmation."""
    row = get_settings_row(db)
    mr = db.scalar(
        select(MergeRequest).where(
            MergeRequest.project == row.gitlab_project,
            MergeRequest.iid == iid,
        )
    )
    if mr is None:
        return _templates(request).TemplateResponse(
            request, "mrs/not_found.html", {"iid": iid}, status_code=404
        )
    profile = db.get(ModelProfile, model_profile_id)
    error: str | None = None
    if profile is None:
        error = f"No model profile with id {model_profile_id} — add one in Settings."
    elif schedule_type not in (ScheduleType.immediate.value, ScheduleType.nightly.value):
        error = f"Unknown schedule type {schedule_type!r}."
    extras = _parse_extra_projects(await request.form())
    if error is None and any(not e["url"] for e in extras):
        error = "Every extra project row needs a repository URL."
    if error is not None:
        context = detail_context(db, mr)
        context["schedule_error"] = error
        return _templates(request).TemplateResponse(request, "mrs/detail.html", context)

    job = scheduling.enqueue(
        db,
        mr=mr,
        profile=profile,
        schedule_type=schedule_type,
        prompt_override=prompt_override,
        extra_projects=extras,
        post_to_gitlab=post_to_gitlab,
    )
    if schedule_type == ScheduleType.immediate.value:
        message = f"Review scheduled — {profile.name}, immediate (queue position {job.position})."
    else:
        message = f"Review scheduled — {profile.name}, nightly (will run after {row.nightly_time})."
    context = detail_context(db, mr, message=message)
    return _templates(request).TemplateResponse(request, "mrs/detail.html", context)


@router.post("/mrs/{iid}/schedule-extra-row")
def schedule_extra_row(request: Request, iid: int) -> HTMLResponse:
    """One empty extra-project row (HTMX appends it to #extra-projects)."""
    return _templates(request).TemplateResponse(
        request,
        "mrs/partials/schedule_extra_row.html",
        {"index": uuid4().hex[:8], "url": "", "ref": "", "path": ""},
    )


def _parse_extra_projects(form) -> list[dict[str, str]]:
    """Collect extra_url_<n>/extra_ref_<n>/extra_path_<n> form fields into
    [{url, ref?, path?}] objects (rows with an empty URL are dropped)."""
    fields: dict[str, dict[str, str]] = {}
    for name, value in form.multi_items():
        match = _EXTRA_RE.match(name)
        if match:
            fields.setdefault(match["index"], {})[match["field"]] = str(value)
    projects = []
    for values in fields.values():
        url = (values.get("url") or "").strip()
        if not url:
            continue
        project = {"url": url}
        ref = (values.get("ref") or "").strip()
        path = (values.get("path") or "").strip()
        if ref:
            project["ref"] = ref
        if path:
            project["path"] = path
        projects.append(project)
    return projects
