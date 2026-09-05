"""FakeOrchestrator: canned outcomes, log lines, concurrency high-water mark."""

import threading

from app.orchestrator import Orchestrator, RunOutcome
from app.orchestrator.fake_orchestrator import (
    CANNED_RESULT_JSON,
    FakeOrchestrator,
    FakeOutcome,
)


def _run(run_id=1, job_id=2, profile_id=3):
    run = type("Run", (), {"id": run_id})()
    job = type("Job", (), {"id": job_id, "model_profile_id": profile_id})()
    return run, job


def test_default_is_success_with_canned_result():
    orch = FakeOrchestrator()
    assert isinstance(orch, Orchestrator)
    run, job = _run()
    logs: list[str] = []
    outcome = orch.run_review(run, job, log_chunk=logs.append)

    assert isinstance(outcome, RunOutcome)
    assert outcome.exit_code == 0
    assert outcome.result_json == CANNED_RESULT_JSON
    assert outcome.error is None
    assert outcome.timed_out is False
    assert orch.run_count == 1
    assert orch.max_seen == 1
    assert any("starting" in line for line in logs)
    assert any("finished" in line for line in logs)


def test_configurable_outcomes():
    run, job = _run()
    ok = FakeOrchestrator.success(result_json={"summary": "custom"}).run_review(run, job)
    assert ok.result_json == {"summary": "custom"}
    assert ok.exit_code == 0

    failed = FakeOrchestrator.failure(exit_code=3, error="boom").run_review(run, job)
    assert failed.exit_code == 3
    assert failed.error == "boom"
    assert failed.result_json is None

    timed = FakeOrchestrator.timeout().run_review(run, job)
    assert timed.timed_out is True
    assert timed.exit_code == 124

    # set_outcome swaps the canned result in place
    orch = FakeOrchestrator()
    assert orch.run_review(run, job).exit_code == 0
    orch.set_outcome(FakeOutcome(exit_code=7, result_json=None, error="late failure"))
    assert orch.run_review(run, job).exit_code == 7


def test_max_seen_tracks_concurrent_runs():
    orch = FakeOrchestrator(delay=0.1)
    run, job = _run()

    results = []

    def worker():
        results.append(orch.run_review(run, job))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert orch.run_count == 4
    assert orch.max_seen >= 2  # at least some overlap given the delay
    assert all(r.exit_code == 0 for r in results)
    assert orch._active == 0  # counter returns to zero


def test_no_log_chunk_is_fine():
    orch = FakeOrchestrator()
    run, job = _run()
    outcome = orch.run_review(run, job)  # no log_chunk passed
    assert outcome.exit_code == 0
