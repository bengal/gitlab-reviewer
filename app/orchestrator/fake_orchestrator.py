"""In-memory orchestrator for dev and tests: no podman, no disk writes.

Configurable per-instance outcome (success with a canned result_json by
default, failure, or timeout) plus an optional ``delay`` per run so concurrent
calls overlap. Concurrency accounting: ``run_count`` counts calls and
``max_seen`` records the highest number of overlapping ``run_review`` calls —
M6 uses it to assert the ``max_concurrent_reviews`` cap.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.orchestrator import RunOutcome

CANNED_RESULT_JSON: dict = {
    "summary": "Automated fake review: no issues found.",
    "findings": {
        "critical": [],
        "important": [],
        "minor": [],
        "positive": [{"file": "README.md", "line": None, "description": "Documentation present."}],
    },
    "commit_message_review": "",
    "questions": [],
}

# A miniature stand-in for ``opencode export <sessionID>``: the shape the
# review-runner's session capture produces (info + messages, with a
# reasoning part so the "model thinking" UI path is exercised too).
CANNED_SESSION_JSON: dict = {
    "info": {
        "id": "ses_fake0000000000000000000000",
        "title": "fake review session",
        "agent": "build",
        "model": {"id": "fake-model", "providerID": "fake"},
        "version": "fake",
    },
    "messages": [
        {
            "role": "user",
            "parts": [{"type": "text", "text": "Review the MR diff."}],
        },
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "reasoning",
                    "text": "Thinking: the diff is small, check the one changed file.",
                },
                {"type": "text", "text": "Automated fake review: no issues found."},
            ],
        },
    ],
}


@dataclass
class FakeOutcome:
    """A canned RunOutcome; the default is success with CANNED_RESULT_JSON
    plus CANNED_SESSION_JSON."""

    exit_code: int = 0
    result_json: dict | None = field(default_factory=lambda: CANNED_RESULT_JSON)
    result_markdown: str | None = None
    session_json: dict | None = field(default_factory=lambda: CANNED_SESSION_JSON)
    error: str | None = None
    timed_out: bool = False


class FakeOrchestrator:
    """Orchestrator stand-in: instant, deterministic, concurrency-accounting."""

    def __init__(self, *, outcome: FakeOutcome | None = None, delay: float = 0.0):
        self._outcome = outcome or FakeOutcome()
        self._delay = delay
        self._active = 0
        self.max_seen = 0
        self.run_count = 0

    def set_outcome(self, outcome: FakeOutcome) -> None:
        """Swap the canned outcome (e.g. mid-suite success -> failure)."""
        self._outcome = outcome

    @classmethod
    def success(
        cls,
        *,
        result_json: dict | None = None,
        session_json: dict | None = None,
        delay: float = 0.0,
    ) -> "FakeOrchestrator":
        return cls(
            outcome=FakeOutcome(
                result_json=result_json if result_json is not None else CANNED_RESULT_JSON,
                session_json=session_json if session_json is not None else CANNED_SESSION_JSON,
            ),
            delay=delay,
        )

    @classmethod
    def failure(
        cls, *, exit_code: int = 1, error: str = "simulated failure", delay: float = 0.0
    ) -> "FakeOrchestrator":
        return cls(
            outcome=FakeOutcome(
                exit_code=exit_code,
                result_json=None,
                result_markdown=None,
                session_json=None,
                error=error,
            ),
            delay=delay,
        )

    @classmethod
    def timeout(cls, *, delay: float = 0.0) -> "FakeOrchestrator":
        return cls(
            outcome=FakeOutcome(
                exit_code=124,
                result_json=None,
                result_markdown=None,
                session_json=None,
                error="simulated timeout",
                timed_out=True,
            ),
            delay=delay,
        )

    def stop_run_container(self, run_id: int) -> None:
        """No-op: the fake backend runs in-process and has no containers."""

    def run_review(
        self,
        run,
        job,
        *,
        log_chunk: Callable[[str], None] | None = None,
    ) -> RunOutcome:
        self._active += 1
        self.max_seen = max(self.max_seen, self._active)
        self.run_count += 1
        try:
            if self._delay:
                time.sleep(self._delay)
            if log_chunk is not None:
                log_chunk(
                    f"[fake] starting review run {getattr(run, 'id', '?')}"
                    f" for job {getattr(job, 'id', '?')}"
                )
                log_chunk(f"[fake] model profile id {getattr(job, 'model_profile_id', '?')}")
                log_chunk(
                    f"[fake] finished: exit_code={self._outcome.exit_code}"
                    f" timed_out={self._outcome.timed_out}"
                )
            return RunOutcome(
                exit_code=self._outcome.exit_code,
                result_json=self._outcome.result_json,
                result_markdown=self._outcome.result_markdown,
                session_json=self._outcome.session_json,
                error=self._outcome.error,
                timed_out=self._outcome.timed_out,
            )
        finally:
            self._active -= 1
