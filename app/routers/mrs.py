"""Merge request list and detail routes (with HTMX sync)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_settings_row
from app.deps import get_db
from app.models import MergeRequest, ModelProfile
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


@router.get("/mrs", response_class=HTMLResponse)
def mr_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    row = get_settings_row(db)
    configured = _configured(row)
    mrs = _list_mrs(db, row) if configured else []
    return _templates(request).TemplateResponse(
        request,
        "mrs/list.html",
        {"configured": configured, "mrs": mrs, "sync_message": None, "sync_error": False},
    )


@router.post("/mrs/sync", response_class=HTMLResponse)
def mr_sync(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Re-fetch open MRs; re-renders the list partial (HTMX swap target)."""
    row = get_settings_row(db)
    if not _configured(row):
        return _templates(request).TemplateResponse(
            request,
            "mrs/partials/list.html",
            {"configured": False, "mrs": [], "sync_message": None, "sync_error": False},
        )
    added, updated, unchanged, error = sync_open_mrs(db)
    if error:
        message, failed = f"Sync failed: {error}", True
    else:
        message, failed = f"Synced: {added} added, {updated} updated, {unchanged} unchanged.", False
    return _templates(request).TemplateResponse(
        request,
        "mrs/partials/list.html",
        {
            "configured": True,
            "mrs": _list_mrs(db, row),
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
