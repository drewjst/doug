# Reviewing changes to Doug

Doug reviews every PR here (ADR-0008), and agent reviewers review the work before it
becomes a PR. This file records what those two layers keep getting wrong, so the next
reviewer starts where the last one finished. Add to it when a review misses something —
a lesson that stays in one session's context is not a lesson.

## Verdicts on a fix must judge the replacement, not the removal

"ADDRESSED" means the defect no longer exists. It does not mean the line changed.

The case that produced this rule: a review found `ingest.claim()`'s docstring wrong about
why sqlite cannot double-claim a row. The fix round rewrote the sentence, the scoped
re-review verdicted it ADDRESSED — and the replacement was *also* wrong, in a subtler way
(it claimed a racing writer cannot read the row; sqlite's deferred transactions mean it
can, and the loser fails on its UPDATE with SQLITE_BUSY instead). Doug caught it on the PR,
one layer past both agent reviews.

When the fix is an explanation rather than a behavior change, the re-reviewer has to
evaluate the new explanation on its merits, against the mechanism it describes. A diff that
replaces a wrong claim with a different wrong claim reads exactly like a successful fix.

## A finding that depends on code outside the diff must say so

Doug reviews a diff, not a repository, and it reports two kinds of finding without
distinguishing them: things the diff proves, and things the diff merely permits. Two of its
five findings on PR #19 were the second kind — a datetime aware/naive concern that
`store.py` disproves (every column is `DateTime(timezone=True)`), and a duplicate-read
concern that `worker.py`'s head re-check answers.

Neither was a bad finding; both were unresolvable from what it was shown. Check the
surrounding code before fixing or dismissing, and record which one it was. The same rule
binds agent reviewers, who should mark these ⚠️ rather than assert them.

## Settle a resolution finding with the check that already ran

PR #28's `reader:missing-import` said `threading.Thread` was newly used with no
`import threading` in the diff. The import was already at `api/doug/api.py:7`, three tests
spawn that thread, and `ruff check` — which runs on every PR under
`select = ["E", "F", "I", "UP", "B"]` — was green before Doug emitted the finding. F821 is
undefined-name. **The falsifier had already run.**

Before disposing a finding about a name, an import, or a symbol, check whether CI already
answered it. Ruff's boundary, measured 2026-08-02 against a probe file with that exact
select list:

| ruff catches | ruff misses |
|---|---|
| `F821` undefined name, intra-file — including a function-scoped import referenced from another function | a `TYPE_CHECKING`-only import dereferenced at runtime (a live `NameError`) |
| `F403` / `F405` star imports | `from x import Y` where `Y` is absent from the target module |
| | an import of a module that does not exist at all |
| | `getattr(obj, "made_up_attribute", "")` |

Those four are the only places a resolution finding can still be real. Everything else in
the class is disproved by a command that ran green before the review started.

The general form is worth more than the table. **A claim about an absence cannot be settled
by looking at the same place the claim came from.** "No `import threading` was added" is a
fact about the diff; whether the import exists is a fact about the repo. Re-reading the diff
confirms the finding every time and proves nothing — the check and the error are the same
observation. Go to the file.

## Verify platform semantics before fixing a platform finding

PR #22 produced a plausible warning that a `neutral` check run might not satisfy a
required check in some GitHub configurations. That would undermine ADR-0010, but GitHub's
current documentation says the opposite: `success`, `skipped`, and `neutral` are
successful required-check states.

Claims about GitHub behavior need current primary-source evidence before they change code
or a decision record. If the documentation is clear, cite it in the disposition. If it is
not, reproduce the exact branch-protection or ruleset configuration. Do not turn “the
check is missing” or “the check came from the wrong expected App” into “`neutral`
blocks” — those are different failure modes. A required check can wait forever when no
result is posted; a posted `neutral` result satisfies the required status check.

Source checked 2026-08-01: [Troubleshooting required status
checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks#required-check-needs-to-succeed-against-the-latest-commit-sha).

## Read Doug's coverage line before trusting its verdict

Every verdict carries what was actually read: `Partial read: 83% of the diff (30,000 of
35,956 chars). Cut inside api/tests/test_ingest.py. Never sent: ROADMAP.md.` A clear on a
partial read is not evidence about the unread part, and Doug says so itself.

This is also a PR-size signal pointing the same way as one-PR-per-task: a diff small enough
to be read whole produces a verdict worth something. A 36k-char diff does not.

## The recurring defect class here is a comment that outlives its truth

Across these reviews the same shape keeps recurring: a docstring asserting a durability,
ordering, or concurrency property the code does not have. `ingest.py` claimed a dying
instance "loses a claim, not a review" while stranding it forever; `apply()` promised
"already done is satisfied, not failed" and then raised on a duplicate version row;
`claim()` explained a sqlite guarantee twice, wrongly both times.

These are not cosmetic. This product sells calibrated claims, and a comment is a claim with
no test behind it. When a docstring states a property, find the code that enforces it —
and if nothing does, the docstring is the bug.

## Mutation testing cannot see a property whose tests share an assumption with the code

A mutation battery answers "does any test notice this edit?" It cannot answer "does any test
supply an input that would make this edit matter?" So a property whose every test input is
drawn from the same assumption the code makes survives every mutant — and the survival reads
as coverage.

The case that produced this rule: `ingest._revive` picks the reconcile sweep's cooloff terms
with `trigger == "reconcile"`, and the comment above it states the fail-open direction as a
safety property — anything unrecognized takes the live branch, so a mistake costs spend rather
than a PR that 202s and is never reviewed. Rewritten as `trigger != "live"` it is identical
over the two values `Trigger` allows and inverted over every other one, which is the natural
edit the moment somebody adds a third trigger. The whole suite passed against that mutant,
through the implementer's battery, the controller's re-run of it, and every mutation either
ran, because all of them used valid triggers. `api.py`'s `REVIEW_BANDS` is the same shape with
the assumption in the fixtures: lowercase keys, lowercase states in every test payload, and a
GitHub REST API that spells those states uppercase.

The tell is a property that is load-bearing, stated in prose, and exercised only by inputs
drawn from the set the code already assumes — two enum values, one letter case, one SQL
dialect. The remedy is one input from outside that set, and the test is usually three lines,
which is why it is worth writing rather than arguing about.

Sweeping the rest of `ingest.py` for the shape found three more, left unfixed and recorded
here instead: `REVIVABLE` can be emptied to `()` with the suite green (nine comments across
four files cite it as what excludes `'running'` from revival; no code reads it — `_revive`
spells the statuses literally), `claim()`'s Postgres `SKIP LOCKED` branch can be disabled
outright because every test runs sqlite, which its own docstring says takes a different path,
and `reclaim_stalled`'s `max(0, rowcount)` clamp can be dropped. A green mutation run over
those is not evidence about them.

## Where the plan's own text is the defect

The step-2 plan carries literal code. Several times its sample violated a constraint the
same plan states in prose. The standing ruling is that plan **intent** governs over plan
**sample**, the fix goes in, and the ruling gets recorded in the PR body rather than
applied silently — the plan was reviewed by people who deserve to see where it was wrong.

## Doug's own findings: expect roughly half to be disproved by code it wasn't shown

Two rounds of Doug reviewing this branch produced nine findings. Four were real, two were
disproved by files outside the diff (`Coverage` declaring the field it was said to reject;
`save_deviations` always writing a row, so "no rows" means no read rather than a dropped
one), and three were "wrong as stated, right about something adjacent."

That last category is the valuable one and the easiest to throw away. Doug claimed the
replay path could reuse the wrong coverage for the intent read; it cannot, because
`coverage()` is a pure function of the diff. But the property was asserted in two docstrings
and enforced by nothing, and the replay path had just started depending on it — so the
finding was right that something was missing, and wrong about what. It bought a test.

The rule: before dismissing a finding, find the code that disproves it and say which file
that was. Before accepting one, check whether the fix it suggests is the fix the codebase
actually needs — Doug flagged the idempotency pre-read as advisory, and the useful response
was not to add a lock but to upgrade an already-planned index to a unique one.

## Log every finding, not only the ones that taught something

"Roughly half" above is an impression, not a measurement, and this file cannot make it one:
a finding that produced no lesson never got written down, so there is no denominator here
and never will be.

`docs/findings-log.jsonl` is the denominator. One line per finding, appended at disposition
time — when you already hold the answer, because the rule above already makes you name the
file that settled it before dismissing anything. It is transcription, not new work.

```json
{"date":"2026-08-02","pr":28,"layer":"doug","rule":"reader:missing-import",
 "verdict":"disproved","changed":false,
 "settled_by":"api/doug/api.py:7 — already imported; ruff F821 green before the finding",
 "source":"prospective","note":"optional, one line"}
```

(Shown wrapped; it is one line in the file. `jq -e . docs/findings-log.jsonl` is the check.)

The schema is also enforced in code: `uv run python -m doug.findings_log check`
(and the pin in `api/tests/test_findings_log.py`). Append at disposition time with
`uv run python -m doug.findings_log append --pr N --layer doug --rule … --verdict
disproved|real|adjacent --changed|--no-changed --settled-by "…"`. Rates are
prospective-only (`… rate`); backfill never enters the denominator.

The product path also applies the resolution rule without editing the frozen
prompt (ADR-0002): after a reader verdict, **missing-import** findings are
settled against runtime imports in the full file at the reviewed head
(`doug/settle.py`). `if TYPE_CHECKING:` imports do not settle (residual-real
per the table above). Dropped findings leave `risk_score` alone and add a
weight-0 `settled-missing-import` reason so a flagged empty-finding check run
is not silent.

`layer` is `doug` or `agent-reviewer` — the two layers this file exists to track, kept
separable so one never speaks for the other. `verdict` is `real | disproved | adjacent`.

Three rules, each of which someone will otherwise get wrong:

- **`adjacent` is not a soft `disproved`.** It is the third category above — wrong as
  stated, right about something nearby — and it is *the valuable one and the easiest to
  throw away*. Two of the entries seeded into the log bought a test each while being false.
- **`changed` is a separate axis from `verdict`, and you need both.** A true finding that
  changed nothing is a re-report of something the code already documents; a false finding
  that changed something found a real gap by the wrong route. Collapsing them into one
  column loses exactly the distinction that makes the log worth keeping, and it would score
  Doug's best mode — *"this code does not justify itself"* — as failure.
- **Backfilled rows carry `"source":"backfill"` and are excluded from every rate.** The
  seeded rows were reconstructed from this file's prose and from `IDEAS.md`, not recorded at
  disposition. They demonstrate the schema; they are not evidence. The denominator starts
  with the first `prospective` row. Same discipline as `verdicts.source` quarantining
  `replay` and `research` from published numbers.

**This is not precision.** ADR-0005 reserves that word for defect prediction and mandates
two tables for it. Whether a finding is *true* is a different quantity from whether it
*predicted a defect* — a finding can be true and worthless, or false and load-bearing. Never
report a rate from this log as precision, and never put the two in the same table.

## A shared commit SHA does not make App and CI the same idempotency domain

PR #38's medium finding said making `find_review` require NULL App ids intentionally
doubled spend and could break an App webhook redelivery routed through `/v1/review`.
The extra verdict was real; the claimed dedupe regression was not. The two verdicts are
the two independent soak instruments that `/compare` exists to measure. `api.py` is the
only production caller of `find_review`, for the CI-only `/v1/review` route. App webhook
deliveries enqueue a job, and `worker.py` deduplicates those with
`find_verdict_by_identity(installation_id, github_repo_id, pr_number, head_sha)` instead.
The same-path CI replay still pays once, while an App result cannot erase the independent
CI observation. `test_review_repeat_for_same_commit_replays_without_a_second_row` and
`test_review_after_app_for_same_commit_scores_a_distinct_ci_verdict` pin both directions.

Before treating a dedupe helper as global, enumerate its production callers and identify
the event identity each caller owns. A hypothetical route from one delivery mechanism
through another route is not a current regression. If the product explicitly runs two
instruments on one commit, cross-instrument dedupe destroys the evidence rather than
saving a duplicate read.

The same review said `_comparison_run` could raise when a legacy `reads` row omitted one
of its projected coverage keys. That legacy shape has never existed: merged commit
`3983030` introduced `reads` with `diff_chars`, `sent_chars`, `files_sent`, `files_unseen`,
and `file_cut` together, and none were added later. The reviewed implementation obtained
coverage from `select(reads)`, whose mapping contained every declared column even when
nullable `file_cut` was NULL. The fixed implementation explicitly projects those same
columns from a `latest_read` alias. A database physically missing a declared column fails
the SQL SELECT before response projection; changing `coverage[key]` to
`coverage.get(key)` would not degrade that schema mismatch. Check table history and the
producer contract before adding fallbacks for a row shape only a partial diff makes
plausible.

Two adjacent findings were valid. The per-verdict coverage SELECT was an N+1 and became a
single outer join. The PR-group limit also did not bound duplicate rows; silently slicing
them would corrupt pairing and missing-path metrics, so successful comparison responses
remain lossless while results above the run ceiling fail with an explicit 413.

The replacement review called that 413 a hard failure with no graceful degradation, but
it did not read the web client. `web/lib/api.ts` converts every non-OK comparison response
to the explicit `unavailable` source, and `/compare` renders that state without summary
zeroes or partial runs. Pagination could improve availability later; returning a bounded
but incomplete group now would be a correctness regression. Trace an error through its
consumer before claiming the user sees a crash or fabricated state.

It also called a both-App-ids-NULL row with no head SHA a classification heuristic. That
is the accepted legacy CI identity, not a guess introduced by `_comparison_path`; rows
with one App id are excluded in SQL. The web model assigns a null-head run its own
`head-unknown` group, counts no missing path, and computes no delta. Check the producer-era
contract and downstream neutral state before relabeling an intentionally unpairable row as
malformed.

The replacement pass's remaining query-cost warning is plausible but unproven: one
set-based query can still become slow as the ledger grows. Require a production-scale
execution plan or measured latency before replacing it again; SQL shape alone does not
establish a regression, and speculative query rewrites can reintroduce the N+1 or cut
duplicate evidence.

## New tables never need a migration — only new columns on an existing one do

PR #25 got a medium finding: `deep_read_counters` and `verdicts.prompt_hash` looked
unmigrated, so a deployed database would raise on first use. `store.py`'s own module
docstring disproves half of it (`prompt_hash` already has both a `Table` column and a
migration 001 entry, landed in #18, outside this diff) and the migration mechanism itself
disproves the other half: `migrations.py`'s `MIGRATIONS` list has never once contained a
`CREATE TABLE` — `review_jobs`, `installations`, `installation_repos`, and `outcome_jobs`
all shipped without one, because `create_all()` (called on every `_get_engine()`) adds any
table missing from the target database and only ever fails to add a *column* to a table
that already exists. A migration is for the second case, never the first.

Not a bad finding — a fresh reader has no way to know that convention from the diff alone,
and it bought a real regression test (`test_deep_read_counters_needs_no_migration_on_a_database_that_predates_it`)
that builds a database with every table except the new one and proves `create_all()` still
adds it. Same rule as PR #19's datetime finding: check the surrounding code, then say which
file settled it, in the disposition — here, `store.py`'s docstring plus the absence of any
`CREATE TABLE` anywhere in `migrations.py`.

## "No migration for this column" often means "not in the diff's new versions"

PR #30's high finding: `find_review` / `save_review` now use `verdicts.head_sha`, but "the
visible migration list only adds `prompt_hash` (v2) and indexes (v3)." Disposition: **false**.
Migration **001** has added `head_sha` since #18 (`6a1a213`); this PR only started writing
and querying it. Doug was looking at the *diff's* `MIGRATIONS` delta (new or touched
versions), not the full list, and said so in the coverage line — `Partial read: 50% … Never
sent: … test_migrations.py`.

Same shape as #25's `prompt_hash` half: a column that already has an older migration looks
unmigrated when the reader only sees the versions this PR introduced. Before treating a
missing-migration finding as Critical/High:

1. Read Doug's coverage line (unread `migrations.py` / tests are a stop sign).
2. Open the full `MIGRATIONS` list and search for `ADD COLUMN <name>` in *every* version,
   not only the ones in the diff hunk.
3. Confirm the `Table()` definition and the migration agree — but know how far that guard
   reaches. `test_no_migrated_table_has_a_column_unaccounted_for_by_baseline_or_migration`
   (`api/tests/test_migrations.py:163`) loops `for table in _BASELINE_DDL`, and
   `_BASELINE_DDL` holds **only `verdicts` and `outcomes`**. A new column on `findings`,
   `reads`, or `deviations` is unguarded in both directions: the suite stays green while
   production lacks the column. Add the table to `_BASELINE_DDL` in the same PR that adds
   the column, or the drift test everyone will cite is not watching.

If the column is already migrated outside the diff, say so in the disposition and name the
version + landing PR. Do not add a duplicate `ALTER` "to be safe" — that is noise, and on
sqlite without `IF NOT EXISTS` it depends entirely on `_SATISFIED` text matching.

PR #30's second Doug pass repeated this same high finding after `claim_generation`
(migration 004) was added — still false for the same reason. The coverage line again
cut before the older migration versions. A finding that returns after a disposition
that named the landing migration is not new evidence; re-check the full `MIGRATIONS`
list before reopening it.

## "Post failure loses the check run" must distinguish raise from swallow, and retry from tradeoff

PR #30 ordered `ingest.complete` before `check_run.post` so a lost claim cannot emit a
second check run on the identity-replay path. Doug flagged that as "if the GitHub post
raises or the process dies, the job is already done and never retried — the silent
never-reviewed failure." Disposition: **half-true**.

`check_run.post` **never raises** (ADR-0010: failure is swallowed and logged). The
GitHub-outage path was already "reviewed with no check run" under the old order. What
changed is only the process-death window *between* complete and post: that job will not
retry, while death *before* complete still recovers via reclaim + identity-replay.

That is the intentional tradeoff against duplicate check runs after reclaim. When
disposing an ordering finding against this path:

1. Read `check_run.post` — does it raise, or swallow?
2. Ask which failure the queue can still retry (status still `running`) versus which it
   cannot (already `done`).
3. Name the competing defect the new order closes (here: double post on lost claim).
   Do not treat "ADR-0010 says swallow post failure" as "posting must be ungated by
   queue state" — skipping a post when the claim is lost is not the same as swallowing a
   GitHub error after a held claim.

## A completeness check must be about content that could have been reviewed, not about hitting an API's raw file list

PR #25 also introduced `Coverage.complete` requiring `files_sent == changed_files`, and got
a real medium finding: a PR touching one binary file (a screenshot, a lockfile checksum)
would be marked incomplete forever, because a file with no patch never produces a diff
header and `files_sent` can never count it. The naive fix — drop the check — would have
reopened the exact bug it exists to catch (a 250-file PR silently rendering as fully read).

The right fix distinguishes what GitHub's `DiffEntry` actually tells you: a genuine binary
comes back with `additions == deletions == 0` alongside `patch=None`, because git cannot
count lines in it. A large text file GitHub declines to inline for size still carries the
real line counts it computed. Only the second case is content that should have been
reviewable and was not — `files_dropped` now excludes the first, and `complete` compares
against `files_dropped` rather than the raw `changed_files` count, which was always going to
disagree with `files_sent` on any PR touching a non-text file. `changed_files` stays as a
display fact for the receipt ("N of M"), decoupled from the boolean.

Same shape as the idempotency-pre-read case above: the finding named a real gap and
suggested the wrong repair. The useful move was asking what GitHub's own data can actually
distinguish before picking which files count as "dropped."

## Intentional uniqueness is not a behavior-change defect

PR #43's migration 005 made App-path `(installation, repo, pr, head_sha)` unique so the
published denominator cannot double-count. Doug flagged `reader:behavior-change`: same-SHA
re-scores no longer insert a new verdict row. That is the decision, not a regression — ADR-0011
records it. A finding that restates a locked uniqueness contract as an accidental change
should be dismissed, not "fixed" by re-opening duplicate ledger rows.

## Do not invent schema dependents the unread files would disprove

Same PR, `reader:unsafe-migration` (high): migration 005 deletes duplicate verdicts after
clearing findings/reads/deviations, and Doug warned that "e.g. outcomes" would dangle or
violate an FK. outcomes has never carried `verdict_id`; it joins by identity columns. The
tables that *do* FK to `verdicts.id` are declared in `store.py` — which the coverage line
said was never sent (`Partial read … Never sent: api/tests/test_store.py`). The finding
treated a guessed dependent as fact.

Disposition when this shape appears: read the coverage line first (see above), then check
`store.metadata` foreign keys before expanding a destructive migration. The repair on #43
was a pin of the real closed FK set plus a migration comment, not deleting from tables that
do not reference the row.

## A beyond-ticket finding about a missing decision wants an ADR, not a revert

PR #43 also got unvalidated `beyond-ticket` notes: ADR-0001 had rejected a migration
framework until data-in-flight needed preserving, and the index-not-on-Table convention was
undocumented. Both were decision-record gaps. The right move was ADR-0011 (sanction
destructive constraint prep; name the create_all divergence), not ripping out migration 005
or declaring the unique index on the SQLAlchemy `Table` (which would reintroduce the
divergence migration 003 already refused).

When Doug says the code went past a recorded rejection, either (a) the rejection still
binds and the code is wrong, or (b) the situation the ADR said to revisit has arrived and
the record needs updating. Pick deliberately; do not "fix" (b) by reverting the work that
forced the revisit.

## Two token classes

`DOUG_API_TOKEN` is the **operator** credential: unscoped, reaches every
endpoint, and is what `doug-web` sends server-side (`web/lib/api.ts`). Reviews
that assume "the token" is tenant-scoped are reading the wrong class.

A **tenant** token is dispensed by `POST /v1/installations/token`, stored only
as `sha256` in `installations.token_hash`, and resolves to exactly one
`installation_id`. It reaches `/v1/queue` and nothing else.

Three things a reviewer should check, because each has a failure that looks
fine in passing tests:

1. **Any new filter on `latest_reviews` goes inside the grouped subquery.**
   Outside, an excluded row can still win `max(id)` for its PR and then be
   dropped — the PR vanishes rather than falling back. Pinned by
   `test_scoped_queue_falls_back_to_the_app_row_under_a_newer_ci_row`.
2. **Cross-tenant is 404, never an empty list.** An empty list reads as "no
   reviews yet" and confirms the caller's guess might be real.
3. **New GitHub calls on public endpoints check the caller's credential
   first.** The shared 5,000/hr REST quota was exhausted twice on 2026-08-02;
   a public endpoint that spends Doug's quota before the caller's is a drain
   loop. Pinned by `test_non_admin_pat_never_spends_dougs_github_quota`.

## A table only a webhook populates can be empty in production

Found 2026-08-04, by inspecting the production ledger while chasing an
unrelated finding on PR #48.

`installations` has **one writer** — `api.py:730`, inside the `installation`
webhook handler — and it is read by `worker.reconcile_all` (via
`store.active_installations`) and by `tenancy.mint`/`store.active_repos`.
In production that table held **zero rows**, while `verdicts` held 33 rows
carrying `installation_id = 150424894`. The App path was demonstrably working;
the table describing the installation had simply never been written, because
Doug was installed before that handler existed and no `installation` delivery
was ever replayed.

Every test seeds the row first — `upsert_installation(...)` is the opening line
of the fixtures — so the whole suite passes against a state production is not
in. The green suite is evidence about the code, not about the ledger.

When reviewing anything that reads a table:

1. **Ask who writes it, and whether that writer has definitely run in
   production.** A webhook handler shipped after the event it handles will
   never have fired for installations that predate it. Redelivery is a manual
   act nobody performs by default.
2. **Distrust a passing test whose fixture creates the row under review.** It
   proves the read works given the row; it says nothing about whether the row
   exists. This is the same class as ADR-0002's self-referential test — a check
   that cannot fail in the direction that matters.
3. **Prefer one query against the real ledger to any amount of reasoning about
   it.** The reasoning here — "the table is populated by the webhook, the
   webhook works, reviews are happening" — was individually true at every step
   and wrong at the end.

The symptom this hid: `reconcile_all` loops over `active_installations()`, so
with an empty table the startup sweep enqueues nothing *by construction*. That
had been recorded in HANDOFF as "the webhook path drains jobs promptly, so at
any boot there is nothing pending for the sweep to find" — a plausible
explanation for the right observation and the wrong reason.

## Your disposition is invisible to the next review pass — and writing it down makes it more so

Two Doug passes on PR #48, 2026-08-04. The migration finding
(`missing-migration-dependency`, then `schema-dependency`) came back on the second
pass, after the first had been disproved *against production* and the disproof
written into this PR's own design doc. Doug was arguing with an answer it had
never been shown: the design doc was in its **Never sent** list both times.

That makes the unmigrated-column rule **5/5 disproved** across PRs 25, 30 (twice)
and 48 (twice) — see "No migration for this column…" above, which already
documented the reasoning and already warned against adding a defensive `ALTER`.
The new part is the mechanism, and it is slightly perverse:

**The read budget is fixed (30,000 chars) and the diff is not.** Across one
session of answering findings in-repo, this PR went 105k → 122k chars and Doug's
coverage went **29% → 24%**. Every disposition, limit and roadmap item added to
the branch pushed the percentage down. Documenting the answer is what put the
answer out of reach.

Consequences for how to work:

1. **A repeated finding is not persistence, and not new evidence.** Check the
   coverage line for whether the file carrying your disposition was sent at all
   before you treat a second flag as a signal worth re-litigating.
2. **Put the disposition where the reader will see it** — a PR comment or a
   code comment near the flagged line — not only in a doc the budget will cut.
   The findings log is the durable record; the code is the reachable one.
3. **Expect coverage to fall as you respond to a review.** If a finding matters,
   settle it in code or in a comment beside the code, because prose added
   elsewhere in the branch measurably reduces what the next pass reads.

The standing advice not to add a speculative `ALTER … ADD COLUMN` now has a
concrete cost attached. One was written for `installations.token_hash` on this
PR and reverted: `ALTER TABLE` on a table that does not exist raises
`no such table`, which `_SATISFIED` does not match, so `apply()` would have
propagated it and **crash-looped the revision on cold start**. Three existing
tests caught it immediately. Production already had the column — verified by
querying `information_schema`, which is the check that should have come first.
