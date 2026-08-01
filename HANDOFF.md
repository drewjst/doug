# HANDOFF — doug

State:    building — M0 CLOSED. M1 six-tenths done, ONE PR PER TASK
          (Andrew's call, 2026-08-01: more Doug verdicts + smaller diffs
          Doug can actually read whole). Merged to main: Tasks 1-2 (#18),
          3 (#19), 4 (#20), 5 (#23), 8 (#22, ADR-0010). Task 7a
          (reconcile functions) in flight as PR #24, fix round 2.

          SESSION HANDOVER 2026-08-01 13:22 PDT. Two sessions were live on
          this plan at once; Andrew's call was to stop the older one and
          leave a single owner. It had pushed 09fb2fe minutes earlier, so
          verify against origin rather than against memory. There is now
          exactly one owner — keep it that way, and check `ps` for a live
          session in .claude/worktrees before resuming here.
Next:     M1 Task 6 — the webhook rewrite (api/doug/api.py), carrying BOTH
          amendments in docs/superpowers/plans/2026-08-01-step-2-amendments.md
          (clock-start outcome_jobs rows; pull_request_review ingest; NO
          token mint). Then Task 7b (Step 4 only — wire reconcile_all into
          the lifespan Task 6 creates), Task 9 (retire the CI token path),
          Task 10 (deploy cutover). Task 7 Steps 1-3 already shipped, so
          only its Step 4 remains.
Blockers: none for code. Two things only Andrew can do:
          - subscribe the App to the "Pull request review" event before
            Task 6's third-party ingest receives anything (handler is
            inert but fully fixture-testable until then) — Task 10 checklist
          - rotate + delete api/.backtest-cache/llm-probe/api-key
            (confirmed never public; needs Anthropic console access)

Execution model (do not rediscover this):
- One PR per task. Doug reviews each (ADR-0008); read its findings, but
  VERIFY before fixing or dismissing — roughly half are disproved by files
  outside the diff. See docs/REVIEWING.md, which is the accumulated
  lessons from ~20 findings across two review layers.
- Per task: fresh implementer subagent from an extracted brief, then an
  INDEPENDENT reviewer, then a fix round, then a scoped re-review. Do not
  let the implementer grade its own work, and do not fix findings in the
  controller session.
- Extract a brief with sed from the plan; never make a subagent read all
  4591 lines. Task line ranges: T6 2638-3395, T7 3396-3716 (Step 4 starts
  at 3267 of that slice), T9 4025-4244, T10 4245-4591.

Standing rules this branch learned the hard way:
- A docstring asserting a durability/ordering/concurrency property must be
  TRUE. Eight separate findings here were comments promising guarantees
  the code did not make. If nothing enforces the claim, the comment is the
  bug.
- Plan INTENT governs over the plan's literal code sample. Several samples
  violated constraints the same plan states in prose. Fix it, and record
  the ruling in the PR body rather than applying it silently.
- A test that cannot fail when its named behavior regresses is an
  Important finding. Two shipped tests here were vacuous; both were caught
  by mutation, not by reading.

Key facts for the executor:
- App: dougs-review, App ID 4450932, installation 150424894 on drewjst
  (User, selected: doug only). Perms checks:write/contents:read/
  pull_requests:read/metadata:read; events: pull_request. Private key in
  Secret Manager doug-github-app-key (no IAM grant yet — deliberate, Task
  10 decides the dedicated-SA custody). Webhook secret doug-webhook-secret
  v2 (v1 has a trailing newline; prod pinned to :2, disable v1 at cutover).
  Webhook verified end-to-end in prod: ping + installation events 202 with
  valid signatures; deliveries currently verify-and-discard (api.py:331).
- Install visibility is "Only on this account" — flip to "Any account"
  before installing on lemahq/lema (Task 10 cutover).
- The plan was built by 3 drafting agents on locked interfaces, reviewed by
  2 adversarial verifiers (both verify by execution), 3 blockers + 5 majors
  fixed. Deepest invariants (do not "tidy" these away): enqueue REVIVES
  failed/superseded rows in place with a STABLE id; drain's seen-set bounds
  both retry burn and the force-push supersede/revive ping-pong; the
  no-ledger 503 is scoped to the three handled webhook events only.
- Derangement check (2026-07-31): BAR FAILED and the instrument is invalid
  for constraint-style records — validates nothing either way. Deviation
  findings stay UNBELIEVED; check-run copy must keep the "unvalidated"
  label. Positive-control experiment needed before further intent-stream
  investment. Full analysis: workspace/research/phase1-entry-preregistration.md
  (workspace/ is untracked — lives only on Andrew's machine).

Decisions this session (2026-08-01, PR #24 fix round 2 + Task 6 prep):
- The failed-revive cooloff is charged to the CALLER, not to the row.
  09fb2fe put FAILED_REVIVE_COOLOFF_SECONDS inside the shared _revive, so
  it also suppressed the two live paths — a PR reopened or force-pushed
  back to a failed head SHA within the hour would 202 and never be
  reviewed. enqueue now takes `trigger`, defaulting to live; only
  reconcile_installation's sweep passes "reconcile" — rejected: leaving the
  cooloff in _revive (trades a bounded spend leak for a silent review
  loss that lands on a person, not on a restart loop).
- store.find_verdict_by_identity must ALSO exclude tier='external', which
  the Task 6 amendment does not say. It keys on exactly the four App
  identity columns ordered id DESC, and it is worker.process_job's
  idempotency pre-read — so a human approval at head SHA X would satisfy
  Doug's own idempotency check at SHA X, Doug would never review that
  commit, and the check run would render a score=0.0 tier='external' row
  as Doug's verdict — rejected: guarding only the two helpers the
  amendment names.
- latest_reviews' external exclusion goes INSIDE the grouped max(id)
  subquery, not on the outer query: filtering outside makes a PR whose
  newest row is external vanish from /v1/queue instead of falling back to
  Doug's verdict — rejected: the one-line outer filter.
- External verdict rows get a dedicated writer beside store.save_review,
  not save_review itself (which hardcodes scored_at=now and takes a
  Verdict, while the amendment requires scored_at=review.submitted_at and
  no scoring code on the path) — rejected: threading a synthetic Verdict
  and a scored_at override through the scoring writer.
- find_review's immunity to external rows is INCIDENTAL, not designed (it
  matches pr_meta['head_sha'] as JSON and external rows write no pr_meta).
  The explicit exclusion goes in anyway.

Decisions this session (2026-08-01, M1 Tasks 1–2):
- outcome_jobs is a store.metadata table, NOT a migration (Global
  Constraint: new tables via create_all; migrations are for columns on
  existing prod tables) — rejected: ROADMAP's literal "migration 002"
  framing for the table.
- installation.created token mint SKIPPED: hash-only storage makes an
  install-time mint unrecoverable dead weight; M2's dispense endpoint
  mints and writes installations.token_hash (column landed in Task 2) —
  rejected: minting a token nobody can ever read back.
- verdicts.source widened to String(64) ('review:<login>' needs 46) —
  rejected: plan's String(20).
- Two plan-mandated defects fixed against the plan's literal code because
  the plan's own stated invariants condemned them: apply()'s version
  insert now swallows the duplicate-version race (docstring: "already
  done is satisfied, not failed"); drift test now pins BOTH directions
  (baseline + migrations == metadata) — rejected: shipping the plan's
  verbatim body over its intent.
- pull_request_review ingest design (for Task 6): tier='external',
  band cleared/flagged from review state, dedup on (inst, repo, pr,
  source, head_sha, scored_at); latest_reviews/find_review must exclude
  tier='external'. GitHub App needs the "Pull request review" event
  subscription — MANUAL step, Task 10 cutover checklist.

Decisions this session (2026-07-31/08-01, M0 pass):
- workflow-summary-test-fidelity: DROPPED, branch deleted (local + remote).
  Its only real content vs main was a test regex fix, already byte-identical
  on main; the branch's sole diff was a stale HANDOFF.md snapshot —
  rejected: merging it (nothing to merge).
- PR #15 was already merged upstream before this session acted on it
  (by a concurrent session); local main fast-forwarded, no rebase needed.
- Intent-stream posture (per-installation flag, default OFF for tenants,
  ON for dogfood, experimental label) needed no new decision — confirmed
  already written into design-lock.md:62 — rejected: re-deciding it.
- Key rotation at api/.backtest-cache/llm-probe/api-key deferred by Andrew
  this session (needs Anthropic console access) — rejected: deleting the
  file without rotating first (would just lose the credential, not retire
  it).

Prior decisions this session (2026-07-31/08-01, step-2 plan):
- Step-2 plan pushed straight to main (Andrew's instruction, sole session);
  execution returns to PRs. — rejected: PRing the plan doc (explicitly
  overridden by Andrew).
- ADR-0003 will be superseded by ADR-0010 (neutral check run) in the same
  commit as the check-run code; ADR-0007 and ADR-0008 get prose corrections
  only (their decisions stand, their surface references die with CI).
- Anthropic key rotation staged create-then-revoke-after-verify (Task 10)
  so the live reader never breaks between rotation and deploy.

Pointers:
- Plan: docs/superpowers/plans/2026-07-31-step-2-github-app-webhook-ingest.md
  (commits d51eec8..94f87e9+). Spec: docs/superpowers/specs/
  2026-07-30-github-app-tenancy-dashboard-design.md (lema mentions
  clarified 2026-08-01).
- Roadmap: docs/design/outcome-loop/ROADMAP.md — the tracking document,
  M0 through M6.
- Full session state: ../HANDOFF.md on Andrew's machine (project root,
  above this repo) is the richer, hook-maintained handoff.
- PR #16 merged (240caf5). Open PRs: #24 only.
- ROADMAP M1 checkboxes are STALE — Tasks 4, 5 and 8 are merged but still
  unticked. Fold the correction into a task PR rather than a lone commit.
- stash@{0} (queue-polish era): dashboard repoint + the lost step-1 plan
  file. Both obsolete (repoint shipped via deploy config; plan content
  landed in #14) — drop deliberately when convenient.
- Carried forward: reader-feedback items 3 & 4 (invariant-vs-mechanism;
  severity = impact × confidence) need a frozen v2 prompt + validation run —
  credits now exist, still unscheduled. lema#643 had FOUR reader findings
  (reader:brittle-test-assertion, low, unscored) — evidence the reader
  reads tests it is given.
