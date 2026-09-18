"""Merge request list and detail routes (with HTMX sync)."""

import math

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_settings_row
from app.deps import get_db
from app.models import JobStatus, MergeRequest, ModelProfile, ReviewRun, ScheduledJob, ScheduleType
from app.services import scheduling
from app.services.mr_sync import sync_open_mrs

router = APIRouter()

MR_PAGE_SIZE = 25


def _configured(row) -> bool:
    return bool(row.gitlab_url.strip() and row.gitlab_project.strip() and row.gitlab_token.strip())


def _templates(request: Request):
    return request.app.state.templates


def _list_mrs(db: Session, row, page: int = 1) -> tuple[list[MergeRequest], int, int]:
    """Cached MRs of the configured project (highest iid first), one page at a time.

    Returns ``(page_items, total_count, effective_page)``; an out-of-range
    ``page`` (e.g. a stale link after the list shrank) is clamped to the last
    page, never an error.
    """
    total = db.scalar(
        select(func.count())
        .select_from(
            select(MergeRequest.id).where(MergeRequest.project == row.gitlab_project).subquery()
        )
    ) or 0
    pages = max(1, math.ceil(total / MR_PAGE_SIZE))
    page = max(1, min(page, pages))
    items = list(
        db.scalars(
            select(MergeRequest)
            .where(MergeRequest.project == row.gitlab_project)
            .order_by(MergeRequest.iid.desc())
            .offset((page - 1) * MR_PAGE_SIZE)
            .limit(MR_PAGE_SIZE)
        )
    )
    return items, total, page


def _review_counts(db: Session, mrs: list[MergeRequest]) -> dict[int, tuple[int, int]]:
    """Per-MR (review runs so far, queued jobs) counts for the list columns."""
    counts = {mr.id: (0, 0) for mr in mrs}
    if not mrs:
        return counts
    mr_ids = [mr.id for mr in mrs]
    runs = dict(
        db.execute(
            select(ReviewRun.merge_request_id, func.count(ReviewRun.id))
            .where(ReviewRun.merge_request_id.in_(mr_ids))
            .group_by(ReviewRun.merge_request_id)
        ).all()
    )
    queued = dict(
        db.execute(
            select(ScheduledJob.merge_request_id, func.count(ScheduledJob.id))
            .where(
                ScheduledJob.merge_request_id.in_(mr_ids),
                ScheduledJob.status == JobStatus.queued.value,
            )
            .group_by(ScheduledJob.merge_request_id)
        ).all()
    )
    for mr in mrs:
        counts[mr.id] = (runs.get(mr.id, 0), queued.get(mr.id, 0))
    return counts


def _list_context(
    db: Session, row, page: int, *, configured: bool, message: str | None, failed: bool
) -> dict:
    """Template context for the list partial: the current page slice plus the
    pagination numbers (page, pages, row range) for the controls."""
    mrs, total, page = _list_mrs(db, row, page=page) if configured else ([], 0, 1)
    return {
        "configured": configured,
        "mrs": mrs,
        "total": total,
        "page": page,
        "pages": max(1, math.ceil(total / MR_PAGE_SIZE)),
        "page_start": (page - 1) * MR_PAGE_SIZE + 1,
        "page_end": min(total, page * MR_PAGE_SIZE),
        "review_counts": _review_counts(db, mrs),
        "default_branch": row.gitlab_default_branch,
        "sync_message": message,
        "sync_error": failed,
    }


@router.get("/mrs", response_class=HTMLResponse)
def mr_list(request: Request, page: int = 1, db: Session = Depends(get_db)) -> HTMLResponse:
    row = get_settings_row(db)
    context = _list_context(
        db, row, page, configured=_configured(row), message=None, failed=False
    )
    return _templates(request).TemplateResponse(request, "mrs/list.html", context)


@router.post("/mrs/sync", response_class=HTMLResponse)
def mr_sync(request: Request, page: int = Form(1), db: Session = Depends(get_db)) -> HTMLResponse:
    """Re-fetch open MRs; re-renders the list partial (HTMX swap target),
    keeping the requested page."""
    row = get_settings_row(db)
    if not _configured(row):
        context = _list_context(
            db, row, page, configured=False, message=None, failed=False
        )
        return _templates(request).TemplateResponse(request, "mrs/partials/list.html", context)
    added, updated, unchanged, error = sync_open_mrs(db)
    if error:
        message, failed = f"Sync failed: {error}", True
    else:
        message, failed = f"Synced: {added} added, {updated} updated, {unchanged} unchanged.", False
    context = _list_context(db, row, page, configured=True, message=message, failed=failed)
    return _templates(request).TemplateResponse(request, "mrs/partials/list.html", context)


@router.post("/mrs/schedule-selected", response_class=HTMLResponse)
def schedule_selected_mrs(
    request: Request,
    db: Session = Depends(get_db),
    mr_iids: list[str] = Form(default=[]),
    schedule_type: str = Form("immediate"),
    page: int = Form(1),
) -> HTMLResponse:
    """Batch-schedule reviews for the selected open MRs using the default
    model profile and the known library projects from settings. Re-renders
    the list partial (HTMX swap target), keeping the current page; also works
    as a plain form POST."""
    row = get_settings_row(db)
    if not _configured(row):
        context = _list_context(
            db, row, page, configured=False, message=None, failed=False
        )
        return _templates(request).TemplateResponse(request, "mrs/partials/list.html", context)
    message, failed = None, False
    profile = db.scalar(
        select(ModelProfile).order_by(ModelProfile.is_default.desc(), ModelProfile.name).limit(1)
    )
    if profile is None:
        message, failed = "No model profiles yet — add one in Settings.", True
    elif schedule_type not in (ScheduleType.immediate.value, ScheduleType.nightly.value):
        message, failed = f"Unknown schedule type {schedule_type!r}.", True
    else:
        try:
            iids = list(dict.fromkeys(int(value) for value in mr_iids))
        except ValueError:
            iids = []
        if not iids:
            message, failed = "Select at least one merge request.", True
        else:
            selected = list(
                db.scalars(
                    select(MergeRequest).where(
                        MergeRequest.project == row.gitlab_project,
                        MergeRequest.iid.in_(iids),
                        MergeRequest.state == "opened",
                    )
                )
            )
            if not selected:
                message, failed = "None of the selected merge requests are open.", True
            else:
                for mr in selected:
                    scheduling.enqueue(
                        db,
                        mr=mr,
                        profile=profile,
                        schedule_type=schedule_type,
                        extra_projects=list(row.known_libraries or []),
                    )
                when = (
                    "immediately"
                    if schedule_type == ScheduleType.immediate.value
                    else f"nightly (after {row.nightly_time})"
                )
                message = f"Scheduled {len(selected)} review(s) — {profile.name}, {when}."
    context = _list_context(db, row, page, configured=True, message=message, failed=failed)
    return _templates(request).TemplateResponse(request, "mrs/partials/list.html", context)


def detail_context(db: Session, mr: MergeRequest, message: str | None = None) -> dict:
    """Context for mrs/detail.html: the snapshot plus the schedule-form inputs
    (profiles, known-library suggestions, defaults). Shared by the detail
    route and the schedule POST in app.routers.queue."""
    row = get_settings_row(db)
    profiles = list(
        db.scalars(select(ModelProfile).order_by(ModelProfile.is_default.desc(), ModelProfile.name))
    )
    return {
        "mr": mr,
        "profiles": profiles,
        "known_libraries": row.known_libraries or [],
        "nightly_time": row.nightly_time,
        "default_post_to_gitlab": row.post_results_to_gitlab,
        "schedule_message": message,
    }


@router.get("/mrs/{iid}", response_class=HTMLResponse)
def mr_detail(request: Request, iid: int, db: Session = Depends(get_db)) -> HTMLResponse:
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
    return _templates(request).TemplateResponse(request, "mrs/detail.html", detail_context(db, mr))
