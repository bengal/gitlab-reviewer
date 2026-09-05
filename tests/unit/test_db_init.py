"""init_db schema/Alembic reconciliation (regression: app boot before alembic)."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from app import models  # noqa: F401  -- register tables on Base.metadata
from app.db import Base, init_db, make_engine

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def alembic_cfg(tmp_path, monkeypatch):
    db_file = tmp_path / "reconcile.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def _stamped_version(url: str) -> str | None:
    from sqlalchemy import create_engine

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    finally:
        engine.dispose()
    return row[0] if row else None


def test_init_db_fresh_db_is_adopted_by_alembic(tmp_path, monkeypatch):
    db_file = tmp_path / "fresh.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    engine = make_engine(f"sqlite:///{db_file}")
    try:
        init_db(engine)
        with engine.connect() as conn:
            assert "settings" in inspect(conn).get_table_names()
        assert _stamped_version(f"sqlite:///{db_file}")
        # a later `alembic upgrade head` is a clean no-op, not a DDL collision
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        command.upgrade(cfg, "head")
        assert _stamped_version(f"sqlite:///{db_file}")
    finally:
        engine.dispose()


def test_init_db_adopts_create_all_schema_with_empty_version_table(alembic_cfg, tmp_path, monkeypatch):
    # The reported failure state: an earlier app boot created all tables via
    # create_all, then a failed `alembic upgrade head` left an empty
    # alembic_version table behind.
    db_file = tmp_path / "dev.sqlite3"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = make_engine(url)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE alembic_version ("
                    "version_num VARCHAR(32) NOT NULL, "
                    "CONSTRAINT pk_alembic_version PRIMARY KEY (version_num))"
                )
            )
        assert _stamped_version(url) is None

        init_db(engine)  # must not re-create tables, must stamp head

        assert _stamped_version(url) is not None
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        command.upgrade(cfg, "head")  # no-op now, previously crashed
        assert _stamped_version(url) is not None
    finally:
        engine.dispose()


def test_init_db_leaves_alembic_managed_db_untouched(alembic_cfg):
    # A DB migrated by alembic itself keeps its stamp; init_db is a no-op.
    command.upgrade(alembic_cfg, "head")
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    before = _stamped_version(url)
    assert before is not None

    engine = make_engine(url)
    try:
        init_db(engine)
        assert _stamped_version(url) == before
    finally:
        engine.dispose()
