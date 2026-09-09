"""model_profile: the model's context window

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06

opencode only auto-compacts a session when it knows the model's context
window (an unknown window disables the overflow check entirely). For local
models the window is not in models.dev, so it is captured per profile and
rendered into the model's ``limit`` in opencode.json.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_profile", sa.Column("context_window", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_profile", "context_window")
