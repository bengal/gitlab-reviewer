"""Scheduling service: enqueue ordering, reorder, cancel/move, nightly set,
promotion, and claim semantics (positions, statuses, validation)."""

import pytest
from sqlalchemy import select

from app.models import (
    JobStatus,
    MergeRequest,
    ModelProfile,
    ScheduledJob,
    ScheduleType,
)
from app.services import scheduling


@pytest.fixture()
def mr(db):
    row = MergeRequest(
        project="group/proj",
        iid=1,
        title="First MR",
        author="alice",
        source_branch="feature-1",
        target_branch="main",
        sha="a" * 40,
        web_url="https://gitlab.example.com/group/proj/-/merge_requests/1",
        state="opened",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def profile(db):
    row = ModelProfile(name="claude", provider="anthropic", model_id="claude-sonnet-4")
    db.add(row)
    db.commit()
    return row


def enqueue(db, mr, profile, kind=ScheduleType.immediate, **kwargs) -> ScheduledJob:
    return scheduling.enqueue(db, mr=mr, profile=profile, schedule_type=kind, **kwargs)


def positions(db, kind=ScheduleType.immediate) -> dict[int, int]:
    """{job_id: position} of the queued jobs of the given type."""
    rows = db.scalars(
        select(ScheduledJob).where(
            ScheduledJob.schedule_type == kind,
            ScheduledJob.status == JobStatus.queued.value,
        )
    ).all()
    return {job.id: job.position for job in rows}


# -- enqueue -----------------------------------------------------------------


def test_enqueue_appends_within_schedule_type_set(db, mr, profile):
    a = enqueue(db, mr, profile)
    b = enqueue(db, mr, profile)
    c = enqueue(db, mr, profile)
    nightly = enqueue(db, mr, profile, kind=ScheduleType.nightly)
    d = enqueue(db, mr, profile)

    assert a.position == 1
    assert b.position == 2
    assert c.position == 3
    assert nightly.position == 1  # separate set
    assert d.position == 4
    assert all(job.status == JobStatus.queued for job in (a, b, c, nightly, d))


def test_enqueue_stores_fields(db, mr, profile):
    job = enqueue(
        db,
        mr,
        profile,
        prompt_override="  look at the error handling  ",
        extra_projects=[{"url": "https://gitlab.example.com/group/lib", "ref": "v1", "path": "lib"}],
        post_to_gitlab=True,
    )
    db.refresh(job)
    assert job.prompt_override == "look at the error handling"
    assert job.extra_projects == [
        {"url": "https://gitlab.example.com/group/lib", "ref": "v1", "path": "lib"}
    ]
    assert job.post_to_gitlab is True
    assert job.created_at is not None
    assert job.updated_at is not None


def test_enqueue_blank_prompt_override_becomes_none(db, mr, profile):
    job = enqueue(db, mr, profile, prompt_override="   ")
    db.refresh(job)
    assert job.prompt_override is None
    assert job.extra_projects == []
    assert job.post_to_gitlab is None


# -- reorder -----------------------------------------------------------------


def test_reorder_rewrites_positions_exactly(db, mr, profile):
    a = enqueue(db, mr, profile)
    b = enqueue(db, mr, profile)
    c = enqueue(db, mr, profile)

    scheduling.reorder(db, [c.id, a.id, b.id])

    assert positions(db) == {c.id: 1, a.id: 2, b.id: 3}


def test_reorder_rejects_non_queued_ids(db, mr, profile):
    a = enqueue(db, mr, profile)
    b = enqueue(db, mr, profile)
    c = enqueue(db, mr, profile)
    a.status = JobStatus.claimed.value
    db.commit()

    with pytest.raises(ValueError):
        scheduling.reorder(db, [a.id, b.id, c.id])
    # nightly jobs are not part of the immediate queue
    n = enqueue(db, mr, profile, kind=ScheduleType.nightly)
    with pytest.raises(ValueError):
        scheduling.reorder(db, [b.id, c.id, n.id])
    # unknown ids
    with pytest.raises(ValueError):
        scheduling.reorder(db, [b.id, c.id, 9999])
    # a stale set (missing a live queued job that arrived later) is rejected
    d = enqueue(db, mr, profile)
    with pytest.raises(ValueError):
        scheduling.reorder(db, [b.id, c.id])
    # positions untouched by the failed attempts: a's claim left a gap at 1
    # (d appended at max+1 = 4); compact_positions fills the gap
    assert positions(db) == {b.id: 2, c.id: 3, d.id: 4}
    scheduling.compact_positions(db)
    assert positions(db) == {b.id: 1, c.id: 2, d.id: 3}


def test_reorder_empty_list_rejected(db, mr, profile):
    enqueue(db, mr, profile)
    with pytest.raises(ValueError):
        scheduling.reorder(db, [])


# -- cancel / move ------------------------------------------------------------


def test_cancel_queued_job_and_compact(db, mr, profile):
    a = enqueue(db, mr, profile)
    b = enqueue(db, mr, profile)
    c = enqueue(db, mr, profile)

    scheduling.cancel(db, b)

    db.refresh(b)
    assert b.status == JobStatus.cancelled
    assert positions(db) == {a.id: 1, c.id: 2}


def test_cancel_rejects_non_queued(db, mr, profile):
    a = enqueue(db, mr, profile)
    a.status = JobStatus.done.value
    db.commit()
    with pytest.raises(ValueError):
        scheduling.cancel(db, a)


def test_move_to_top_and_bottom(db, mr, profile):
    a = enqueue(db, mr, profile)
    b = enqueue(db, mr, profile)
    c = enqueue(db, mr, profile)

    scheduling.move_to_top(db, c)
    assert positions(db) == {c.id: 1, a.id: 2, b.id: 3}

    scheduling.move_to_bottom(db, c)
    assert positions(db) == {a.id: 1, b.id: 2, c.id: 3}


def test_move_rejects_non_queued_immediate(db, mr, profile):
    a = enqueue(db, mr, profile)
    nightly = enqueue(db, mr, profile, kind=ScheduleType.nightly)
    a.status = JobStatus.claimed.value
    db.commit()

    with pytest.raises(ValueError):
        scheduling.move_to_top(db, nightly)
    with pytest.raises(ValueError):
        scheduling.move_to_bottom(db, a)


# -- nightly set ---------------------------------------------------------------


def test_enroll_and_remove_nightly(db, mr, profile):
    a = enqueue(db, mr, profile)
    b = enqueue(db, mr, profile)

    scheduling.enroll_nightly(db, b)
    db.refresh(b)
    assert b.schedule_type == ScheduleType.nightly
    assert b.position == 1
    assert positions(db) == {a.id: 1}
    assert [job.id for job in scheduling.list_nightly(db)] == [b.id]

    # removing from the nightly set re-queues it at the end of the live queue
    scheduling.remove_nightly(db, b)
    db.refresh(b)
    assert b.schedule_type == ScheduleType.immediate
    assert b.position == 2
    assert positions(db) == {a.id: 1, b.id: 2}


def test_enroll_nightly_rejects_non_queued(db, mr, profile):
    a = enqueue(db, mr, profile)
    a.status = JobStatus.running.value
    db.commit()
    with pytest.raises(ValueError):
        scheduling.enroll_nightly(db, a)


def test_remove_nightly_rejects_non_nightly(db, mr, profile):
    a = enqueue(db, mr, profile)
    with pytest.raises(ValueError):
        scheduling.remove_nightly(db, a)


def test_promote_nightly_flips_with_fresh_positions(db, mr, profile):
    a = enqueue(db, mr, profile)
    b = enqueue(db, mr, profile)
    x = enqueue(db, mr, profile, kind=ScheduleType.nightly)
    y = enqueue(db, mr, profile, kind=ScheduleType.nightly)

    promoted = scheduling.promote_nightly(db)
    assert [job.id for job in promoted] == [x.id, y.id]  # nightly order preserved

    assert positions(db) == {a.id: 1, b.id: 2, x.id: 3, y.id: 4}
    assert scheduling.list_nightly(db) == []

    # nothing left to promote
    assert scheduling.promote_nightly(db) == []


# -- claim ---------------------------------------------------------------------


def test_claim_next_picks_min_position_and_claims(db, mr, profile):
    a = enqueue(db, mr, profile)
    b = enqueue(db, mr, profile)
    nightly = enqueue(db, mr, profile, kind=ScheduleType.nightly)

    claimed = scheduling.claim_next(db)
    assert claimed is not None and claimed.id == a.id
    assert claimed.status == JobStatus.claimed
    db.refresh(a)
    assert a.status == JobStatus.claimed

    claimed = scheduling.claim_next(db)
    assert claimed is not None and claimed.id == b.id

    # the nightly job is never claimed by the pump
    assert scheduling.claim_next(db) is None
    db.refresh(nightly)
    assert nightly.status == JobStatus.queued


def test_claim_next_returns_none_when_empty(db):
    assert scheduling.claim_next(db) is None
