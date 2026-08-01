from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

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


def _age_finished_at(url: str, job_id: int, seconds: int) -> None:
    """Push a failed job's finished_at into the past, standing in for the
    cooloff elapsing. Same reason as _age_started_at: the alternative is a
    real multi-minute sleep in the suite."""
    with create_engine(url).begin() as conn:
        conn.execute(
            store.review_jobs.update()
            .where(store.review_jobs.c.id == job_id)
            .values(finished_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )


def _age_started_at(url: str, job_id: int, seconds: int) -> None:
    """Push a claimed job's started_at into the past, standing in for real
    wall-clock time passing while an instance holds (or crashes with) a
    claim — reclaim_stalled's lease check has no other way to be exercised
    without an actual multi-minute sleep."""
    with create_engine(url).begin() as conn:
        conn.execute(
            store.review_jobs.update()
            .where(store.review_jobs.c.id == job_id)
            .values(started_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )


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
    would heal nothing, permanently, on that PR and every restart after.

    On the sweep's own terms the healing is delayed by
    FAILED_REVIVE_COOLOFF_SECONDS, not withheld, so this ages the row past it —
    the cooloff bounds how often a poison PR can re-arm, and is tested in its
    own right below."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    for _ in range(3):
        ingest.fail(job_id, "credentials missing")
    assert {j["id"]: j for j in _jobs(url)}[job_id]["status"] == "failed"
    _age_finished_at(url, job_id, seconds=ingest.FAILED_REVIVE_COOLOFF_SECONDS + 60)

    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40, trigger="reconcile") == job_id
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


def test_reclaim_stalled_re_pends_a_running_job_past_its_lease(tmp_path, monkeypatch):
    """A crashed instance leaves its claim at 'running' forever — nothing
    else in this module can bring the row back, because REVIVABLE excludes
    'running' on purpose. This is the only path that turns an abandoned
    claim back into reviewable work, and it must not charge the crash as an
    attempt against the job."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.claim()
    _age_started_at(url, job_id, seconds=ingest.STALL_LEASE_SECONDS + 1)

    assert ingest.reclaim_stalled() == 1
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["started_at"] is None


def test_reclaim_stalled_leaves_a_fresh_claim_strictly_alone(tmp_path, monkeypatch):
    """The anti-double-spend guarantee: a job a live worker is still holding
    must never be re-pended out from under it, or two workers pay for the
    same model read of the same SHA. Only claims older than the lease are
    fair game."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.claim()

    assert ingest.reclaim_stalled() == 0
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "running"


def test_a_reclaimed_job_rejoins_the_ordinary_lifecycle(tmp_path, monkeypatch):
    """Without reclaim, a crashed instance's claim is a dead end: 'running'
    is never claimed again and a later enqueue collides with it forever, so
    reconcile can never queue that PR's review again, on any restart. Once
    reclaimed the row is ordinary pending work — claimable, and if it goes
    on to fail out, revivable by a later enqueue exactly like any other
    failed job. This is the guarantee the module docstring now promises in
    place of the durability claim it used to overstate."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.claim()
    _age_started_at(url, job_id, seconds=ingest.STALL_LEASE_SECONDS + 1)
    ingest.reclaim_stalled()

    assert ingest.claim()["id"] == job_id  # claimable again, not stuck

    for _ in range(3):
        ingest.fail(job_id, "still broken")
    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) == job_id


def test_reclaim_stalled_is_a_no_op_when_storage_is_disabled(monkeypatch):
    """Storage-disabled is a no-op here, matching claim() rather than the
    RuntimeError the rest of this module raises — drain and reconcile call
    this unconditionally on a cadence and must not need a try/except."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert ingest.reclaim_stalled() == 0


def test_claim_returns_none_when_storage_is_disabled(monkeypatch):
    """claim() is worker.drain's stop condition and its safe no-op on an
    unconfigured deployment — the locked contract returns None for both an
    empty queue and a missing ledger, with no exception either way, which is
    what lets drain skip a try/except around it."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert ingest.claim() is None


def test_enqueue_only_swallows_the_review_jobs_unique_violation(tmp_path, monkeypatch):
    """The IntegrityError catch around the insert exists to turn one
    specific collision — the uq_review_job dedupe index — into a revive-or-
    None result. It must not silently absorb an unrelated constraint
    violation on a table this code doesn't even touch and misreport it as
    'already queued'; that is exactly the silent-success mode the
    RuntimeError guard on a missing ledger exists to prevent elsewhere in
    this module. Exercises a real IntegrityError from a different table's
    unique constraint, not a mock, so the discriminator is proven against
    genuine driver error text."""
    _db(tmp_path, monkeypatch)
    engine = store._get_engine()
    row = {
        "installation_id": INSTALL,
        "github_repo_id": REPO_ID,
        "full_name": REPO,
        "state": "active",
        "updated_at": datetime.now(UTC),
    }
    with engine.begin() as conn:
        conn.execute(store.installation_repos.insert(), row)
    with pytest.raises(IntegrityError) as exc_info:
        with engine.begin() as conn:
            conn.execute(store.installation_repos.insert(), row)
    assert not ingest._is_dedupe_collision(exc_info.value)


def test_complete_clears_a_stale_error_from_earlier_failed_attempts(tmp_path, monkeypatch):
    """A job that failed once and then succeeded on retry must not land
    'done' still carrying the error text from the attempt that didn't work —
    a reader checking why a PR wasn't reviewed would be told about a problem
    that no longer exists."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.fail(job_id, "transient timeout")

    ingest.complete(job_id, None)
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "done" and row["error"] is None


def test_a_revived_job_clears_a_stray_verdict_id(tmp_path, monkeypatch):
    """_revive's reset list is the invariant for what "never reviewed" means
    on a revived row; verdict_id belongs in it even though no code path
    today leaves one behind on a failed or superseded row — complete() only
    ever sets verdict_id on its way to 'done', which isn't revivable. Seeds
    one directly to hold that invariant even if a future change ever makes
    it reachable, rather than trusting it by omission."""
    url = _db(tmp_path, monkeypatch)
    verdict_id = store.save_review(REPO, 7, "deterministic", VERDICT, source="app")
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.supersede(job_id)
    with create_engine(url).begin() as conn:
        conn.execute(
            store.review_jobs.update()
            .where(store.review_jobs.c.id == job_id)
            .values(verdict_id=verdict_id)
        )

    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) == job_id
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "pending" and row["verdict_id"] is None


def test_the_dedupe_marker_still_matches_the_real_constraint_name():
    """enqueue tells "this SHA is already queued" apart from a genuine
    integrity failure by matching the driver's error text, because neither
    sqlite nor psycopg exposes the violated constraint in a portable way.
    That makes the constraint's NAME load-bearing from a second file: rename
    it in store.py and every redelivery stops deduping and starts 5xx-ing the
    webhook, with nothing here to catch it. Pin the two together so a rename
    fails this test instead of production.
    """
    names = {c.name for c in store.review_jobs.constraints if c.name}
    assert "uq_review_job" in names, (
        f"review_jobs' unique constraint was renamed; {names} no longer contains the name "
        "ingest._DEDUPE_COLLISION matches on"
    )
    assert any("uq_review_job" in marker for marker in ingest._DEDUPE_COLLISION)


def test_a_live_event_revives_a_failed_job_without_waiting_out_the_cooloff(
    tmp_path, monkeypatch
):
    """The cooloff belongs to the caller, not to the row. A reopen, or a
    force-push back to the SHA whose review failed during an outage, is a
    person asking for this PR now, and what enqueue promises a live caller is
    "queue this SHA for review". Suppressing that for an hour is a PR silently
    never reviewed — the opposite failure from the one the cooloff exists to
    bound, which is a restart loop re-arming a poison PR. Only the reconcile
    sweep pays that penalty; the default caller does not.
    """
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    for _ in range(3):
        ingest.fail(job_id, "reader exploded")
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    # finished_at is set and recent: the row really is inside the cooloff
    # window, so reviving it is the caller's doing and not _revive's
    # NULL-finished_at escape hatch.
    assert row["status"] == "failed" and row["finished_at"] is not None

    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) == job_id
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "pending" and row["attempts"] == 0


def test_a_reconcile_sweep_does_not_re_arm_a_job_that_just_burned_its_attempts(
    tmp_path, monkeypatch
):
    """Reconcile runs on every startup, and a Cloud Run service cold-starts
    often. Without a cooloff, a permanently-broken PR is re-armed on each one
    and the drain burns max_attempts paid model reads against it every time —
    a bill that scales with restart frequency rather than with anything the
    customer did. Healing a transient failure an hour later is worth having;
    retrying a poison PR every ninety seconds forever is not.
    """
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    for _ in range(3):
        ingest.fail(job_id, "reader exploded")
    assert {j["id"]: j for j in _jobs(url)}[job_id]["status"] == "failed"

    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40, trigger="reconcile") is None
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "failed" and row["attempts"] == 3


def test_a_reconcile_sweep_revives_a_failed_job_once_its_cooloff_has_passed(
    tmp_path, monkeypatch
):
    """The cooloff bounds the retry rate; it must not strand the PR. A genuine
    outage — wiped credentials, a provider down — has to heal on a later
    restart, which is the whole reason revive exists."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    for _ in range(3):
        ingest.fail(job_id, "reader exploded")

    stale = datetime.now(UTC) - timedelta(seconds=ingest.FAILED_REVIVE_COOLOFF_SECONDS + 60)
    with create_engine(url).begin() as conn:
        conn.execute(
            store.review_jobs.update()
            .where(store.review_jobs.c.id == job_id)
            .values(finished_at=stale)
        )

    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40, trigger="reconcile") == job_id
    row = {j["id"]: j for j in _jobs(url)}[job_id]
    assert row["status"] == "pending" and row["attempts"] == 0


def test_a_superseded_job_revives_immediately_on_either_path(tmp_path, monkeypatch):
    """The cooloff is a penalty for failing, not for being overtaken, and it is
    charged to the caller that repeats itself — so neither reason to wait
    applies to a 'superseded' row on either path. A branch force-pushed back to
    an earlier SHA is an instruction to review that SHA now, whether the
    instruction reaches the queue as a delivery or is re-derived by the startup
    sweep; making it wait an hour would be a bug, not a saving."""
    url = _db(tmp_path, monkeypatch)
    job_id = ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40)
    ingest.supersede(job_id)
    assert {j["id"]: j for j in _jobs(url)}[job_id]["status"] == "superseded"

    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40, trigger="reconcile") == job_id
    assert {j["id"]: j for j in _jobs(url)}[job_id]["status"] == "pending"

    ingest.supersede(job_id)
    assert ingest.enqueue(INSTALL, REPO_ID, REPO, 7, "a" * 40) == job_id
    assert {j["id"]: j for j in _jobs(url)}[job_id]["status"] == "pending"
