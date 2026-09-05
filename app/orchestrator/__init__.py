"""Review execution substrate: one short-lived container (or fake) per run.

``Orchestrator`` is the interface the worker (M6) programs against;
``PodmanOrchestrator`` implements it with ``podman run`` (no ``--rm``:
failed/timed-out containers are kept for post-mortems) and
``FakeOrchestrator`` is an in-memory stand-in for dev/tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.config import Settings
    from app.models import ReviewRun, ScheduledJob


@dataclass
class RunOutcome:
    """Raw outcome of one review execution.

    Mapping to review_run.status happens in the service layer (M6):
    ``success`` when exit 0 or a result was captured, ``timeout`` when
    ``timed_out``, otherwise ``error``.
    """

    exit_code: int
    result_json: dict | None = None
    result_markdown: str | None = None
    error: str | None = None
    timed_out: bool = False


@runtime_checkable
class Orchestrator(Protocol):
    """Executes one claimed job as an isolated, throwaway review run."""

    def run_review(
        self,
        run: ReviewRun,
        job: ScheduledJob,
        *,
        log_chunk: Callable[[str], None] | None = None,
    ) -> RunOutcome:
        """Run the review for ``run``/``job``.

        ``log_chunk`` receives each streamed stdout/stderr line (already
        scrubbed of secrets); pass None to discard output.
        """
        ...


def create_orchestrator(settings: Settings | None = None) -> Orchestrator:
    """Pick the orchestrator backend per config (M6 stores it on app.state)."""
    from app.config import get_settings

    settings = settings or get_settings()
    if settings.orchestrator == "fake":
        from app.orchestrator.fake_orchestrator import FakeOrchestrator

        return FakeOrchestrator()
    from app.orchestrator.podman_client import PodmanOrchestrator

    return PodmanOrchestrator(settings=settings)


__all__ = ["Orchestrator", "RunOutcome", "create_orchestrator"]
