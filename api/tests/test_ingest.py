import pytest
from sqlalchemy import create_engine, select

from doug import ingest, store
from doug.models import Band, Reason, Verdict

INSTALL = 150424894
REPO_ID = 900001
REPO = "drewjst/doug"

VERDICT = Verdict(
    score=0.4,
    band=Band.CLEARED,
    threshold=0.62,
    reasons=[Reason(rule="size", label="small change", weight=0.0)],
)


def _db(tmp_path, monkeypatch) -> str:
    url = f"sqlite:///{tmp_path}/doug.db"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def _jobs(url: str) -> list[dict]:
    with create_engine(url).connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                select(store.review_jobs).order_by(store.review_jobs.c.id)
            ).mappings()
        ]


def test_enqueue_suppresses_a_redelivered_push(tmp_path, monkeypatch):
    """GitHub delivers at least once, not exactly once, and a duplicate that
    got through would buy a second model read of a diff already queued. The
    unique index is the guard; enqueue reports the suppression as None rather
    than inventing a job id."""
    url = _db(tmp_path, monkeypatch)
    first = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    assert first is not None
    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) is None
    assert [j["id"] for j in _jobs(url)] == [first]


def test_a_new_head_sha_supersedes_the_pending_job_it_replaces(tmp_path, monkeypatch):
    """A five-commit push burst is five deliveries for one PR. Reviewing every
    intermediate SHA costs five model reads to describe a tree nobody will
    merge, so only the newest pending SHA survives."""
    url = _db(tmp_path, monkeypatch)
    old = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    new = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "b" * 40)
    by_id = {j["id"]: j for j in _jobs(url)}
    assert by_id[old]["status"] == "superseded"
    assert by_id[new]["status"] == "pending"


def test_a_finished_job_is_never_superseded(tmp_path, monkeypatch):
    """Only pending work is cheap to discard. A done job has already been paid
    for and its verdict is in the ledger; rewriting its status would make the
    row lie about what happened."""
    url = _db(tmp_path, monkeypatch)
    done = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.complete(done, None)
    ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "b" * 40)
    assert {j["id"]: j["status"] for j in _jobs(url)}[done] == "done"


def test_claim_takes_the_oldest_pending_job_and_marks_it_running(tmp_path, monkeypatch):
    """Two drains must never take the same job, so a claim is a write, not a
    read. Oldest-first keeps one busy repo from starving another."""
    url = _db(tmp_path, monkeypatch)
    first = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    second = ingest.enqueue(INSTALL, REPO_ID, REPO, 8, "b" * 40)

    job = ingest.claim()
    assert job["id"] == first
    assert job["status"] == "running" and job["started_at"] is not None
    assert job["repo_full_name"] == REPO and job["head_sha"] == "a" * 40

    assert {j["id"]: j["status"] for j in _jobs(url)}[first] == "running"
    assert ingest.claim()["id"] == second


def test_claim_returns_none_when_nothing_is_pending(tmp_path, monkeypatch):
    """The drain loop's stop condition. An empty queue is the normal state."""
    _db(tmp_path, monkeypatch)
    assert ingest.claim() is None


def test_fail_re_pends_below_the_cap_and_gives_up_at_it(tmp_path, monkeypatch):
    """A model call that times out should be retried; one that fails three
    times is broken, and a job that retried forever would spend real money
    doing it."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)

    queued_at = {j["id"]: j for j in _jobs(url)}[job_id]["enqueued_at"]
    ingest.fail(job_id, "read timed out")
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "pending" and row["attempts"] == 1
    assert row["error"] == "read timed out"
    assert row["started_at"] is None  # re-pended, not still running
    # Behind the rest of the queue, not back at its head: claim() orders by
    # enqueued_at, so an untouched timestamp hands the job straight back and
    # burns all three attempts in one drain, in milliseconds.
    assert row["enqueued_at"] > queued_at

    ingest.fail(job_id, "read timed out")
    ingest.fail(job_id, "read timed out")
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "failed" and row["attempts"] == 3
    assert row["finished_at"] is not None


def test_fail_truncates_the_error(tmp_path, monkeypatch):
    """Anthropic and githubkit both raise exceptions carrying whole request
    bodies. One job must not be able to write a megabyte into the queue."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.fail(job_id, "x" * 5000)
    assert len({j["id"]: j for j in _jobs(url)}[job_id]["error"]) == 500


def test_complete_records_the_verdict_the_job_produced(tmp_path, monkeypatch):
    """The queue row is the only link from a delivery to the ledger row it
    caused; without it, "did this push get reviewed?" is unanswerable."""
    url = _db(tmp_path, monkeypatch)
    verdict_id = store.save_review(REPO, 7, "deterministic", VERDICT, source="app")
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)

    ingest.complete(job_id, verdict_id)
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "done"
    assert row["verdict_id"] == verdict_id and row["finished_at"] is not None


def test_enqueue_without_a_ledger_refuses_loudly(tmp_path, monkeypatch):
    """Storage is optional everywhere else in this codebase — save_review
    no-ops without DATABASE_URL. It cannot be optional here: a silent no-op
    would return the same None that means "already queued", and every webhook
    on an unconfigured deployment would report success while reviewing
    nothing."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)


def test_a_failed_job_is_revived_by_a_later_enqueue(tmp_path, monkeypatch):
    """The unique index carries no status column, so a row that gave up blocks
    re-insertion exactly like a reviewed one. Reconcile exists to heal PRs
    whose review never landed — a deploy that wiped the App credentials, a
    provider outage — and if a collision with a 'failed' row returned None it
    would heal nothing, permanently, on that PR and every restart after."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    for _ in range(3):
        ingest.fail(job_id, "credentials missing")
    assert {j["id"]: j for j in _jobs(url)}[job_id]["status"] == "failed"

    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) == job_id
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["error"] is None and row["finished_at"] is None
    # One row, not two, and the same id. worker.drain bounds a
    # supersede/revive ping-pong with a seen-set of job ids, so a revival
    # that allocated a fresh id would quietly turn that bound back into an
    # unbounded loop.
    assert len(_jobs(url)) == 1


def test_a_superseded_job_is_revived_by_a_later_enqueue(tmp_path, monkeypatch):
    """GitHub does not order deliveries, so the SHA that lost the supersede
    race can be the PR's real head. The worker re-enqueues whatever head it
    finds, and that call has to be able to bring the row back."""
    url = _db(tmp_path, monkeypatch)
    first = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "b" * 40)
    assert {j["id"]: j for j in _jobs(url)}[first]["status"] == "superseded"

    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) == first
    assert {j["id"]: j for j in _jobs(url)}[first]["status"] == "pending"


def test_running_and_finished_jobs_are_never_revived(tmp_path, monkeypatch):
    """Only the two states that mean "queued and never reviewed" come back.
    Reviving in-flight or completed work is exactly the duplicate spend the
    unique index exists to prevent."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.claim()
    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) is None
    assert {j["id"]: j for j in _jobs(url)}[job_id]["status"] == "running"

    ingest.complete(job_id, None)
    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) is None
    assert {j["id"]: j for j in _jobs(url)}[job_id]["status"] == "done"


def test_a_replayed_older_delivery_leaves_the_newer_job_pending(tmp_path, monkeypatch):
    """Supersede runs after the insert lands, not before it. Running it first
    meant a redelivered older push superseded the newer pending job and then
    collided on its own row, leaving the PR with nothing pending and no
    further delivery coming to fix it."""
    url = _db(tmp_path, monkeypatch)
    ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    newer = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "b" * 40)

    ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)  # the replay
    assert {j["id"]: j["status"] for j in _jobs(url)}[newer] == "pending"


def test_release_returns_a_claimed_job_without_charging_an_attempt(tmp_path, monkeypatch):
    """drain has to claim a job before it can tell whether it already ran it
    this pass. Undoing that claim cannot cost an attempt — the job was never
    attempted, and charging it would retire a healthy PR after three drains
    that never touched it."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    queued_at = {j["id"]: j for j in _jobs(url)}[job_id]["enqueued_at"]
    ingest.claim()

    ingest.release(job_id)
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["started_at"] is None
    # Keeps its place, unlike a failure: nothing was attempted.
    assert row["enqueued_at"] == queued_at
    assert ingest.claim()["id"] == job_id


def test_supersede_retires_a_job_whose_sha_is_no_longer_the_head(tmp_path, monkeypatch):
    """The worker's stale-head guard needs a terminal state that is honest:
    not 'done', because no verdict exists, and not 'failed', because nothing
    went wrong. Revivable, so a force-push back to this SHA re-queues the row
    instead of colliding with it forever."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.claim()

    ingest.supersede(job_id)
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "superseded" and row["finished_at"] is not None
    assert row["verdict_id"] is None

    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) == job_id
    assert {j["id"]: j for j in _jobs(url)}[job_id]["status"] == "pending"
