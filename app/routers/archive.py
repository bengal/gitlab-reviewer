"""Archive browser (M6b): archived runs, read-only.

The archive is a pure view over review_run rows with archived_at set —
archiving itself happens on the results page (POST /results/{id}/archive).
The detail reuses the results detail partial (results/partials/detail.html)
in read-only mode, so there is no archive button here.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models import ReviewRun
from app.routers.results import detail_context, run_rows

router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


@router.get("/archive", response_class=HTMLResponse)
def archive_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Table of archived runs (same columns as /results)."""
    context = {
        "runs": run_rows(db, archived=True),
        "link_prefix": "/archive/",
        "empty_hint": "No archived runs yet — archive finished runs from the Results page.",
    }
    return _templates(request).TemplateResponse(request, "archive/list.html", context)


@router.get("/archive/{run_id}", response_class=HTMLResponse)
def archive_detail(request: Request, run_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    """Read-only detail of an archived run (same template as /results/{id})."""
    run = db.get(ReviewRun, run_id)
    if run is None or run.archived_at is None:
        return _templates(request).TemplateResponse(
            request, "results/not_found.html", {"run_id": run_id}, status_code=404
        )
    context = detail_context(db, run)
    context["read_only"] = True
    context["message"] = None
    return _templates(request).TemplateResponse(request, "archive/detail.html", context)
