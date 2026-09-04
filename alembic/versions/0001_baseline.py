"""baseline

Revision ID: 0001
Revises:
Create Date: 2026-09-04

Hand-written baseline creating all five tables to match app.models, plus the
settings singleton row (id=1).
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gitlab_url", sa.String(length=512), nullable=False),
        sa.Column("gitlab_project", sa.String(length=512), nullable=False),
        sa.Column("gitlab_token", sa.Text(), nullable=False),
        sa.Column("default_review_prompt", sa.Text(), nullable=False),
        sa.Column("nightly_time", sa.String(length=5), nullable=False),
        sa.Column("max_concurrent_reviews", sa.Integer(), nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("post_results_to_gitlab", sa.Boolean(), nullable=False),
        sa.Column("known_libraries", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "model_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model_id", sa.String(length=300), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("api_key", sa.String(length=1024), nullable=True),
        sa.Column("api_key_env", sa.String(length=200), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("extra_opencode_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "merge_request",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project", sa.String(length=512), nullable=False),
        sa.Column("iid", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("author", sa.String(length=300), nullable=False),
        sa.Column("source_branch", sa.String(length=512), nullable=False),
        sa.Column("target_branch", sa.String(length=512), nullable=False),
        sa.Column("sha", sa.String(length=64), nullable=False),
        sa.Column("web_url", sa.String(length=1000), nullable=False),
        sa.Column("state", sa.String(length=50), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project", "iid", name="uq_merge_request_project_iid"),
    )
    op.create_table(
        "scheduled_job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("merge_request_id", sa.Integer(), nullable=False),
        sa.Column("model_profile_id", sa.Integer(), nullable=False),
        sa.Column("prompt_override", sa.Text(), nullable=True),
        sa.Column("extra_projects", sa.JSON(), nullable=False),
        sa.Column("schedule_type", sa.String(length=20), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("post_to_gitlab", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["merge_request_id"], ["merge_request.id"]),
        sa.ForeignKeyConstraint(["model_profile_id"], ["model_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduled_job_merge_request_id", "scheduled_job", ["merge_request_id"], unique=False)
    op.create_index("ix_scheduled_job_model_profile_id", "scheduled_job", ["model_profile_id"], unique=False)
    op.create_table(
        "review_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scheduled_job_id", sa.Integer(), nullable=False),
        sa.Column("merge_request_id", sa.Integer(), nullable=False),
        sa.Column("model_profile_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("container_id", sa.String(length=120), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log", sa.Text(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("result_markdown", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("gitlab_note_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["merge_request_id"], ["merge_request.id"]),
        sa.ForeignKeyConstraint(["model_profile_id"], ["model_profile.id"]),
        sa.ForeignKeyConstraint(["scheduled_job_id"], ["scheduled_job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_review_run_scheduled_job_id", "review_run", ["scheduled_job_id"], unique=False
    )
    op.create_index("ix_review_run_merge_request_id", "review_run", ["merge_request_id"], unique=False)
    op.create_index("ix_review_run_model_profile_id", "review_run", ["model_profile_id"], unique=False)
    op.create_index("ix_review_run_archived_at", "review_run", ["archived_at"], unique=False)

    op.execute(
        "INSERT INTO settings "
        "(id, gitlab_url, gitlab_project, gitlab_token, default_review_prompt, nightly_time, "
        "max_concurrent_reviews, poll_interval_seconds, post_results_to_gitlab, known_libraries) "
        "VALUES (1, '', '', '', '', '02:30', 2, 30, 0, '[]')"
    )


def downgrade() -> None:
    op.drop_index("ix_review_run_archived_at", table_name="review_run")
    op.drop_index("ix_review_run_model_profile_id", table_name="review_run")
    op.drop_index("ix_review_run_merge_request_id", table_name="review_run")
    op.drop_index("ix_review_run_scheduled_job_id", table_name="review_run")
    op.drop_table("review_run")
    op.drop_index("ix_scheduled_job_model_profile_id", table_name="scheduled_job")
    op.drop_index("ix_scheduled_job_merge_request_id", table_name="scheduled_job")
    op.drop_table("scheduled_job")
    op.drop_table("merge_request")
    op.drop_table("model_profile")
    op.drop_table("settings")
