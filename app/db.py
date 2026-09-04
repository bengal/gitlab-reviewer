"""Database engine, session factory and declarative Base."""

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def get_engine() -> Engine:
    """The active engine (created by init_db)."""
    if _engine is None:
        raise RuntimeError("database not initialized; call init_db() first")
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """The active session factory (created by init_db)."""
    if _session_factory is None:
        raise RuntimeError("database not initialized; call init_db() first")
    return _session_factory


def get_db_session() -> Session:
    """Open a new session on the active engine."""
    return get_session_factory()()


def init_db(engine: Engine | None = None) -> Engine:
    """Create tables (dev path) and ensure the settings singleton row exists.

    Production uses Alembic migrations instead of create_all; init_db is used
    by the app factory and tests for a zero-migration dev setup.
    """
    global _engine, _session_factory
    from app import models  # noqa: F401  -- register all tables on Base.metadata

    eng = engine or make_engine()
    Base.metadata.create_all(eng)
    _engine = eng
    _session_factory = sessionmaker(bind=eng, expire_on_commit=False)
    session = _session_factory()
    try:
        get_settings_row(session)
    finally:
        session.close()
    return eng


def get_settings_row(session: Session) -> "models.SettingsRow":  # noqa: F821
    """Return the settings singleton row, creating it (id=1) if missing."""
    from app import models

    row = session.get(models.SettingsRow, 1)
    if row is None:
        row = models.SettingsRow(id=1)
        session.add(row)
        session.commit()
    return row
