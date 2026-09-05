"""Merge request list and detail routes (with HTMX sync)."""

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


def _configured(row) -> bool:
    return bool(row.gitlab_url.strip() and row.gitlab_project.strip() and row.gitlab_token.strip())


def _templates(request: Request):
    return request.app.state.templates


def _list_mrs(db: Session, row) -> list[MergeRequest]:
    return list(
        db.scalars(
            select(MergeRequest)
            .where(MergeRequest.project == row.gitlab_project)
            .order_by(MergeRequest.iid.desc())
        )
    )


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


@router.get("/mrs", response_class=HTMLResponse)
def mr_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    row = get_settings_row(db)
    configured = _configured(row)
    mrs = _list_mrs(db, row) if configured else []
    return _templates(request).TemplateResponse(
        request,
        "mrs/list.html",
        {
            "configured": configured,
            "mrs": mrs,
            "review_counts": _review_counts(db, mrs),
            "default_branch": row.gitlab_default_branch,
            "sync_message": None,
            "sync_error": False,
        },
    )


@router.post("/mrs/sync", response_class=HTMLResponse)
def mr_sync(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Re-fetch open MRs; re-renders the list partial (HTMX swap target)."""
    row = get_settings_row(db)
    if not _configured(row):
        return _templates(request).TemplateResponse(
            request,
            "mrs/partials/list.html",
            {
                "configured": False,
                "mrs": [],
                "review_counts": {},
                "sync_message": None,
                "sync_error": False,
            },
        )
    added, updated, unchanged, error = sync_open_mrs(db)
    if error:
        message, failed = f"Sync failed: {error}", True
    else:
        message, failed = f"Synced: {added} added, {updated} updated, {unchanged} unchanged.", False
    mrs = _list_mrs(db, row)
    return _templates(request).TemplateResponse(
        request,
        "mrs/partials/list.html",
        {
            "configured": True,
            "mrs": mrs,
            "review_counts": _review_counts(db, mrs),
            "default_branch": row.gitlab_default_branch,
            "sync_message": message,
            "sync_error": failed,
        },
    )


@router.post("/mrs/schedule-selected", response_class=HTMLResponse)
def schedule_selected_mrs(
    request: Request,
    db: Session = Depends(get_db),
    mr_iids: list[str] = Form(default=[]),
    schedule_type: str = Form("immediate"),
) -> HTMLResponse:
    """Batch-schedule reviews for the selected open MRs using the default
    model profile and the known library projects from settings. Re-renders
    the list partial (HTMX swap target); also works as a plain form POST."""
    row = get_settings_row(db)
    if not _configured(row):
        return _templates(request).TemplateResponse(
            request,
            "mrs/partials/list.html",
            {
                "configured": False,
                "mrs": [],
                "review_counts": {},
                "sync_message": None,
                "sync_error": False,
            },
        )
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
    mrs = _list_mrs(db, row)
    return _templates(request).TemplateResponse(
        request,
        "mrs/partials/list.html",
        {
            "configured": True,
            "mrs": mrs,
            "review_counts": _review_counts(db, mrs),
            "default_branch": row.gitlab_default_branch,
            "sync_message": message,
            "sync_error": failed,
        },
    )


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
