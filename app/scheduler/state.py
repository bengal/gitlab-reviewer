"""Reference to the running FastAPI app for background (non-request) code.

The queue pump and worker threads run outside of request scope, so they reach
the app (``app.state.orchestrator``, settings, ...) through this small
registry instead of a FastAPI dependency.
"""

from fastapi import FastAPI

_app: FastAPI | None = None


def set_app(app: FastAPI) -> None:
    """Register the live app (called once from ``create_app()``)."""
    global _app
    _app = app


def get_app() -> FastAPI:
    """The registered app; raises RuntimeError when create_app() never ran."""
    if _app is None:
        raise RuntimeError("no app registered; call create_app() first")
    return _app
