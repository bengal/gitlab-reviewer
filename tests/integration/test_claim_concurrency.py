"""Claim concurrency against a real (tmp) SQLite database.

8 threads x 50 claim attempts race over 30 seeded queued jobs. The SQLite
BEGIN IMMEDIATE path must serialize the select+update so that exactly 30
claims succeed, each job is claimed exactly once, and no exception escapes.
"""

import threading

from sqlalchemy import select

from app.db import get_db_session
from app.models import JobStatus, MergeRequest, ModelProfile, ScheduledJob, ScheduleType
from app.services import scheduling

NUM_JOBS = 30
NUM_THREADS = 8
CLAIMS_PER_THREAD = 50


def test_concurrent_claims_claim_each_job_exactly_once(app, db):
    profile = ModelProfile(name="claude", provider="anthropic", model_id="claude-sonnet-4")
    mrs = [MergeRequest(project="group/proj", iid=i, title=f"MR {i}") for i in range(1, NUM_JOBS + 1)]
    db.add_all([profile, *mrs])
    db.commit()
    for position, mr in enumerate(mrs, start=1):
        db.add(
            ScheduledJob(
                merge_request_id=mr.id,
                model_profile_id=profile.id,
                schedule_type=ScheduleType.immediate.value,
                position=position,
                status=JobStatus.queued.value,
            )
        )
    db.commit()

    claimed_ids: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        for _ in range(CLAIMS_PER_THREAD):
            session = get_db_session()
            try:
                job = scheduling.claim_next(session)
                if job is not None:
                    with lock:
                        claimed_ids.append(job.id)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                session.close()

    threads = [threading.Thread(target=worker) for _ in range(NUM_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, [repr(exc) for exc in errors[:3]]
    assert len(claimed_ids) == NUM_JOBS
    assert len(set(claimed_ids)) == NUM_JOBS  # no double-claim

    rows = list(db.scalars(select(ScheduledJob)))
    assert {job.id for job in rows} == set(claimed_ids)
    assert all(job.status == JobStatus.claimed for job in rows)

    # the queue is drained
    session = get_db_session()
    try:
        assert scheduling.claim_next(session) is None
    finally:
        session.close()
