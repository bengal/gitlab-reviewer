"""Database engine, session factory and declarative Base."""

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
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


_ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"


def _ensure_alembic_head(eng: Engine) -> None:
    """Stamp a schema Alembic doesn't know about with the current head revision.

    A DB created via create_all (an app boot before `alembic upgrade head`) or
    left behind by a failed migration run has tables but no (or an empty)
    alembic_version row; without a stamp, `alembic upgrade head` re-runs the
    baseline DDL and crashes with "table ... already exists".
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    with eng.connect() as conn:
        inspector = inspect(conn)
        tables = inspector.get_table_names()
        stamped = False
        if "alembic_version" in tables:
            stamped = conn.execute(text("SELECT 1 FROM alembic_version LIMIT 1")).first() is not None
    if stamped:
        return  # alembic-managed schema: migrations are authoritative

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    heads = ScriptDirectory.from_config(cfg).get_current_head()
    if heads is None:
        raise RuntimeError("no alembic revisions found under alembic/versions")
    heads = (heads,) if isinstance(heads, str) else tuple(heads)
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one alembic head, got {heads!r}")
    head = heads[0]
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT pk_alembic_version PRIMARY KEY (version_num))"
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": head},
        )


def init_db(engine: Engine | None = None) -> Engine:
    """Prepare the schema and ensure the settings singleton row exists.

    A fresh DB gets create_all plus an alembic head stamp, so a later
    `alembic upgrade head` is a clean no-op; a schema created by a previous
    app boot (or left by a failed migration) is adopted via the same stamp
    instead of colliding with the baseline migration. Alembic-managed DBs
    (already stamped) are left untouched.
    """
    global _engine, _session_factory
    from app import models  # noqa: F401  -- register all tables on Base.metadata

    eng = engine or make_engine()
    with eng.connect() as conn:
        inspector = inspect(conn)
        tables = inspector.get_table_names()
    if models.SettingsRow.__table__.name not in tables:
        Base.metadata.create_all(eng)
    _ensure_alembic_head(eng)
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
