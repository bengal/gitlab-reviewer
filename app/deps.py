"""Shared FastAPI dependencies: settings, database, session auth.

Auth model: a single shared password (APP_PASSWORD_HASH) gates the app. A
successful login (app.routers.auth) sets the ``nm_review_session`` cookie, an
itsdangerous-signed and timestamped payload.

``require_auth`` is the guard. It is wired centrally in create_app() as a
FastAPI *global* dependency, so every API route registered on the app
(including routers included by later milestones) is guarded without
per-router work. Exempt paths: /login*, /healthz and /static (EXEMPT_PREFIXES).
/static is served by a Mount and never reaches a dependency; it is listed here
so the same check can be reused by the middleware in app.main that covers
FastAPI's plain (non-API) routes such as /docs and /openapi.json.

Note: with the FastAPI version pinned in this project, a dependency returning
a Response does not short-circuit the endpoint, so the guard raises
``HTTPException(status_code=303, headers={"Location": "/login"})`` instead of
returning a RedirectResponse.
"""

from collections.abc import Iterator
from typing import Any

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, TimedSerializer
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db_session

SESSION_COOKIE = "nm_review_session"
SESSION_MAX_AGE = 12 * 60 * 60  # seconds; expired cookies count as unauthenticated
SESSION_PAYLOAD: dict[str, Any] = {"user": "shared"}

# Path prefixes reachable without a session cookie.
EXEMPT_PREFIXES = ("/login", "/healthz", "/static")


def is_exempt_path(path: str, exempt_prefixes: tuple[str, ...] = EXEMPT_PREFIXES) -> bool:
    """True when the request path is exempt from authentication."""
    return path.startswith(exempt_prefixes)


def get_settings_dep() -> Settings:
    """Return the cached application settings."""
    return get_settings()


def get_db() -> Iterator[Session]:
    """Yield a request-scoped database session."""
    session = get_db_session()
    try:
        yield session
    finally:
        session.close()


def _serializer() -> TimedSerializer:
    return TimedSerializer(get_settings().session_secret, salt="nm-review-session")


def encode_session(payload: dict[str, Any] | None = None) -> str:
    """Sign a session payload for use as the cookie value."""
    return _serializer().dumps(payload if payload is not None else SESSION_PAYLOAD)


def decode_session(cookie_value: str | None) -> dict[str, Any] | None:
    """Validate a signed cookie value.

    Returns the payload dict, or None when the cookie is missing, tampered
    with, or older than SESSION_MAX_AGE.
    """
    if not cookie_value:
        return None
    try:
        payload = _serializer().loads(cookie_value, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return payload if isinstance(payload, dict) else None


def get_session(request: Request) -> dict[str, Any] | None:
    """Read and validate the session cookie of the current request."""
    return decode_session(request.cookies.get(SESSION_COOKIE))


def require_auth(
    request: Request,
    session: dict[str, Any] | None = Depends(get_session),
) -> dict[str, Any] | None:
    """Route guard: 303 redirect to /login when no valid session is present.

    Exempt paths (EXEMPT_PREFIXES) pass through. Registered as a global
    dependency in create_app(); see the module docstring.
    """
    if session is not None or is_exempt_path(request.url.path):
        return session
    raise HTTPException(status_code=303, headers={"Location": "/login"})
