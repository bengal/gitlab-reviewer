"""review_run: opencode session export (model thinking + transcript)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-09

The review-runner entrypoint exports opencode's session for each run
(``opencode export <sessionID>``) into /out/session.json; the app stores the
export on the run so the model's reasoning (thinking) blocks, tool calls and
full transcript can be inspected or downloaded after the review.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_run", sa.Column("session_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_run", "session_json")
