"""The review_jobs queue — the durable gap between a webhook and a review.

A delivery must be recorded and answered in milliseconds; a review takes a
model call. Everything between those two facts lives in this table: the
webhook enqueues and returns 202, a worker claims and runs. Nothing is held
in process memory, so a Cloud Run instance dying mid-review loses a claim,
not a review.

Uniqueness is (installation_id, github_repo_id, pr_number, head_sha) and it
is enforced by the database, not by a check-then-insert: two deliveries of
the same push arrive concurrently often enough that a race here would mean
paying for the same read twice.
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import store

# The two states a collision may revive. Both mean "this SHA was queued and
# never reviewed"; every other state means the work is queued, in flight, or
# already paid for.
REVIVABLE = ("failed", "superseded")


def _engine():
    engine = store._get_engine()
    if engine is None:
        raise RuntimeError("review_jobs requires DATABASE_URL")
    return engine


def _job_filter(installation_id: int, github_repo_id: int, pr_number: int):
    return (
        store.review_jobs.c.installation_id == installation_id,
        store.review_jobs.c.github_repo_id == github_repo_id,
        store.review_jobs.c.pr_number == pr_number,
    )


def enqueue(
    installation_id: int,
    github_repo_id: int,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
) -> int | None:
    """Queue one head SHA for review. None means this SHA needs no new work.

    A collision on the unique index is not automatically a duplicate. The
    index carries no status column, so a row that ended 'failed' or
    'superseded' blocks re-insertion exactly like a reviewed one — and those
    are the two states reconcile exists to repair. Colliding with one revives
    it rather than dropping the work; the cost of a permanently broken PR is
    therefore max_attempts per reconcile, which is bounded, instead of never
    being retried again on any restart.

    Superseding older pending SHAs happens after the insert lands and in the
    same transaction. Running it first meant a redelivered older push
    superseded the newer pending job and then collided on its own row,
    leaving the PR with nothing pending at all. It is a spend optimisation
    for the ordinary in-order burst, not the thing that decides which SHA
    gets reviewed: GitHub does not order deliveries, so worker.process_job
    re-checks the PR's real head before paying for a read.
    """
    engine = _engine()
    now = datetime.now(UTC)
    try:
        with engine.begin() as conn:
            job_id = int(
                conn.execute(
                    store.review_jobs.insert().returning(store.review_jobs.c.id),
                    {
                        "installation_id": installation_id,
                        "github_repo_id": github_repo_id,
                        "repo_full_name": repo_full_name,
                        "pr_number": pr_number,
                        "head_sha": head_sha,
                        "status": "pending",
                        "attempts": 0,
                        "enqueued_at": now,
                    },
                ).scalar_one()
            )
            conn.execute(
                update(store.review_jobs)
                .where(
                    *_job_filter(installation_id, github_repo_id, pr_number),
                    store.review_jobs.c.head_sha != head_sha,
                    store.review_jobs.c.status == "pending",
                )
                .values(status="superseded", finished_at=now)
            )
            return job_id
    except IntegrityError:
        return _revive(engine, installation_id, github_repo_id, pr_number, head_sha, now)


def _revive(
    engine,
    installation_id: int,
    github_repo_id: int,
    pr_number: int,
    head_sha: str,
    now: datetime,
) -> int | None:
    """Return a queued-but-unreviewed row to pending, or None if there is none.

    The status test lives in the UPDATE's WHERE rather than in a SELECT before
    it: a concurrent drain can claim or finish the row between the two, and a
    zero-row result is the only reliable way to find out that it did.

    The row is updated in place and keeps its id — never deleted and
    re-inserted. worker.drain bounds a supersede/revive ping-pong (its
    stale-head guard supersedes a job, which this then revives) with a
    seen-set of job ids, and a fresh id per revival would defeat it silently,
    turning the spin back into an unbounded loop inside a held instance.
    """
    with engine.begin() as conn:
        job_id = conn.execute(
            update(store.review_jobs)
            .where(
                *_job_filter(installation_id, github_repo_id, pr_number),
                store.review_jobs.c.head_sha == head_sha,
                store.review_jobs.c.status.in_(REVIVABLE),
            )
            .values(
                status="pending",
                attempts=0,
                error=None,
                enqueued_at=now,
                started_at=None,
                finished_at=None,
            )
            .returning(store.review_jobs.c.id)
        ).scalar_one_or_none()
    return int(job_id) if job_id is not None else None


def claim() -> dict | None:
    """Take the oldest pending job, or None. Marks it running before returning.

    Ordering is (enqueued_at, id), which is what makes fail()'s bump of
    enqueued_at put a re-pended job behind the rest of the queue rather than
    back at its head.

    On Postgres the select takes a row lock with SKIP LOCKED, so concurrent
    drains take different jobs instead of blocking on the same one. sqlite
    has one writer by construction, so the plain transaction is already the
    same guarantee.
    """
    engine = store._get_engine()
    if engine is None:
        return None
    pending = (
        select(store.review_jobs)
        .where(store.review_jobs.c.status == "pending")
        .order_by(store.review_jobs.c.enqueued_at, store.review_jobs.c.id)
        .limit(1)
    )
    if engine.dialect.name == "postgresql":
        pending = pending.with_for_update(skip_locked=True)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        row = conn.execute(pending).mappings().first()
        if row is None:
            return None
        conn.execute(
            update(store.review_jobs)
            .where(store.review_jobs.c.id == row["id"])
            .values(status="running", started_at=now)
        )
        return {**row, "status": "running", "started_at": now}


def release(job_id: int) -> None:
    """Put a claimed job back without spending an attempt.

    drain claims a job before it can tell whether it has already run it this
    pass. Leaving the repeat 'running' strands it, and fail() would charge an
    attempt against work nobody attempted. enqueued_at is deliberately
    untouched — unlike a failure, nothing here justifies losing its place.
    """
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            update(store.review_jobs)
            .where(store.review_jobs.c.id == job_id)
            .values(status="pending", started_at=None)
        )


def complete(job_id: int, verdict_id: int | None) -> None:
    """Mark a job done. verdict_id is None when the review produced no ledger
    row — a skipped PR is finished, not failed."""
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            update(store.review_jobs)
            .where(store.review_jobs.c.id == job_id)
            .values(status="done", verdict_id=verdict_id, finished_at=datetime.now(UTC))
        )


def supersede(job_id: int) -> None:
    """Retire a job whose head SHA is no longer the PR's.

    Neither 'done' — there is no verdict — nor 'failed', since nothing went
    wrong. It lands in a revivable state on purpose: a force-push back to
    this SHA re-queues this row rather than being suppressed by it.
    """
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            update(store.review_jobs)
            .where(store.review_jobs.c.id == job_id)
            .values(status="superseded", finished_at=datetime.now(UTC))
        )


def fail(job_id: int, error: str, *, max_attempts: int = 3) -> None:
    """Record a failed attempt: back to pending below the cap, failed at it.

    started_at is cleared on the retry so a re-pended row is not reported as
    having been running since its first attempt.
    """
    engine = _engine()
    now = datetime.now(UTC)
    with engine.begin() as conn:
        attempts = (
            conn.execute(
                select(store.review_jobs.c.attempts).where(store.review_jobs.c.id == job_id)
            ).scalar_one()
            + 1
        )
        values = {"attempts": attempts, "error": error[:500], "started_at": None}
        if attempts >= max_attempts:
            values |= {"status": "failed", "finished_at": now}
        else:
            # Back of the queue, not the front. claim() orders by
            # (enqueued_at, id), so leaving enqueued_at alone hands the job
            # straight back to the next claim and burns every attempt in one
            # pass, before whatever was transient has any chance to clear.
            # This orders it behind existing work; worker.drain's seen-set is
            # what stops a re-claim when it is the only pending row.
            values |= {"status": "pending", "enqueued_at": now}
        conn.execute(
            update(store.review_jobs).where(store.review_jobs.c.id == job_id).values(**values)
        )
