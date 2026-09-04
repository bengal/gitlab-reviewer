"""Login/logout routes for the shared-password session.

GET  /login  -- render the login form
POST /login  -- verify APP_PASSWORD_HASH, set the signed session cookie
POST /logout -- clear the session cookie (guarded; unauthenticated is a no-op
                redirect back to /login)
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.deps import SESSION_COOKIE, SESSION_MAX_AGE, encode_session
from app.security import verify_password

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@router.get("/login")
def login_form(request: Request) -> Response:
    return _templates(request).TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(request: Request, password: str = Form("")) -> Response:
    if verify_password(password, get_settings().app_password_hash):
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            encode_session(),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response
    return _templates(request).TemplateResponse(
        request, "login.html", {"error": "Invalid password."}, status_code=401
    )


@router.post("/logout")
def logout(request: Request) -> Response:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
