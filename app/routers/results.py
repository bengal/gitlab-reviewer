"""Results browser (M6b): current runs, run detail, archive action.

The /results list shows non-archived runs by default; the status and
archive-state filters are plain GET form params so the page works without
JavaScript. The run detail body lives in results/partials/detail.html and is
shared with the read-only /archive/{id} view (app.routers.archive).
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models import MergeRequest, ModelProfile, ReviewRun, RunStatus
from app.schemas.result import parse_review_result
from app.services.result_render import SEVERITY_SECTIONS, finding_ref, render_result_markdown

router = APIRouter()

_RUN_STATUS_VALUES = frozenset(s.value for s in RunStatus)


def _templates(request: Request):
    return request.app.state.templates


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def format_run_duration(started_at: datetime | None, finished_at: datetime | None) -> str:
    """A run's wall-clock duration as a short human string
    ('45s', '7m 12s', '1h 5m'). Empty while it has not finished yet (the
    table shows "in progress" in that case)."""
    if started_at is None or finished_at is None:
        return ""
    total = int((finished_at - started_at).total_seconds())
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def run_rows(db: Session, *, status: str = "all", archived: bool | None = None) -> list:
    """Runs joined with their MR and model profile, newest first.

    ``archived``: None = no filter, False = only non-archived runs,
    True = only archived runs. Unknown ``status`` values are ignored.
    """
    stmt = (
        select(ReviewRun, MergeRequest, ModelProfile)
        .join(MergeRequest, MergeRequest.id == ReviewRun.merge_request_id)
        .join(ModelProfile, ModelProfile.id == ReviewRun.model_profile_id)
        .order_by(ReviewRun.id.desc())
    )
    if status in _RUN_STATUS_VALUES:
        stmt = stmt.where(ReviewRun.status == status)
    if archived is True:
        stmt = stmt.where(ReviewRun.archived_at.is_not(None))
    elif archived is False:
        stmt = stmt.where(ReviewRun.archived_at.is_(None))
    return list(db.execute(stmt).all())


def detail_context(db: Session, run: ReviewRun) -> dict:
    """Context for results/partials/detail.html, shared with /archive/{id}."""
    mr = db.get(MergeRequest, run.merge_request_id)
    profile = db.get(ModelProfile, run.model_profile_id)
    return {
        "run": run,
        "mr": mr,
        "profile": profile,
        # Parsed result for the HTML severity buckets (None when the run has
        # no structured result_json, e.g. the raw-markdown fallback).
        "result": parse_review_result(run.result_json) if run.result_json else None,
        # Exact markdown the post-back posts / would post (M6b).
        "result_markdown": render_result_markdown(run.result_json, run.result_markdown, run),
        "severity_sections": SEVERITY_SECTIONS,
        "finding_ref": finding_ref,
    }


def _not_found(request: Request, run_id: int) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request, "results/not_found.html", {"run_id": run_id}, status_code=404
    )


@router.get("/results", response_class=HTMLResponse)
def results_list(
    request: Request,
    db: Session = Depends(get_db),
    status: str = "all",
    archive: str = "current",
) -> HTMLResponse:
    """Runs table with status and archive-state filters."""
    status = status if status in _RUN_STATUS_VALUES or status == "all" else "all"
    archived = None if archive == "all" else False
    context = {
        "runs": run_rows(db, status=status, archived=archived),
        "status": status,
        "archive_filter": "all" if archive == "all" else "current",
        "status_choices": [s.value for s in RunStatus],
        "link_prefix": "/results/",
        "empty_hint": "No review runs — schedule one from the MR list, or check the filters.",
        "run_duration": format_run_duration,
    }
    return _templates(request).TemplateResponse(request, "results/list.html", context)


@router.get("/results/{run_id}", response_class=HTMLResponse)
def results_detail(request: Request, run_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Run detail: status banner, timings, rendered result, log, Archive button."""
    run = db.get(ReviewRun, run_id)
    if run is None:
        return _not_found(request, run_id)
    context = detail_context(db, run)
    context["read_only"] = False
    context["message"] = None
    return _templates(request).TemplateResponse(request, "results/detail.html", context)


@router.get("/results/{run_id}/session")
def results_session_download(request: Request, run_id: int, db: Session = Depends(get_db)):
    """Download the run's opencode session export (model thinking + full
    transcript) as a JSON attachment. 404 when the run is unknown or the run
    produced no session capture (e.g. fake backend, opencode failed)."""
    run = db.get(ReviewRun, run_id)
    if run is None:
        return _not_found(request, run_id)
    if not run.session_json:
        return Response(status_code=404, content="no session captured for this run")
    body = json.dumps(run.session_json, indent=2, ensure_ascii=False) + "\n"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="run-{run_id}-session.json"'},
    )


@router.post("/results/{run_id}/archive", response_class=HTMLResponse)
def results_archive(request: Request, run_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Archive a run (archived_at = now) and re-render the detail page."""
    run = db.get(ReviewRun, run_id)
    if run is None:
        return _not_found(request, run_id)
    if run.archived_at is None:
        run.archived_at = _now()
        db.commit()
        message = "Run archived — it now appears in the Archive."
    else:
        message = "Run is already archived."
    context = detail_context(db, run)
    context["read_only"] = False
    context["message"] = message
    return _templates(request).TemplateResponse(request, "results/detail.html", context)
