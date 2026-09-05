"""settings: cache the project's default branch

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05

The MR list hides "→ <target>" when the target equals the project's
default branch; the branch name is fetched from GitLab during MR sync.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("settings", sa.Column("gitlab_default_branch", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("settings", "gitlab_default_branch")
