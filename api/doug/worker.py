"""The job pipeline — one claimed job in, one check run out.

Everything the webhook must not do inline lives here: minting an
installation token, fetching the PR, scoring it, persisting it, posting
the check run. The handler's whole job is to make the work durable and
return 202; a delivery must never wait on a paid model read.

Failure policy differs from the CI-token review path's on purpose. There, a
down ledger must not fail somebody's CI, so save_review's exception becomes
a reason on the response and the review still "succeeds" (api.py's
/v1/review handler). Here the durable row IS the deliverable — a job
marked done having written nothing is a green checkmark over an empty
ledger — so save_review raising propagates, failing the job, and
ingest.fail decides whether to retry. save_deviations keeps the same
swallow api.py's handler uses for it: it is a genuinely separate write
(ADR-0007), and by the time it runs here the verdict it hangs off is
already durable, so a failure there must not cost the job that already
succeeded.
"""

import sys

from . import app_auth, check_run, ingest, reader, review, store
from .models import Band, Reason, Verdict


def process_job(job: dict) -> int | None:
    """Run one job. Returns the verdict id — the recorded one on a replay,
    a freshly scored one otherwise — or None when the job's head SHA no
    longer matches the PR's: that job lands 'superseded', not 'done', and
    the current head is enqueued in its place.
    """
    gh = app_auth.installation_client(job["installation_id"])
    owner, name = job["repo_full_name"].split("/", 1)

    # Idempotency read, before anything else — including the head-freshness
    # check below, not only the paid calls past it. reclaim_stalled() (or
    # ingest.fail(), if ingest.complete itself raises) can re-pend a
    # 'running' job whose save_review already landed but whose check-run
    # post or ingest.complete never ran: the worker died somewhere in
    # between. A naive retry would re-score from scratch — a second paid
    # score_one/read_intent, and a second verdicts row for the same commit,
    # since verdicts carries no unique constraint to stop it. Ordering this
    # ahead of the head-freshness check matters, not just for cost: a job
    # with an already-durable verdict must replay it regardless of whether
    # the PR has since moved on, or the head check below would supersede
    # this row — leaving a verdict in the ledger whose job never reached
    # 'done' and whose check run never posted, for a commit that really was
    # read.
    existing = store.find_verdict_by_identity(
        job["installation_id"], job["github_repo_id"], job["pr_number"], job["head_sha"]
    )
    if existing is not None:
        verdict = Verdict(
            score=existing["score"],
            band=Band(existing["band"]),
            threshold=existing["threshold"],
            reasons=[Reason(**r) for r in existing["reasons"]],
        )
        cov = reader.Coverage(**existing["coverage"]) if existing["coverage"] else None
        # Both reads truncate the same diff at the same DIFF_BUDGET (see
        # store.find_verdict_by_identity), so the risk read's stored
        # coverage is also what the intent read saw — reused rather than
        # invented. Without it there is nothing to hedge a replayed
        # deviation with, so the section is left out entirely rather than
        # rendered as if the read had been complete (ADR-0007's "a partial
        # read must never render as a whole one" cuts both ways here).
        intent_read = None
        if existing["intent_alignment"] is not None and cov is not None:
            intent_read = review.IntentRead(
                alignment=existing["intent_alignment"],
                refs=existing["intent_refs"],
                findings=[reader.DeviationFinding(**d) for d in existing["deviations"]],
                coverage=cov,
            )
        title, summary = check_run.render(existing["tier"], verdict, intent_read, cov)
        check_run.post(gh, owner, name, job["head_sha"], title, summary)
        ingest.complete(job["id"], existing["id"])
        return existing["id"]

    # Read the PR's current head before spending anything on it. A job can
    # sit in the queue behind a backlog, or be re-pended by a retry, long
    # enough for the branch to move — and fetch_pr would then read the NEW
    # diff while every identity column, the unique index and the check run
    # still said the old SHA. That mislabels a read rather than losing one,
    # which is worse: the verdict looks like evidence about a commit it
    # never saw. The SHA that overtook this one gets its own job.
    current = gh.rest.pulls.get(
        owner=owner, repo=name, pull_number=job["pr_number"]
    ).parsed_data.head.sha
    if current != job["head_sha"]:
        ingest.supersede(job["id"])
        ingest.enqueue(
            job["installation_id"],
            job["github_repo_id"],
            job["repo_full_name"],
            job["pr_number"],
            current,
        )
        return None

    meta, diff = review.fetch_pr(gh, owner, name, job["pr_number"])
    tier, verdict, rv, cov = review.score_one(meta, diff)
    intent_read = review.read_intent(gh, owner, name, meta, diff)

    verdict_id = store.save_review(
        job["repo_full_name"],
        job["pr_number"],
        tier,
        verdict,
        rv,
        model=reader.MODEL if tier == "reader" else None,
        pr_meta=meta.model_dump(mode="json"),
        coverage=cov,
        github_repo_id=job["github_repo_id"],
        installation_id=job["installation_id"],
        head_sha=job["head_sha"],
        source="app",
    )
    if intent_read is not None:
        try:
            store.save_deviations(
                verdict_id,
                intent_read.findings,
                intent_read.refs,
                intent_read.alignment,
            )
        except Exception as e:  # noqa: BLE001 — the verdict is already saved
            verdict.reasons.append(
                Reason(rule="deviations-unrecorded", label=str(e)[:200], weight=0.0)
            )

    title, summary = check_run.render(tier, verdict, intent_read, cov)
    # The job's head SHA, never meta's: by now pulls.get may already be
    # returning a newer commit, and that commit has its own job.
    check_run.post(gh, owner, name, job["head_sha"], title, summary)
    ingest.complete(job["id"], verdict_id)
    return verdict_id


def drain(max_jobs: int = 20) -> int:
    """Claim and run up to max_jobs. Returns how many were attempted.

    Bounded because this runs inside a request's background task: an
    unbounded drain on a busy morning would hold a Cloud Run instance for
    minutes past the response it belongs to. The next delivery kicks it
    again, and reconcile catches anything neither ever reaches.

    One job's failure must not strand the queue behind it — the whole
    queue is FIFO-ish and a poison job would otherwise block every PR
    opened after it.

    Calls ingest.reclaim_stalled() once, before the first claim — not per
    job. A worker that claims a job and then dies (a deploy, a scale-down,
    an OOM) leaves the row 'running' forever: REVIVABLE deliberately
    excludes that status, so no enqueue can ever bring it back on its own,
    and the SHA is silently never reviewed again. reclaim_stalled() is a
    no-op on a ledger-less deployment, exactly like claim(), so this needs
    no try/except around it either.

    The seen-set can end a pass early with pending work still queued, not
    only at max_jobs: a stale-head requeue enqueued mid-pass, or a revive,
    or a reclaim, can sort behind a job this pass already ran (claim()
    orders by enqueued_at), so the lap gets detected — and the pass stops —
    before every pending row has been looked at. That is fine: the row is
    exactly as durable as it was before drain ran, and the next delivery's
    kick reaches it.
    """
    reclaimed = ingest.reclaim_stalled()
    if reclaimed:
        print(f"doug: reclaimed {reclaimed} stalled job(s)", file=sys.stderr)

    attempted = 0
    seen: set[int] = set()
    while attempted < max_jobs:
        job = ingest.claim()
        if job is None:
            break
        if job["id"] in seen:
            # Lapped the queue: this exact job id already ran once this
            # pass. Three ways to land here, none of them implying a
            # failure: ingest.fail re-pends a job below the attempt cap, so
            # retrying it here would not be a retry — nothing has had time
            # to change — and would burn the whole attempt budget against
            # one transient fault in under a second; a force-push ping-pong
            # revives a superseded row in place; a reclaim re-pends a
            # stalled 'running' row in place. All three keep the row's
            # original id, so the seen-set catches every one of them with
            # no special case.
            ingest.release(job["id"])
            break
        seen.add(job["id"])
        attempted += 1
        try:
            process_job(job)
        except Exception as e:  # noqa: BLE001 — ingest.fail decides retry vs give up
            print(
                f"doug: job {job['id']} failed ({type(e).__name__}: {e})",
                file=sys.stderr,
            )
            ingest.fail(job["id"], str(e))
    return attempted


def _skip_reason(p) -> str | None:
    """Why this open PR must not be enqueued, or None.

    The same gate the pull_request webhook applies in api.py. Duplicated
    rather than shared on purpose: the two callers hold different objects —
    a githubkit model here, a parsed webhook payload there — and githubkit
    models an absent field as the UNSET sentinel, not None, so `if p.draft`
    is not the same test as `p.draft is True`. If the webhook's gate
    changes, this changes with it.
    """
    # Only an explicit draft=False proceeds. True, the UNSET sentinel, and a
    # genuinely missing field all fall through to "skip" — the same
    # safe-direction-is-skip choice the fork check below makes for its own
    # UNSET/missing case, applied here too rather than defaulting an unknown
    # draft state to "safe to review".
    if getattr(p, "draft", True) is not False:
        return "draft"
    head_id = getattr(getattr(getattr(p, "head", None), "repo", None), "id", None)
    base_id = getattr(getattr(getattr(p, "base", None), "repo", None), "id", None)
    # A fork's raw diff enters the prompt (_user_text, reader.py:179-187),
    # so an outside contributor must not be able to drive spend. UNSET or
    # missing ids are treated as a fork: the safe direction to be wrong in
    # is "skip".
    if not isinstance(head_id, int) or not isinstance(base_id, int):
        return "fork"
    return "fork" if head_id != base_id else None


# Hard ceiling on how many open PRs reconcile will look at per repo. No
# repo Doug expects to sit behind has anywhere near this many open at once;
# hitting it is itself a signal something is wrong (a runaway bot, a
# misconfigured install), logged rather than looping unboundedly or
# truncating without a trace. Bounds the per-repo cost so one install with
# a pathological number of open PRs cannot hang reconcile_all() for every
# other tenant behind it.
_MAX_OPEN_PRS_PER_REPO = 1000


def reconcile_installation(installation_id: int) -> int:
    """Enqueue every reviewable open PR this installation can see.

    The healing path for missed deliveries. GitHub retries a *failed*
    delivery, but a delivery this service 202s and then loses to a restart
    is never retried, and the redelivery window is not a guarantee — so
    recovery does not trust webhooks at all, it re-derives the world from
    the API and lets the queue's unique index throw away what it already
    has.

    Deliberately pulls.list rather than review.fetch_open_prs: that helper
    also fetches per-PR files to build PRMetadata, which is one extra
    request per open PR for data reconcile never reads. The worker fetches
    the diff when the job actually runs.

    Paginates rather than trusting a single page: GitHub caps one page at
    100, and "every open PR" (the promise above) would quietly become "the
    newest 100" on a busy repo otherwise, silently and permanently — the
    kind of gap between a docstring's claim and what the code does that
    this codebase's reviewers now check for. _MAX_OPEN_PRS_PER_REPO is the
    one place that promise still has an edge, and it's logged when hit.

    Does not call ingest.reclaim_stalled(): that sweep is installation-
    agnostic (whole queue, by lease age, not by tenant), so calling it here
    would reclaim other tenants' stranded rows as a side effect of
    reconciling one of them — Task 6 calls this function alone, from the
    installation.created handler, and that call must not have a blast
    radius past the one installation it names. reconcile_all is where the
    stalled-claim sweep belongs; see its docstring.
    """
    gh = app_auth.installation_client(installation_id)
    count = 0
    for repo_id, full_name in store.active_repos(installation_id):
        owner, _, name = full_name.partition("/")
        pulls: list = []
        page = 1
        try:
            while True:
                batch = gh.rest.pulls.list(
                    owner=owner, repo=name, state="open", per_page=100, page=page
                ).parsed_data
                pulls.extend(batch)
                if len(batch) < 100 or len(pulls) >= _MAX_OPEN_PRS_PER_REPO:
                    break
                page += 1
        except Exception as e:  # noqa: BLE001 — one unreadable repo is not fatal
            print(
                f"doug: reconcile skipped {full_name} ({type(e).__name__}: {e})",
                file=sys.stderr,
            )
            continue
        if len(pulls) >= _MAX_OPEN_PRS_PER_REPO:
            # A full page can overshoot the cap by up to per_page - 1 (the
            # break above only checks after a whole page lands), so trim
            # back to the cap exactly — the log line below must describe
            # what actually got reconciled, not what almost did.
            pulls = pulls[:_MAX_OPEN_PRS_PER_REPO]
            print(
                f"doug: reconcile capped at {_MAX_OPEN_PRS_PER_REPO} open PRs for "
                f"{full_name}; the rest were not reconciled this pass",
                file=sys.stderr,
            )
        for p in pulls:
            reason = _skip_reason(p)
            if reason is not None:
                print(
                    f"doug: reconcile skipped {full_name}#{p.number} ({reason})",
                    file=sys.stderr,
                )
                continue
            head_sha = getattr(getattr(p, "head", None), "sha", None)
            if not isinstance(head_sha, str):
                continue
            # installation_repos' full_name can go stale: a repo can be
            # deleted and its name picked up by an unrelated one. repo_id
            # (github_repo_id) is the fact the store's tenancy actually keys
            # on and the only one GitHub still guarantees, so a PR whose
            # base repo id disagrees with it belongs to a different repo
            # than the one this installation was granted — reviewing it
            # under this installation's identity would be wrong, not just
            # imprecise.
            base_id = getattr(getattr(getattr(p, "base", None), "repo", None), "id", None)
            if base_id != repo_id:
                print(
                    f"doug: reconcile skipped {full_name}#{p.number} "
                    f"(base repo id {base_id} != installation_repos' {repo_id})",
                    file=sys.stderr,
                )
                continue
            # enqueue has two outcomes on a collision here, not one. A row
            # already 'pending', 'running', or 'done' at this head SHA
            # collides and returns None — the ordinary dedupe reconcile
            # exists for, since the unique index carries no status column.
            # A row that is 'failed' or 'superseded' at this SHA is instead
            # REVIVED by ingest._revive: reset to status='pending',
            # attempts=0, and its (non-None) id comes back, so it counts
            # here too. That's deliberate — a PR that burned every attempt
            # before a restart is healed rather than staying dead forever —
            # but it isn't free: a revived job pays for up to max_attempts
            # model reads again. trigger='reconcile' is what bounds the
            # repetition: this sweep runs on every cold start, so a 'failed'
            # row it meets inside FAILED_REVIVE_COOLOFF_SECONDS is left
            # alone and the same broken PR costs one budget per cooloff
            # window rather than one per restart. This is the only caller
            # that passes it — a live event revives immediately, because
            # nobody reopens a PR every ninety seconds.
            if (
                ingest.enqueue(
                    installation_id,
                    repo_id,
                    full_name,
                    p.number,
                    head_sha,
                    trigger="reconcile",
                )
                is not None
            ):
                count += 1
    return count


def reconcile_all() -> int:
    """Reconcile every active installation. Returns total jobs enqueued.

    This is the startup reconcile path (Task 7's amendment): the one thing
    it does that reconcile_installation deliberately does not is call
    ingest.reclaim_stalled() once, up front, before the per-installation
    enqueue sweep below runs for anyone.

    A missed *delivery* is healed by re-deriving the world from the API and
    letting enqueue's unique index dedupe against it — that's
    reconcile_installation. A *crash-stranded claim* is a different failure:
    the row is still 'running', and REVIVABLE deliberately excludes that
    status (reviving it out from under a worker that might still be alive
    would pay for a second read of the same SHA), so enqueue collides with
    it and returns None — forever, on every subsequent startup — unless
    something first puts the row back to 'pending'. reclaim_stalled() is
    that something, and reconcile without it is structurally unable to fix
    the case it's named for: "a Cloud Run instance dying mid-review", which
    is the same failure class as "a deploy that wiped the App credentials"
    but the most likely instance of it, and startup is exactly the event
    that follows it.

    Reclaiming runs before the sweep below, not after and not
    per-installation, and the order is observable, not just a defensive
    nicety: enqueue's supersede-after-insert step (ingest.py) only retires
    rows that are already 'pending' at this (installation, repo, pr) with a
    different head_sha — it cannot touch a row that is still 'running'. So
    when a stranded claim's PR is force-pushed while the claim is stuck,
    reclaiming first is what lets the sweep's insert of the new head SHA
    supersede the stale one in the same pass; reclaiming after would leave
    both the stale SHA and the new one 'pending' — the stale one live work
    a worker will claim and then have to supersede itself, instead of the
    sweep having already retired it.
    test_reconcile_all_supersedes_a_stranded_claim_whose_pr_moved_on pins
    that case behaviorally. test_reconcile_all_calls_reclaim_stalled_before_the_enqueue_sweep
    pins the call order directly on top of it, for the (also real, just not
    behaviorally distinguishable on its own) case where the PR's head SHA
    never changed and the two orderings converge to the same final row
    state either way.

    reclaim_stalled() sweeps by lease age across the whole queue, not by
    tenant, which is exactly why it belongs here and not inside
    reconcile_installation: called there, it would reclaim every other
    tenant's stranded rows as a side effect of reconciling just one
    installation.
    """
    reclaimed = ingest.reclaim_stalled()
    if reclaimed:
        print(f"doug: reconcile reclaimed {reclaimed} stalled job(s)", file=sys.stderr)

    total = 0
    for installation_id in store.active_installations():
        try:
            total += reconcile_installation(installation_id)
        except Exception as e:  # noqa: BLE001 — one bad tenant must not stop the rest
            print(
                f"doug: reconcile failed for installation {installation_id} "
                f"({type(e).__name__}: {e})",
                file=sys.stderr,
            )
    return total
