"""FastAPI application factory."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_settings_row, init_db
from app.deps import SESSION_COOKIE, decode_session, get_db, is_exempt_path, require_auth
from app.orchestrator import create_orchestrator
from app.routers import archive, auth, mrs, queue, results
from app.routers import settings as settings_router
from app.scheduler import init_scheduler
from app.scheduler.state import set_app

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    settings = get_settings()
    init_db()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = init_scheduler(app)
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None and scheduler.running:
                scheduler.shutdown(wait=False)

    # require_auth is wired centrally as a global dependency: every API route
    # registered on this app (including routers included by later milestones)
    # is guarded without per-router work. Exempt paths: /login*, /healthz,
    # /static (see app.deps.EXEMPT_PREFIXES).
    app = FastAPI(title="gitlab-mr-review", lifespan=lifespan, dependencies=[Depends(require_auth)])
    app.state.settings = settings
    # Execution backend for the worker (M6a): FakeOrchestrator when
    # ORCHESTRATOR=fake, PodmanOrchestrator otherwise.
    app.state.orchestrator = create_orchestrator(settings)
    set_app(app)

    templates_dir = BASE_DIR / "templates"
    static_dir = BASE_DIR / "static"
    templates_dir.mkdir(exist_ok=True)
    static_dir.mkdir(exist_ok=True)
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # The global dependency only covers API routes; FastAPI's /docs, /redoc
    # and /openapi.json are plain Starlette routes, so a thin middleware
    # applies the same guard to them.
    @app.middleware("http")
    async def guard_plain_routes(request: Request, call_next):
        if (
            not is_exempt_path(request.url.path)
            and decode_session(request.cookies.get(SESSION_COOKIE)) is None
        ):
            return RedirectResponse(url="/login", status_code=303)
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
        row = get_settings_row(db)
        configured = bool(
            row.gitlab_url.strip() and row.gitlab_project.strip() and row.gitlab_token.strip()
        )
        context = {
            "configured": configured,
            "gitlab_url": row.gitlab_url,
            "gitlab_project": row.gitlab_project,
        }
        return app.state.templates.TemplateResponse(request, "home.html", context)

    app.include_router(auth.router)
    app.include_router(mrs.router)
    app.include_router(queue.router)
    app.include_router(settings_router.router)
    app.include_router(results.router)
    app.include_router(archive.router)

    return app


app = create_app() if __name__ == "__main__" else None
