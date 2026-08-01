"""One claimed job in, one check run out.

The webhook must never review inline, so everything expensive lives here.
These tests cut all five network seams (installation token, PR fetch,
scoring, intent read, check run) and assert on what survives in the
ledger, because the ledger row is the product — the check run is a copy.
"""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, select

from doug import app_auth, check_run, ingest, reader, review, store, worker
from doug.models import Band, PRMetadata, Reason, Verdict

JOB = dict(
    installation_id=150424894,
    github_repo_id=987,
    repo_full_name="drewjst/doug",
    pr_number=7,
    head_sha="a" * 40,
)

RV = reader.ReaderVerdict.model_validate(
    {
        "risk_score": 62,
        "rationale": "Unlocked cache write.",
        "findings": [
            {
                "category_slug": "race-condition",
                "description": "Cache write is not guarded",
                "file": "cache.py",
                "severity": "high",
            }
        ],
    }
)

VERDICT = Verdict(
    score=0.62,
    band=Band.FLAGGED,
    threshold=0.30,
    reasons=[
        Reason(rule="reader:race-condition", label="Cache write is not guarded", weight=0.0)
    ],
)

COV = reader.Coverage(diff_chars=400, sent_chars=400, files_sent=1, files_unseen=[])


def _db(tmp_path, monkeypatch) -> str:
    url = f"sqlite:///{tmp_path}/doug.db"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def _pr() -> PRMetadata:
    return PRMetadata.model_validate(
        dict(number=7, title="Add cache", author="dev", files=["cache.py"])
    )


def _gh(heads: dict[int, str] | None = None):
    """A client whose pulls.get reports the PR's current head SHA.

    By default that is the head of the newest job queued for the PR — the
    branch has not moved since enqueue, which is the ordinary case and
    keeps every other test free of SHA bookkeeping. `heads` moves it, which
    is how a test simulates a push landing between enqueue and claim.
    """
    heads = heads or {}

    def _get(*, owner, repo, pull_number):
        sha = heads.get(pull_number)
        if sha is None:
            with create_engine(os.environ["DATABASE_URL"]).connect() as conn:
                sha = conn.execute(
                    select(store.review_jobs.c.head_sha)
                    .where(store.review_jobs.c.pr_number == pull_number)
                    .order_by(store.review_jobs.c.id.desc())
                    .limit(1)
                ).scalar_one()
        return SimpleNamespace(parsed_data=SimpleNamespace(head=SimpleNamespace(sha=sha)))

    return SimpleNamespace(rest=SimpleNamespace(pulls=SimpleNamespace(get=_get)))


def _wire(monkeypatch, *, tier="reader", intent=None, fetch=None, heads=None) -> list[dict]:
    """Cut every seam that would touch the network. Returns the posted
    check runs, which is what a caller of this pipeline can observe."""
    posted: list[dict] = []
    gh = _gh(heads)
    monkeypatch.setattr(app_auth, "installation_client", lambda i: gh)
    monkeypatch.setattr(review, "fetch_pr", fetch or (lambda gh, o, r, n: (_pr(), "+ x")))
    monkeypatch.setattr(
        review,
        "score_one",
        lambda meta, diff: (
            tier,
            VERDICT.model_copy(deep=True),
            RV if tier == "reader" else None,
            COV if tier == "reader" else None,
        ),
    )
    monkeypatch.setattr(review, "read_intent", lambda gh, o, r, m, d: intent)
    monkeypatch.setattr(
        check_run,
        "post",
        lambda gh, o, r, sha, title, summary: posted.append(
            dict(owner=o, repo=r, head_sha=sha, title=title, summary=summary)
        ),
    )
    return posted


def _rows(url, table):
    with create_engine(url).connect() as conn:
        return [dict(r) for r in conn.execute(select(table)).mappings()]


def _age_started_at(url: str, job_id: int, seconds: int) -> None:
    """Push a claimed job's started_at into the past, standing in for real
    wall-clock time passing while an instance holds (or crashes with) a
    claim — same helper as test_ingest.py's, kept local since this is the
    only place worker.drain's use of the lease needs it."""
    with create_engine(url).begin() as conn:
        conn.execute(
            store.review_jobs.update()
            .where(store.review_jobs.c.id == job_id)
            .values(started_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )


def test_process_job_persists_with_the_app_identity_columns(tmp_path, monkeypatch):
    """Tenancy identity (Global Constraints): every App-path write carries
    the installation, the numeric repo id and the head SHA. A row keyed
    only on "drewjst/doug" cannot be scoped to a customer and does not
    survive a repo rename — the name is display-only."""
    url = _db(tmp_path, monkeypatch)
    _wire(monkeypatch)
    job_id = ingest.enqueue(**JOB)
    verdict_id = worker.process_job(ingest.claim())

    (v,) = _rows(url, store.verdicts)
    (j,) = _rows(url, store.review_jobs)
    assert v["id"] == verdict_id
    assert v["source"] == "app"
    assert v["installation_id"] == JOB["installation_id"]
    assert v["github_repo_id"] == JOB["github_repo_id"]
    assert v["head_sha"] == JOB["head_sha"]
    assert v["repo"] == "drewjst/doug" and v["pr_number"] == 7
    assert v["tier"] == "reader" and v["model"] == reader.MODEL
    assert j["id"] == job_id and j["status"] == "done" and j["verdict_id"] == verdict_id


def test_the_reader_tier_records_the_coverage_it_read_at(tmp_path, monkeypatch):
    url = _db(tmp_path, monkeypatch)
    _wire(monkeypatch)
    ingest.enqueue(**JOB)
    worker.process_job(ingest.claim())
    (r,) = _rows(url, store.reads)
    assert r["diff_chars"] == 400 and r["sent_chars"] == 400


def test_the_deterministic_tier_claims_no_model_and_no_coverage(tmp_path, monkeypatch):
    """model is the reader's provenance. Stamping it on a fallback row
    would make the ledger claim opus-5 scored a PR whose diff was never
    opened, and every precision number computed over tier would be wrong."""
    url = _db(tmp_path, monkeypatch)
    _wire(monkeypatch, tier="deterministic")
    ingest.enqueue(**JOB)
    worker.process_job(ingest.claim())
    (v,) = _rows(url, store.verdicts)
    assert v["tier"] == "deterministic" and v["model"] is None
    assert _rows(url, store.reads) == []


def test_the_check_run_is_posted_against_the_jobs_head_sha(tmp_path, monkeypatch):
    """Not the PR's current SHA. A push burst means pulls.get already
    returns a newer commit than the one this job was enqueued for, and
    hanging this verdict on it would attach a read of one diff to a
    different one — while that newer SHA has a job of its own."""
    _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch)
    ingest.enqueue(**JOB)
    worker.process_job(ingest.claim())
    assert posted[0]["head_sha"] == JOB["head_sha"]
    assert (posted[0]["owner"], posted[0]["repo"]) == ("drewjst", "doug")
    assert posted[0]["title"].lower().startswith("flagged")


def _intent(findings=None):
    return review.IntentRead(
        alignment=41,
        refs=["ADR-0002"],
        findings=findings
        if findings is not None
        else [
            reader.DeviationFinding(
                type="contradicts-ticket",
                description="Edits the frozen reader prompt",
                severity="high",
            )
        ],
        coverage=COV,
    )


def test_deviations_are_recorded_against_the_verdict(tmp_path, monkeypatch):
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch, intent=_intent())
    ingest.enqueue(**JOB)
    verdict_id = worker.process_job(ingest.claim())

    (d,) = _rows(url, store.deviations)
    assert d["verdict_id"] == verdict_id
    assert d["kind"] == "contradicts-ticket" and d["intent_alignment"] == 41
    (v,) = _rows(url, store.verdicts)
    assert v["score"] == 0.62 and v["band"] == "flagged"
    assert "unvalidated" in posted[0]["summary"].lower()


def test_a_failed_deviation_write_does_not_cost_the_verdict(tmp_path, monkeypatch):
    """ADR-0007 makes this a separate write, which is exactly why it must
    not be able to fail the job: retrying would re-run a paid read to
    recover a row the risk verdict does not depend on. It is reported on
    the check run instead of being swallowed silently."""
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch, intent=_intent())

    def _boom(*a, **k):
        raise RuntimeError("deviations table is gone")

    monkeypatch.setattr(store, "save_deviations", _boom)
    ingest.enqueue(**JOB)
    worker.process_job(ingest.claim())

    (v,) = _rows(url, store.verdicts)
    (j,) = _rows(url, store.review_jobs)
    assert v["score"] == 0.62
    assert j["status"] == "done" and j["verdict_id"] == v["id"]
    assert "deviations-unrecorded" in posted[0]["summary"]


def test_no_intent_read_writes_no_deviation_row(tmp_path, monkeypatch):
    """"No read happened" and "read happened, found nothing" are different
    facts and store.save_deviations already encodes the second as a
    kind='none' row. The worker must not blur them by calling it anyway."""
    url = _db(tmp_path, monkeypatch)
    _wire(monkeypatch, intent=None)
    ingest.enqueue(**JOB)
    worker.process_job(ingest.claim())
    assert _rows(url, store.deviations) == []


def test_drain_on_an_empty_queue_is_zero(tmp_path, monkeypatch):
    """Every delivery kicks a drain, including the ones that enqueue
    nothing. The common case must cost one claim and return."""
    _db(tmp_path, monkeypatch)
    _wire(monkeypatch)
    assert worker.drain() == 0


def test_drain_is_a_safe_no_op_when_storage_is_disabled(monkeypatch):
    """No DATABASE_URL is a deliberate mode (store.py's opt-in design), not
    a broken deployment, and drain must stay a no-op rather than raising —
    every one of the calls it makes unconditionally (reclaim_stalled, then
    claim) already returns empty/None for this case instead of erroring. A
    raise here would turn a background task into a crash on every request
    on a ledger-less deployment."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert worker.drain() == 0


def test_drain_runs_the_queue_and_marks_each_job_done(tmp_path, monkeypatch):
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch)
    ingest.enqueue(**JOB)
    ingest.enqueue(**{**JOB, "pr_number": 8, "head_sha": "b" * 40})
    assert worker.drain() == 2
    assert {r["status"] for r in _rows(url, store.review_jobs)} == {"done"}
    assert sorted(p["head_sha"] for p in posted) == ["a" * 40, "b" * 40]


def test_a_failing_job_does_not_strand_the_queue(tmp_path, monkeypatch):
    """A poison job — a deleted PR, a revoked token — is claimed before
    every PR opened after it. If its exception escaped the loop, one bad
    job would silently stop reviewing an entire installation."""
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch)

    def _fetch(gh, owner, repo, number):
        if number == 7:
            raise RuntimeError("boom: 404 pull request not found")
        return _pr(), "+ x"

    monkeypatch.setattr(review, "fetch_pr", _fetch)
    ingest.enqueue(**JOB)
    ingest.enqueue(**{**JOB, "pr_number": 8, "head_sha": "b" * 40})

    assert worker.drain() == 2
    rows = {r["pr_number"]: r for r in _rows(url, store.review_jobs)}
    assert rows[7]["status"] == "pending" and rows[7]["attempts"] == 1
    assert "boom" in rows[7]["error"]
    assert rows[8]["status"] == "done"
    assert [p["head_sha"] for p in posted] == ["b" * 40]
    assert _rows(url, store.verdicts)[0]["pr_number"] == 8


def test_a_job_that_keeps_failing_stops_being_retried(tmp_path, monkeypatch):
    """Below the cap a failure is pending (transient: a 502, a token race).
    At the cap it is failed, because re-running a paid read against a PR
    that will never fetch is spend with no possible verdict."""
    url = _db(tmp_path, monkeypatch)
    _wire(monkeypatch)

    def _fetch(gh, owner, repo, number):
        raise RuntimeError("gone")

    monkeypatch.setattr(review, "fetch_pr", _fetch)
    ingest.enqueue(**JOB)
    for _ in range(3):
        worker.drain()
    (j,) = _rows(url, store.review_jobs)
    assert j["status"] == "failed" and j["attempts"] == 3


def test_drain_stops_at_max_jobs(tmp_path, monkeypatch):
    """The drain runs inside a request's background task. Unbounded, a
    backlog would hold the instance long past the response it belongs to —
    the next delivery kicks it again."""
    url = _db(tmp_path, monkeypatch)
    _wire(monkeypatch)
    for n in (7, 8, 9):
        ingest.enqueue(**{**JOB, "pr_number": n, "head_sha": f"{n}" * 40})
    assert worker.drain(max_jobs=2) == 2
    statuses = sorted(r["status"] for r in _rows(url, store.review_jobs))
    assert statuses == ["done", "done", "pending"]


def test_a_failed_job_is_not_retried_inside_the_same_pass(tmp_path, monkeypatch):
    """ingest.fail re-pends a job below the attempt cap, and the drain
    claims whatever is pending — so without a guard one poison job is
    claimed, failed, re-pended and re-claimed until its three attempts are
    gone, inside a single pass lasting under a second. That is not a retry
    policy; nothing has had time to change. Spreading the attempts across
    passes is what makes "transient" a hypothesis worth holding."""
    url = _db(tmp_path, monkeypatch)
    _wire(monkeypatch)

    def _fetch(gh, owner, repo, number):
        raise RuntimeError("502 from GitHub")

    monkeypatch.setattr(review, "fetch_pr", _fetch)
    ingest.enqueue(**JOB)

    assert worker.drain() == 1
    (j,) = _rows(url, store.review_jobs)
    assert j["attempts"] == 1
    # Released, not left running: the next pass has to be able to claim it.
    assert j["status"] == "pending" and j["started_at"] is None


def test_a_stale_head_is_superseded_and_the_current_one_requeued(tmp_path, monkeypatch):
    """A job can wait behind a backlog, or be re-pended by a retry, long
    enough for the branch to move. fetch_pr would then read the NEW diff
    while the identity columns, the unique index and the check run all
    still said the old SHA — a verdict labelled as evidence about a commit
    it never saw. Losing the read would be better than mislabelling it;
    doing neither is better still."""
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch, heads={7: "c" * 40})
    ingest.enqueue(**JOB)

    assert worker.process_job(ingest.claim()) is None

    jobs = {j["head_sha"]: j for j in _rows(url, store.review_jobs)}
    assert jobs["a" * 40]["status"] == "superseded"
    assert jobs["c" * 40]["status"] == "pending"
    # Nothing was paid for and nothing was published against the stale SHA.
    assert _rows(url, store.verdicts) == []
    assert posted == []


def test_the_stale_head_catch_up_revives_a_failed_job_at_once(tmp_path, monkeypatch):
    """The SHA that overtook a stale job is enqueued on live terms, not the
    sweep's. The branch really moved just now, so the row this catch-up
    collides with must come back at once even if its own review failed
    minutes ago — a force-push back onto a SHA whose review died in an outage
    is exactly the case, and FAILED_REVIVE_COOLOFF_SECONDS is a brake on
    reconcile repeating itself at every cold start, never on a push. Left on
    the sweep's terms this returns None and the PR is silently unreviewed for
    an hour, with the check run never posted and nothing to say why."""
    url = _db(tmp_path, monkeypatch)
    _wire(monkeypatch, heads={7: "a" * 40})
    failed_id = ingest.enqueue(**JOB)
    for _ in range(3):
        ingest.fail(failed_id, "reader exploded")
    ingest.enqueue(**{**JOB, "head_sha": "b" * 40})  # a push, then a force-push back

    assert worker.process_job(ingest.claim()) is None  # the "b" job, now stale

    jobs = {j["head_sha"]: j for j in _rows(url, store.review_jobs)}
    assert jobs["b" * 40]["status"] == "superseded"
    revived = jobs["a" * 40]
    assert revived["id"] == failed_id  # the failed row itself, back in the queue
    assert revived["status"] == "pending" and revived["attempts"] == 0


def test_a_force_push_ping_pong_cannot_spin_the_drain(tmp_path, monkeypatch):
    """The seen-set does double duty, and this is the second job.

    ingest.enqueue REVIVES a superseded row rather than inserting beside it
    (Task 3), so a branch flipping between two SHAs makes each job stale on
    arrival, supersede itself, and revive the other. The two hand the queue
    back and forth with no new rows and no progress — an unbounded spin
    inside a request's background task. Claiming a job this pass already
    ran is the signal that the queue has lapped, whatever the reason.

    The bound rests on _revive updating in place: the row keeps its id, so
    the seen-set recognises it. A revive written as a fresh insert — an
    equally natural way to write it, and one every Task 3 test still
    passes — would hand back a new id each time and quietly restore the
    unbounded loop. Two tasks, one mechanism.
    """
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch)
    flip = iter(["c" * 40, "a" * 40] * 40)

    def _get(**kw):
        return SimpleNamespace(parsed_data=SimpleNamespace(head=SimpleNamespace(sha=next(flip))))

    monkeypatch.setattr(
        app_auth,
        "installation_client",
        lambda i: SimpleNamespace(rest=SimpleNamespace(pulls=SimpleNamespace(get=_get))),
    )
    ingest.enqueue(**JOB)

    # Two jobs touched, then the lap is detected — not max_jobs (20) spins.
    assert worker.drain() == 2
    statuses = {j["head_sha"]: j["status"] for j in _rows(url, store.review_jobs)}
    assert statuses == {"a" * 40: "pending", "c" * 40: "superseded"}
    # Nothing was read and nothing was published while the branch thrashed.
    assert _rows(url, store.verdicts) == []
    assert posted == []


# --- amendment: reclaim_stalled wired into drain --------------------------
#
# A worker that claims a job and then dies (a deploy, a scale-down, an OOM)
# leaves the row 'running' forever: REVIVABLE deliberately excludes that
# status, so no later enqueue can revive it on its own (double-spend guard).
# drain() has to call ingest.reclaim_stalled() itself, once per pass, or the
# hole never closes on its own.


def test_a_stalled_claim_past_its_lease_is_reclaimed_and_actually_reviewed(tmp_path, monkeypatch):
    """The end-to-end guarantee: a crashed instance loses its claim, not the
    review. Reclaiming alone (a row flipping back to 'pending') would not be
    enough on its own — this asserts the job flows all the way through the
    ordinary claim path and produces a verdict and a check run."""
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch)
    ingest.enqueue(**JOB)
    stuck = ingest.claim()  # stands in for a worker that claimed and died
    _age_started_at(url, stuck["id"], seconds=ingest.STALL_LEASE_SECONDS + 1)

    assert worker.drain() == 1

    (j,) = _rows(url, store.review_jobs)
    assert j["id"] == stuck["id"] and j["status"] == "done" and j["verdict_id"] is not None
    assert posted[0]["head_sha"] == JOB["head_sha"]
    assert _rows(url, store.verdicts)[0]["installation_id"] == JOB["installation_id"]


def test_a_stalled_claim_within_its_lease_is_left_strictly_alone(tmp_path, monkeypatch):
    """The guarantee that matters more than the first: a claim a live worker
    still holds must never be reclaimed out from under it, or Doug pays
    twice for every slow read. Only wall-clock age past the lease tells a
    crashed worker apart from one still reading; drain must not touch a
    'running' row that is merely young."""
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch)
    ingest.enqueue(**JOB)
    stuck = ingest.claim()  # freshly claimed — well within the lease

    assert worker.drain() == 0

    (j,) = _rows(url, store.review_jobs)
    assert j["id"] == stuck["id"] and j["status"] == "running"
    assert j["started_at"] is not None
    assert _rows(url, store.verdicts) == []
    assert posted == []


# --- fix: idempotent replay for a job whose verdict already landed -------
#
# The amendment above made reclaim_stalled() reachable from drain, which
# reopened a path save_review never defended: if the worker dies (or
# ingest.complete itself raises) anywhere between save_review committing
# and the job reaching 'done', the row re-pends and a naive retry re-scores
# from scratch — a second paid score_one/read_intent, and a second verdicts
# row for the same commit, since verdicts carries no unique constraint.
# process_job now checks store.find_verdict_by_identity before spending
# anything, and replays the durable verdict instead.


def test_a_reclaimed_job_with_an_already_saved_verdict_replays_without_a_second_read(
    tmp_path, monkeypatch
):
    """Stands in for a crash between save_review landing and ingest.complete
    ever running — the earliest possible point in that window, so a replay
    here has to render and post the check run for the first time, not just
    skip re-scoring. Model-call counters, not just row counts, because a
    duplicate verdicts row and a repeated paid call are two different
    failures and this guards both."""
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        review, "fetch_pr", lambda gh, o, r, n: calls.append("fetch_pr") or (_pr(), "+ x")
    )
    monkeypatch.setattr(
        review,
        "score_one",
        lambda meta, diff: calls.append("score_one")
        or ("reader", VERDICT.model_copy(deep=True), RV, COV),
    )
    monkeypatch.setattr(
        review, "read_intent", lambda gh, o, r, m, d: calls.append("read_intent") or None
    )

    ingest.enqueue(**JOB)
    claimed = ingest.claim()
    # The worker reached save_review and then died — before render, before
    # the check-run post, before ingest.complete. The job row is left
    # 'running' with no verdict_id, exactly as a real crash would leave it.
    verdict_id = store.save_review(
        JOB["repo_full_name"],
        JOB["pr_number"],
        "reader",
        VERDICT.model_copy(deep=True),
        RV,
        model=reader.MODEL,
        pr_meta=_pr().model_dump(mode="json"),
        coverage=COV,
        github_repo_id=JOB["github_repo_id"],
        installation_id=JOB["installation_id"],
        head_sha=JOB["head_sha"],
        source="app",
    )
    _age_started_at(url, claimed["id"], seconds=ingest.STALL_LEASE_SECONDS + 1)

    assert worker.drain() == 1

    assert calls == []  # no model call was repeated
    assert len(_rows(url, store.verdicts)) == 1  # no duplicate row
    (j,) = _rows(url, store.review_jobs)
    assert j["status"] == "done" and j["verdict_id"] == verdict_id
    assert posted[0]["head_sha"] == JOB["head_sha"]
    assert posted[0]["title"].lower().startswith("flagged")


def test_ingest_complete_raising_after_a_saved_verdict_does_not_double_score_on_retry(
    tmp_path, monkeypatch
):
    """The idempotency read guards more than the reclaim path: ingest.fail
    re-pends a job whenever process_job raises for any reason, including
    ingest.complete itself blowing up after save_review already landed — no
    wall-clock wait needed to reach the same "verdict durable, job not
    done" state a crash produces. The second drain() pass must not re-score
    and does post a second, harmless, check run — the crash-after-post case
    the fix report calls out as acceptable."""
    url = _db(tmp_path, monkeypatch)
    posted = _wire(monkeypatch)
    real_complete = ingest.complete
    armed = {"boom": True}

    def _flaky_complete(job_id, verdict_id):
        if armed["boom"]:
            armed["boom"] = False
            raise RuntimeError("db hiccup")
        real_complete(job_id, verdict_id)

    monkeypatch.setattr(ingest, "complete", _flaky_complete)
    ingest.enqueue(**JOB)

    assert worker.drain() == 1  # save_review lands, complete blows up, fail() re-pends
    assert worker.drain() == 1  # replay: idempotent, no second read

    assert len(_rows(url, store.verdicts)) == 1
    (j,) = _rows(url, store.review_jobs)
    assert j["status"] == "done"
    assert len(posted) == 2  # both attempts post; the second is the harmless duplicate


# --- reconcile: the healing path for missed deliveries --------------------


def _pull(number=1, head_sha="a" * 40, draft=False, head_repo_id=42, base_repo_id=42):
    return SimpleNamespace(
        number=number,
        draft=draft,
        head=SimpleNamespace(sha=head_sha, repo=SimpleNamespace(id=head_repo_id)),
        base=SimpleNamespace(repo=SimpleNamespace(id=base_repo_id, full_name="o/r")),
    )


class FakeListGH:
    """Only pulls.list — reconcile must never touch pulls.list_files."""

    def __init__(self, pulls):
        self.rest = SimpleNamespace(
            pulls=SimpleNamespace(list=lambda **kw: SimpleNamespace(parsed_data=pulls))
        )


def _installed(tmp_path, monkeypatch, *, repos=((42, "o/r"),)):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/doug.db")
    store.upsert_installation(1, "o", "Organization", "active")
    store.set_installation_repos(1, list(repos), replace=True)


def test_reconcile_enqueues_open_prs_and_skips_drafts(tmp_path, monkeypatch):
    _installed(tmp_path, monkeypatch)
    gh = FakeListGH([_pull(number=1), _pull(number=2, draft=True)])
    monkeypatch.setattr(worker.app_auth, "installation_client", lambda i: gh)
    assert worker.reconcile_installation(1) == 1
    job = ingest.claim()
    assert job["pr_number"] == 1 and job["github_repo_id"] == 42
    assert ingest.claim() is None


def test_reconcile_skips_fork_prs(tmp_path, monkeypatch):
    """A fork's raw diff enters the prompt (_user_text, reader.py:179-187).
    An outside contributor must not be able to drive spend by opening a PR
    during the window when Doug is restarting and reconciling."""
    _installed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        worker.app_auth, "installation_client",
        lambda i: FakeListGH([_pull(head_repo_id=99)]),
    )
    assert worker.reconcile_installation(1) == 0
    assert ingest.claim() is None


def test_reconcile_does_not_requeue_a_reviewed_head_sha(tmp_path, monkeypatch):
    """The property that makes startup reconcile free rather than a full
    re-review: the unique index carries no status, so a head SHA already
    taken to 'done' collides on insert exactly like a pending one."""
    _installed(tmp_path, monkeypatch)
    job_id = ingest.enqueue(1, 42, "o/r", 1, "a" * 40)
    ingest.claim()
    ingest.complete(job_id, None)
    monkeypatch.setattr(
        worker.app_auth, "installation_client",
        lambda i: FakeListGH([_pull(number=1, head_sha="a" * 40)]),
    )
    assert worker.reconcile_installation(1) == 0


def test_reconcile_all_covers_only_active_installations(tmp_path, monkeypatch):
    """A suspended or deleted installation still has rows in the table —
    reconciling it would mint tokens for an App the account revoked."""
    _installed(tmp_path, monkeypatch)
    store.upsert_installation(2, "gone", "User", "suspended")
    store.set_installation_repos(2, [(43, "gone/r")], replace=True)
    seen = []

    def client(installation_id):
        seen.append(installation_id)
        return FakeListGH([_pull(number=installation_id)])

    monkeypatch.setattr(worker.app_auth, "installation_client", client)
    assert worker.reconcile_all() == 1
    assert seen == [1]


def test_reconcile_all_survives_one_failing_installation(tmp_path, monkeypatch):
    """Reconcile runs at startup for every tenant at once, so one revoked or
    rate-limited installation raising would leave every other tenant's
    missed PRs unqueued until the next restart."""
    _installed(tmp_path, monkeypatch)
    store.upsert_installation(2, "ok", "User", "active")
    store.set_installation_repos(2, [(43, "ok/r")], replace=True)

    def client(installation_id):
        if installation_id == 1:
            raise RuntimeError("401 bad installation")
        # Installation 2's repo is (43, "ok/r") — the base repo id must
        # agree, or the new base-repo-id guard (added in review) would skip
        # this PR for the wrong reason and mask what this test checks.
        return FakeListGH([_pull(number=5, head_repo_id=43, base_repo_id=43)])

    monkeypatch.setattr(worker.app_auth, "installation_client", client)
    assert worker.reconcile_all() == 1


# --- amendment: reconcile_all heals crash-stranded claims ------------------
#
# reconcile_installation heals a *missed* PR via ingest.enqueue, but a
# crash-stranded claim is left 'running' — REVIVABLE deliberately excludes
# that status, so enqueue collides and returns None forever. reconcile_all
# must call ingest.reclaim_stalled() once, before the enqueue sweep, or the
# case Task 7 is named for ("a deploy killed the instance mid-review") is
# never actually healed by a restart.


def test_reconcile_all_heals_a_crash_stranded_claim_end_to_end(tmp_path, monkeypatch):
    """The amendment's 'test for intent': a 'running' job stranded past its
    lease is, after reconcile_all, back in a state where its PR actually
    gets reviewed — not merely a row whose status flipped. This scenario
    alone (no head SHA change while the claim was stranded) converges to
    the same final row state whichever side of the sweep reclaim runs on —
    enqueue's collision with a still-'running' zombie and its collision
    with an already-reclaimed 'pending' row both resolve to None, since
    REVIVABLE excludes both. So it does not, by itself, prove reclaim runs
    first; see test_reconcile_all_supersedes_a_stranded_claim_whose_pr_moved_on
    (where the head SHA does change and ordering has a real behavioral
    effect) and test_reconcile_all_calls_reclaim_stalled_before_the_enqueue_sweep
    (which pins call order directly, for this test's own scenario) for
    that."""
    url = f"sqlite:///{tmp_path}/doug.db"
    _installed(tmp_path, monkeypatch)
    job_id = ingest.enqueue(1, 42, "o/r", 1, "a" * 40)
    stuck = ingest.claim()
    assert stuck["id"] == job_id
    _age_started_at(url, stuck["id"], seconds=ingest.STALL_LEASE_SECONDS + 1)

    monkeypatch.setattr(
        worker.app_auth, "installation_client",
        lambda i: FakeListGH([_pull(number=1, head_sha="a" * 40)]),
    )

    assert worker.reconcile_all() == 0  # reclaimed, not (re)enqueued — no new job minted
    (job,) = _rows(url, store.review_jobs)
    assert job["id"] == job_id and job["status"] == "pending"
    # The reclaimed row is claimable again — a worker will actually review it.
    claimed = ingest.claim()
    assert claimed["id"] == job_id and claimed["head_sha"] == "a" * 40


def test_reconcile_all_supersedes_a_stranded_claim_whose_pr_moved_on(tmp_path, monkeypatch):
    """The case where reclaim-before-sweep has a real, observable effect on
    end state, not just on call order: enqueue's supersede-after-insert
    step (ingest.py) only retires rows that are already 'pending' at this
    (installation, repo, pr) with a different head_sha — it has no effect
    on a row that is still 'running'. A claim stranded at sha A whose PR
    force-pushed to sha B while it was stuck needs reclaim to run first, so
    the sweep's insert of B can supersede A in the same pass. Reclaiming
    after would leave both A and B 'pending' — A as live work a worker
    would claim and then have to supersede itself, instead of the sweep
    having already retired it."""
    url = f"sqlite:///{tmp_path}/doug.db"
    _installed(tmp_path, monkeypatch)
    job_id = ingest.enqueue(1, 42, "o/r", 1, "a" * 40)
    stuck = ingest.claim()
    assert stuck["id"] == job_id
    _age_started_at(url, stuck["id"], seconds=ingest.STALL_LEASE_SECONDS + 1)

    # The PR moved on while the claim was stranded: it now reports "b" * 40.
    monkeypatch.setattr(
        worker.app_auth, "installation_client",
        lambda i: FakeListGH([_pull(number=1, head_sha="b" * 40)]),
    )

    assert worker.reconcile_all() == 1  # b*40 is genuinely new work
    jobs = {j["head_sha"]: j["status"] for j in _rows(url, store.review_jobs)}
    assert jobs["a" * 40] == "superseded"
    assert jobs["b" * 40] == "pending"


def test_reconcile_all_calls_reclaim_stalled_before_the_enqueue_sweep(tmp_path, monkeypatch):
    """Pins the amendment's ordering requirement directly against call
    order. test_reconcile_all_supersedes_a_stranded_claim_whose_pr_moved_on
    already catches a swap behaviorally for the force-push case; this one
    catches it even when no head SHA changes — the scenario
    test_reconcile_all_heals_a_crash_stranded_claim_end_to_end documents as
    converging to the same final state under either ordering. Tracking
    which of ingest.reclaim_stalled / ingest.enqueue fires first is what
    does that."""
    order: list[str] = []
    real_reclaim = ingest.reclaim_stalled
    real_enqueue = ingest.enqueue
    monkeypatch.setattr(
        ingest,
        "reclaim_stalled",
        lambda *a, **k: order.append("reclaim") or real_reclaim(*a, **k),
    )
    monkeypatch.setattr(
        ingest,
        "enqueue",
        lambda *a, **k: order.append("enqueue") or real_enqueue(*a, **k),
    )
    _installed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        worker.app_auth, "installation_client",
        lambda i: FakeListGH([_pull(number=1)]),
    )

    worker.reconcile_all()

    assert "enqueue" in order  # sanity: the sweep did run
    assert order[0] == "reclaim"


def test_reconcile_all_calls_reclaim_stalled_not_reconcile_installation(tmp_path, monkeypatch):
    """Scope note from the amendment: reclaim_stalled sweeps the whole queue
    by lease age, not by tenant, so it belongs in the startup path
    (reconcile_all), not per-installation — a per-installation call would
    sweep other tenants' rows as a side effect of one installation's event.
    Pinned directly against the function object rather than behaviourally,
    since reconcile_installation alone has no stalled row in scope to prove
    it either way."""
    calls = []
    real = ingest.reclaim_stalled
    monkeypatch.setattr(ingest, "reclaim_stalled", lambda *a, **k: calls.append(1) or real())
    _installed(tmp_path, monkeypatch)
    monkeypatch.setattr(worker.app_auth, "installation_client", lambda i: FakeListGH([]))

    worker.reconcile_installation(1)
    assert calls == []

    worker.reconcile_all()
    assert calls == [1]


# --- fix: pagination, tenancy identity, and the dedupe/revive comment -----
#
# A code review ran probes rather than reading, and found three Important
# gaps and three cheap Minors in the reconcile implementation above:
#   1. the ordering-equivalence claim in reconcile_all's docstring (and the
#      test docstring that repeated it) was false for the force-push case —
#      fixed above, by adding the supersede test and correcting both
#      docstrings, rather than down here.
#   2. gh.rest.pulls.list(..., per_page=50) fetched one page, silently
#      capping "every open PR" at 50 on a busy repo.
#   3. the dedupe comment claimed a 'done' row and a 'failed' row collide
#      identically; a 'failed' row is instead revived and spent again.
# Plus three minors: _skip_reason's return value was computed and
# discarded; the draft gate didn't apply its own "unknown means skip"
# principle; and full_name-based reconcile trusted a possibly-stale name
# instead of checking the base repo id GitHub actually reports.


def test_reconcile_installation_paginates_past_the_first_page(tmp_path, monkeypatch):
    """gh.rest.pulls.list caps a single response at 100 results. Before this
    fix, one unpaginated call meant a repo with more than 50 open PRs (or,
    after bumping per_page, 100) was healed only in part — permanently and
    silently, under a docstring that promised 'every reviewable open PR'.
    This fakes a two-page repo (100 + 1) and asserts both pages' PRs are
    enqueued, not just the first."""
    _installed(tmp_path, monkeypatch)
    page1 = [_pull(number=n, head_sha=f"{n:040d}") for n in range(1, 101)]
    page2 = [_pull(number=101, head_sha=f"{101:040d}")]

    def _list(*, page=1, **kw):
        data = {1: page1, 2: page2}.get(page, [])
        return SimpleNamespace(parsed_data=data)

    gh = SimpleNamespace(rest=SimpleNamespace(pulls=SimpleNamespace(list=_list)))
    monkeypatch.setattr(worker.app_auth, "installation_client", lambda i: gh)

    url = f"sqlite:///{tmp_path}/doug.db"
    assert worker.reconcile_installation(1) == 101
    seen = {j["pr_number"] for j in _rows(url, store.review_jobs)}
    assert seen == set(range(1, 102))


def test_reconcile_installation_caps_and_logs_a_pathological_repo(tmp_path, monkeypatch, capsys):
    """The pagination loop still needs a ceiling: an unbounded loop against
    a repo with thousands of open PRs would hang reconcile_all() for every
    other tenant queued behind it. Hitting the cap must be loud, not a
    silent truncation of the same kind Finding 2 exists to fix."""
    _installed(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_MAX_OPEN_PRS_PER_REPO", 150)

    def _list(*, page=1, **kw):
        # Every page comes back full — an unbounded repo, capped by us, not
        # by GitHub running out of pages.
        start = (page - 1) * 100 + 1
        return SimpleNamespace(
            parsed_data=[_pull(number=n, head_sha=f"{n:040d}") for n in range(start, start + 100)]
        )

    gh = SimpleNamespace(rest=SimpleNamespace(pulls=SimpleNamespace(list=_list)))
    monkeypatch.setattr(worker.app_auth, "installation_client", lambda i: gh)

    count = worker.reconcile_installation(1)
    assert count == 150  # capped, not 200 (two full pages) and not unbounded
    assert "capped at 150" in capsys.readouterr().err


def test_reconcile_all_revives_a_pr_that_burned_all_its_attempts(tmp_path, monkeypatch):
    """A PR that burned every retry is not dead forever — ingest._revive
    resets a 'failed' row to 'pending' with attempts=0, which is how a
    review lost to a real outage heals on a later restart.

    The cost of that, which Doug's own review of this PR flagged: reconcile
    runs at every startup, so without a brake a permanently-broken PR
    re-arms max_attempts paid reads on each one, and the bill scales with
    how often the service cold-starts rather than with anything the
    customer did. FAILED_REVIVE_COOLOFF_SECONDS is the brake. This pins
    both halves through reconcile_all — not just ingest.enqueue, which
    test_ingest.py covers directly — because the startup path is where the
    repetition actually comes from.
    """
    url = f"sqlite:///{tmp_path}/doug.db"
    _installed(tmp_path, monkeypatch)
    job_id = ingest.enqueue(1, 42, "o/r", 1, "a" * 40)
    for _ in range(3):
        ingest.fail(job_id, "credentials missing")
    (failed,) = _rows(url, store.review_jobs)
    assert failed["id"] == job_id and failed["status"] == "failed" and failed["attempts"] == 3

    monkeypatch.setattr(
        worker.app_auth, "installation_client",
        lambda i: FakeListGH([_pull(number=1, head_sha="a" * 40)]),
    )
    # A restart inside the cooloff re-arms nothing, however many times it happens.
    assert worker.reconcile_all() == 0
    assert worker.reconcile_all() == 0
    (still_failed,) = _rows(url, store.review_jobs)
    assert still_failed["status"] == "failed" and still_failed["attempts"] == 3

    with create_engine(url).begin() as conn:
        conn.execute(
            store.review_jobs.update()
            .where(store.review_jobs.c.id == job_id)
            .values(
                finished_at=datetime.now(UTC)
                - timedelta(seconds=ingest.FAILED_REVIVE_COOLOFF_SECONDS + 60)
            )
        )

    assert worker.reconcile_all() == 1  # counted: a revive, not a fresh insert
    (revived,) = _rows(url, store.review_jobs)
    assert revived["id"] == job_id  # same row, in place
    assert revived["status"] == "pending" and revived["attempts"] == 0


def test_reconcile_logs_why_a_pr_was_skipped(tmp_path, monkeypatch, capsys):
    """_skip_reason's return value used to be computed and discarded at its
    only call site — an unreadable repo got a log line, but the spend gate
    itself (draft/fork) left no audit trail. The one thing worth being able
    to check after the fact is exactly why a given PR was not reviewed."""
    _installed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        worker.app_auth, "installation_client",
        lambda i: FakeListGH([_pull(number=9, draft=True)]),
    )
    assert worker.reconcile_installation(1) == 0
    err = capsys.readouterr().err
    assert "#9" in err and "draft" in err


def test_skip_reason_treats_missing_or_unset_draft_as_skip():
    """The docstring's whole UNSET rationale was previously unexercised: the
    old check (`getattr(p, "draft", False) is True`) let anything that
    wasn't the literal `True` — including UNSET and a genuinely missing
    field — fall through to "review it". Only an explicit draft=False
    should do that; True, UNSET, and missing must all skip, the same
    direction the fork check already treats its own UNSET/missing case."""
    from githubkit.utils import UNSET

    ready = SimpleNamespace(
        draft=False,
        head=SimpleNamespace(sha="a" * 40, repo=SimpleNamespace(id=42)),
        base=SimpleNamespace(repo=SimpleNamespace(id=42, full_name="o/r")),
    )
    assert worker._skip_reason(ready) is None

    unset = SimpleNamespace(
        draft=UNSET,
        head=ready.head,
        base=ready.base,
    )
    assert worker._skip_reason(unset) == "draft"

    missing = SimpleNamespace(head=ready.head, base=ready.base)  # no draft attribute at all
    assert worker._skip_reason(missing) == "draft"


def test_skip_reason_treats_missing_or_unset_repo_ids_as_fork():
    """Same UNSET rationale, the branch that was already correct — pinned
    with a real UNSET value and a genuinely missing attribute, not just the
    isinstance reasoning in the comment above it."""
    from githubkit.utils import UNSET

    unset_head_id = SimpleNamespace(
        draft=False,
        head=SimpleNamespace(sha="a" * 40, repo=SimpleNamespace(id=UNSET)),
        base=SimpleNamespace(repo=SimpleNamespace(id=42, full_name="o/r")),
    )
    assert worker._skip_reason(unset_head_id) == "fork"

    missing_head = SimpleNamespace(draft=False, head=SimpleNamespace(sha="a" * 40))
    assert worker._skip_reason(missing_head) == "fork"


def test_reconcile_skips_a_pr_whose_base_repo_id_disagrees_with_the_store(tmp_path, monkeypatch):
    """installation_repos' full_name can go stale: a repo can be deleted and
    its name picked up by an unrelated one. github_repo_id is the fact the
    store's tenancy actually keys on and the only one GitHub still
    guarantees, so a PR whose base repo id disagrees with it belongs to a
    different repo than the one this installation was granted, and must
    not be reconciled — or paid for — under this installation's identity.
    head_repo_id is set equal to base_repo_id here so this isn't just the
    fork check firing for the wrong reason."""
    _installed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        worker.app_auth, "installation_client",
        lambda i: FakeListGH([_pull(head_repo_id=999, base_repo_id=999)]),
    )
    assert worker.reconcile_installation(1) == 0
    assert ingest.claim() is None
