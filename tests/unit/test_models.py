"""ORM models: instantiation, enums, constraints, Alembic baseline."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.db import get_settings_row
from app.models import (
    JobStatus,
    MergeRequest,
    ModelProfile,
    ReviewRun,
    RunStatus,
    ScheduledJob,
    ScheduleType,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {"settings", "model_profile", "merge_request", "scheduled_job", "review_run"}


def test_settings_singleton_defaults(db):
    row = get_settings_row(db)
    assert row.id == 1
    assert row.gitlab_url == ""
    assert row.gitlab_token == ""
    assert row.default_review_prompt == ""
    assert row.nightly_time == "02:30"
    assert row.max_concurrent_reviews == 2
    assert row.poll_interval_seconds == 30
    assert row.post_results_to_gitlab is False
    assert row.known_libraries == []
    assert get_settings_row(db) is row


def test_status_enum_values():
    assert {s.value for s in JobStatus} == {"queued", "claimed", "running", "done", "failed", "cancelled"}
    assert {s.value for s in RunStatus} == {"running", "success", "error", "timeout"}
    assert {s.value for s in ScheduleType} == {"immediate", "nightly"}


def test_create_all_model_instances(db):
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    mr = MergeRequest(
        project="group/proj",
        iid=42,
        title="Add a thing",
        author="alice",
        source_branch="feature",
        target_branch="main",
        sha="abc123",
        web_url="http://gitlab/group/proj/-/merge_requests/42",
        state="opened",
        updated_at=now,
        last_seen_at=now,
    )
    profile = ModelProfile(
        name="claude",
        provider="anthropic",
        model_id="claude-sonnet-4",
        is_default=True,
        extra_opencode_json={"some": "config"},
    )
    db.add_all([mr, profile])
    db.commit()

    job = ScheduledJob(
        merge_request_id=mr.id,
        model_profile_id=profile.id,
        prompt_override=None,
        extra_projects=[{"url": "https://example.com/lib", "ref": "v1", "path": "lib"}],
        schedule_type=ScheduleType.nightly.value,
        position=1,
        status=JobStatus.queued.value,
        post_to_gitlab=None,
    )
    db.add(job)
    db.commit()

    run = ReviewRun(
        scheduled_job_id=job.id,
        merge_request_id=mr.id,
        model_profile_id=profile.id,
        status=RunStatus.running.value,
        log="boot\n",
    )
    db.add(run)
    db.commit()

    db.refresh(mr)
    db.refresh(job)
    db.refresh(run)
    assert mr.iid == 42
    assert mr.project == "group/proj"
    assert job.schedule_type is ScheduleType.nightly
    assert job.status is JobStatus.queued
    assert job.extra_projects == [{"url": "https://example.com/lib", "ref": "v1", "path": "lib"}]
    assert run.status is RunStatus.running
    assert run.archived_at is None
    assert run.exit_code is None


def test_merge_request_unique_project_iid(db):
    db.add(MergeRequest(project="g/p", iid=1, title="a"))
    db.commit()
    db.add(MergeRequest(project="g/p", iid=1, title="b"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.fixture()
def alembic_cfg(tmp_path, monkeypatch):
    db_file = tmp_path / "alembic.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def test_alembic_upgrade_and_downgrade(alembic_cfg):
    command.upgrade(alembic_cfg, "head")
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert EXPECTED_TABLES <= set(inspect(conn).get_table_names())
            row = conn.execute(
                text("SELECT id, nightly_time, max_concurrent_reviews, poll_interval_seconds FROM settings")
            ).fetchone()
        assert tuple(row) == (1, "02:30", 2, 30)

        # ORM works against the migrated schema
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory() as session:
            session.add(MergeRequest(project="g/p", iid=1, title="t"))
            session.add(ModelProfile(name="p1", provider="local", model_id="qwen"))
            session.commit()
            mr = session.query(MergeRequest).one()
            profile = session.query(ModelProfile).one()
            session.add(
                ScheduledJob(
                    merge_request_id=mr.id,
                    model_profile_id=profile.id,
                    schedule_type="immediate",
                    position=1,
                    status="queued",
                )
            )
            session.commit()
            assert session.execute(text("SELECT count(*) FROM scheduled_job")).scalar() == 1
    finally:
        engine.dispose()

    command.downgrade(alembic_cfg, "base")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
        assert EXPECTED_TABLES.isdisjoint(tables)
    finally:
        engine.dispose()
