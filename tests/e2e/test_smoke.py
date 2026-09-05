"""Full in-process e2e smoke (MILESTONES M7).

Fresh app (fake orchestrator, scheduler disabled, tmp DB) exercising the whole
happy path in one scenario:

1. log in through the real /login route;
2. seed the settings row (GitLab url/project/token) + one model profile;
3. sync via POST /mrs/sync against a monkeypatched GitLabClient (2 MRs);
4. schedule MR#1 immediate + MR#2 nightly (scheduling.enqueue);
5. pump + drain -> MR#1's run reaches success with result_json findings;
6. schedule two more immediate jobs, reorder via POST /queue/reorder, pump
   -> execution order matches the new order;
7. promote_nightly() + pump -> MR#2's nightly job is done;
8. archive MR#1's run via POST /results/{id}/archive -> it appears in
   /archive and is excluded from the default /results view.

The real TestClient is used everywhere a route exists (login, /mrs, /queue,
/results, /archive); services are called directly where no route exists
(sync, scheduling, pump). No network, podman, or real GitLab.
"""

import threading

from sqlalchemy import select

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
from app.orchestrator import RunOutcome
from app.orchestrator.fake_orchestrator import FakeOrchestrator, FakeOutcome
from app.routers import settings as settings_router
from app.scheduler import worker
from app.schemas.mr import MrSnapshot
from app.security import encrypt_secret
from app.services import mr_sync, review_service, scheduling

PROJECT = "group/project"
GITLAB_URL = "https://gitlab.example.com"
GITLAB_TOKEN = "glpat-smoke-abcdef123456"

# The structured result the fake orchestrator produces: findings must be
# present so the smoke test asserts on real content, not just the canned shape.
SMOKE_RESULT: dict = {
    "summary": "Smoke review: one important issue found.",
    "findings": {
        "critical": [],
        "important": [
            {"file": "src/smoke.py", "line": 10, "description": "off-by-one in loop bound"}
        ],
        "minor": [],
        "positive": [{"file": "README.md", "line": None, "description": "docs updated"}],
    },
    "commit_message_review": "commit message is fine",
    "questions": [],
}


class FakeGitLabClient:
    """Stand-in for app.services.gitlab_client.GitLabClient: two canned MRs."""

    def __init__(self, settings):
        self.settings = settings

    def list_open_merge_requests(self) -> list[MrSnapshot]:
        return [
            MrSnapshot(
                iid=1,
                title="Add feature",
                author="alice",
                source_branch="feature",
                target_branch="main",
                sha="a" * 40,
                web_url=f"{GITLAB_URL}/{PROJECT}/-/merge_requests/1",
                state="opened",
                updated_at=None,
            ),
            MrSnapshot(
                iid=2,
                title="Fix bug",
                author="bob",
                source_branch="fix-bug",
                target_branch="main",
                sha="b" * 40,
                web_url=f"{GITLAB_URL}/{PROJECT}/-/merge_requests/2",
                state="opened",
                updated_at=None,
            ),
        ]

    def test_connection(self) -> tuple[bool, str]:
        return True, "fake connection ok"

    def post_note(self, iid: int, body: str) -> int:
        return 4242


class RecordingFake(FakeOrchestrator):
    """FakeOrchestrator that records the job execution order (for the
    reorder check; with max_concurrent_reviews=1 order is deterministic)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.order: list[int] = []
        self._lock = threading.Lock()

    def run_review(self, run, job, *, log_chunk=None) -> RunOutcome:
        with self._lock:
            self.order.append(job.id)
        return super().run_review(run, job, log_chunk=log_chunk)


def test_full_smoke(app, client, db, test_password, monkeypatch):
    # -- 1. login through the real route -----------------------------------
    resp = client.post("/login", data={"password": test_password}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "nm_review_session" in client.cookies
    assert client.get("/").status_code == 200

    # -- 2. seed settings row + one model profile ---------------------------
    row = get_settings_row(db)
    row.gitlab_url = GITLAB_URL
    row.gitlab_project = PROJECT
    row.gitlab_token = encrypt_secret(GITLAB_TOKEN)
    row.default_review_prompt = "Review this merge request."
    row.max_concurrent_reviews = 2
    row.poll_interval_seconds = 5
    db.commit()

    profile = ModelProfile(
        name="smoke-model",
        provider="local",
        model_id="qwen3:smoke",
        base_url="http://llama.test:8080/v1",
        is_default=True,
    )
    db.add(profile)
    db.commit()

    # -- 3. fake GitLab: patch the GitLabClient name in every namespace that
    #        constructs it (mr_sync for the sync service, the settings router
    #        for test-connection, review_service for the post-back) ---------
    monkeypatch.setattr(mr_sync, "GitLabClient", FakeGitLabClient)
    monkeypatch.setattr(settings_router, "GitLabClient", FakeGitLabClient)
    monkeypatch.setattr(review_service, "GitLabClient", FakeGitLabClient)

    # -- 4. sync -> 2 MRs in the DB, visible on /mrs ------------------------
    resp = client.post("/mrs/sync")
    assert resp.status_code == 200
    assert "2 added" in resp.text

    db.expire_all()
    mrs = {mr.iid: mr for mr in db.scalars(select(MergeRequest))}
    assert set(mrs) == {1, 2}

    listing = client.get("/mrs")
    assert listing.status_code == 200
    assert "!1" in listing.text and "Add feature" in listing.text
    assert "!2" in listing.text and "Fix bug" in listing.text

    mr1, mr2 = mrs[1], mrs[2]

    # -- 5. schedule MR#1 immediate + MR#2 nightly --------------------------
    job1 = scheduling.enqueue(db, mr=mr1, profile=profile, schedule_type=ScheduleType.immediate)
    job2 = scheduling.enqueue(db, mr=mr2, profile=profile, schedule_type=ScheduleType.nightly)
    assert job1.schedule_type == ScheduleType.immediate
    assert job2.schedule_type == ScheduleType.nightly

    # -- 6. pump -> MR#1's run succeeds with findings ------------------------
    orchestrator = RecordingFake(outcome=FakeOutcome(result_json=SMOKE_RESULT))
    app.state.orchestrator = orchestrator

    started = worker.pump_once()
    worker.drain()
    assert len(started) == 1  # only the immediate job is claimed, nightly waits
    db.expire_all()

    assert db.get(ScheduledJob, job1.id).status == JobStatus.done
    assert db.get(ScheduledJob, job2.id).status == JobStatus.queued  # still nightly

    run1 = db.scalar(select(ReviewRun).where(ReviewRun.scheduled_job_id == job1.id))
    assert run1 is not None
    assert run1.status == RunStatus.success.value
    assert run1.exit_code == 0
    assert run1.result_json["findings"]["important"] == SMOKE_RESULT["findings"]["important"]
    assert run1.result_json["findings"]["positive"] == SMOKE_RESULT["findings"]["positive"]
    assert run1.log  # a log was captured
    assert run1.started_at is not None and run1.finished_at is not None

    # -- 7. two more immediate jobs: reorder, then execution follows --------
    # Serialize execution so the claim order is fully deterministic.
    row.max_concurrent_reviews = 1
    db.commit()

    job3 = scheduling.enqueue(db, mr=mr1, profile=profile, schedule_type=ScheduleType.immediate)
    job4 = scheduling.enqueue(db, mr=mr2, profile=profile, schedule_type=ScheduleType.immediate)
    assert (job3.position, job4.position) == (1, 2)  # creation order

    resp = client.post("/queue/reorder", data={"ids": [str(job4.id), str(job3.id)]})
    assert resp.status_code == 200
    db.expire_all()
    assert db.get(ScheduledJob, job4.id).position == 1
    assert db.get(ScheduledJob, job3.id).position == 2

    worker.pump_once()
    worker.drain()
    assert orchestrator.order == [job1.id, job4.id, job3.id]  # reorder honored

    db.expire_all()
    assert db.get(ScheduledJob, job3.id).status == JobStatus.done
    assert db.get(ScheduledJob, job4.id).status == JobStatus.done

    # -- 8. promote nightly -> MR#2's nightly job runs to done --------------
    promoted = scheduling.promote_nightly(db)
    assert [job.id for job in promoted] == [job2.id]

    worker.pump_once()
    worker.drain()
    db.expire_all()
    assert db.get(ScheduledJob, job2.id).status == JobStatus.done
    run2 = db.scalar(select(ReviewRun).where(ReviewRun.scheduled_job_id == job2.id))
    assert run2 is not None
    assert run2.status == RunStatus.success.value

    # every job ran exactly once: 4 runs total
    assert orchestrator.order == [job1.id, job4.id, job3.id, job2.id]
    assert len(list(db.scalars(select(ReviewRun)))) == 4

    # -- 9. archive MR#1's first run: /archive shows it, /results hides it --
    resp = client.post(f"/results/{run1.id}/archive")
    assert resp.status_code == 200
    db.expire_all()
    assert db.get(ReviewRun, run1.id).archived_at is not None

    results = client.get("/results")
    assert results.status_code == 200
    assert f"/results/{run1.id}" not in results.text  # excluded from default view
    assert f"/results/{run2.id}" in results.text  # the others are still listed

    archive = client.get("/archive")
    assert archive.status_code == 200
    assert f"/archive/{run1.id}" in archive.text

    detail = client.get(f"/archive/{run1.id}")
    assert detail.status_code == 200
    assert "off-by-one in loop bound" in detail.text  # findings still readable
