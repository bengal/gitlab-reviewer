"""Application settings singleton table."""

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SettingsRow(Base):
    """Singleton settings row (id is always 1).

    gitlab_token is stored encrypted at rest (see app.security.encrypt_secret).
    """

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    gitlab_url: Mapped[str] = mapped_column(String(512), default="")
    gitlab_project: Mapped[str] = mapped_column(String(512), default="")
    gitlab_token: Mapped[str] = mapped_column(Text, default="")
    default_review_prompt: Mapped[str] = mapped_column(Text, default="")
    nightly_time: Mapped[str] = mapped_column(String(5), default="02:30")
    max_concurrent_reviews: Mapped[int] = mapped_column(Integer, default=2)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    post_results_to_gitlab: Mapped[bool] = mapped_column(Boolean, default=False)
    known_libraries: Mapped[list] = mapped_column(JSON, default=list)
    # The project's default branch, cached by mr_sync so the MR list can
    # hide a redundant "→ <default>" target branch. None until first sync.
    gitlab_default_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
