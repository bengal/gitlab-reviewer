"""Shared test fixtures (conventions used by all milestones).

Fixtures:
- ``app``: a fresh FastAPI app per test — tmp SQLite via DATABASE_URL set
  before the app is created, ORCHESTRATOR=fake, deterministic secrets,
  seeded settings singleton.
- ``client``: a FastAPI TestClient for ``app``.
- ``db``: a SQLAlchemy session bound to ``app``'s engine.

Tests never touch the network, podman, or a real GitLab.
"""

import pytest

_TEST_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_TEST_PASSWORD = "test-password"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Fresh app + tmp SQLite + fake orchestrator, per test."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.sqlite3'}")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("SECRET_ENC_KEY", _TEST_FERNET_KEY)
    monkeypatch.setenv("REVIEW_IMAGE", "gitlab-mr-review/review-runner:test")
    monkeypatch.setenv("ORCHESTRATOR", "fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from app.config import get_settings
    from app.main import create_app
    from app.security import hash_password

    monkeypatch.setenv("APP_PASSWORD_HASH", hash_password(_TEST_PASSWORD))
    get_settings.cache_clear()

    application = create_app()
    try:
        yield application
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def client(app):
    """TestClient for the app fixture."""
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db(app):
    """SQLAlchemy session bound to the app's engine."""
    from app.db import get_db_session

    session = get_db_session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def test_password() -> str:
    """The shared password the app fixture's APP_PASSWORD_HASH was made from."""
    return _TEST_PASSWORD
