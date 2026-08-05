---
title: Post-read settlement may drop findings the frozen reader emits, never rewrite the score
status: proposed
date: 2026-08-04
---

## Context

ADR-0002 freezes `SYSTEM`, `SCHEMA`, `MODEL`, `EFFORT`, `MAX_TOKENS` and
`DIFF_BUDGET` — everything that goes *into* the reader. It says nothing
about what happens to the reader's *output* after the call returns, because
nothing did, until REVIEWING.md's "Log every finding, not only the ones
that taught something" section stated a resolution rule in prose: a claim
about an absence in the diff cannot be settled by re-reading the diff, but
it can be settled against data the model structurally never saw.

Two finding classes now act on that rule in code, both in `doug/settle.py`:
**missing-import** (PR #45, merged) drops a finding disproved by a runtime
import in the full file at head; **unmigrated-column/schema-dependency**
(PR #49) drops a finding disproved by the live database schema. Both were
100% wrong historically before settlement — `docs/findings-log.jsonl` has
missing-import at 1/1 disproved and the schema class at 5/5 disproved
across PRs 25, 30 (twice) and 48 (twice) — and neither touches
`risk_score`; only the rendered finding list changes.

Doug's own review of PR #49 named the gap directly, as a `beyond-ticket`
deviation: no recorded decision sanctions settling reader output at all.
That the deviation stream is itself unvalidated (2026-07-31 derangement
check, still failed) doesn't make the underlying observation wrong — a
grep already confirms it: no ADR mentions `settle.py`.

The gap is not cosmetic. Settlement can make a *true* finding disappear if
its own check is wrong, and this is not hypothetical: PR #49's fix commit
got its own Doug review and turned up two settlement bugs in the first
attempt — an incidental real column mention could settle an unrelated
whole-table claim, and a free-text classifier could route an unrelated
finding (a migration script's own concurrency bug) into the settlement
path entirely. Both were reproduced and fixed before merge, but nothing
short of an adversarial second read caught either; a happy-path unit test
would have shipped both. Freezing the model's *inputs* protects the
validity of one probe (ADR-0002's AUC numbers). Nothing was protecting
whether a user ever sees a finding the frozen model actually raised.

## Decision

Post-read settlement is an accepted mechanism, distinct from and
unconstrained by ADR-0002 — it never edits `SYSTEM`/`SCHEMA`/`MODEL`/input,
only filters what renders after the frozen call returns. A finding class
may be added to `doug/settle.py`'s settlement path only when all five hold:

1. **The claim is falsifiable against data the model structurally could
   not see** — the full file at head, the live schema, anything outside
   the diff it was given. Never against a re-read of the same diff; the
   check and the error would be the same observation.
2. **`risk_score` is never rewritten.** Settlement can remove or relabel a
   finding from the rendered list. It never touches the score the frozen
   instrument produced — that score is the validated artifact ADR-0002's
   evidence attaches to, not the finding text.
3. **A drop is never silent.** Every settlement path appends a weight-0
   `settled-<rule>` reason naming what was dropped and why, so a
   flagged-but-empty-of-findings verdict is never mysterious.
4. **Scope is an explicit slug allowlist, not a free-text heuristic.**
   PR #49's own history is the argument: a `"migrat" in desc and "missing"
   in desc`-style fallback was the exact bug, twice, and every real
   historical instance in `findings-log.jsonl` already carried a slug the
   allowlist could have named directly. A class's residual-real cases that
   must *not* settle (missing-import's `TYPE_CHECKING` imports; the
   schema class's whole-new-table claims) are documented in the same
   commit that adds the class.
5. **New settlement logic ships with reproduced-bug tests, not only
   happy-path ones.** A test that only proves the filter drops what it's
   supposed to drop does not prove it leaves everything else alone; both
   PR #49 bugs were unit-tested and green on the happy path.

## Rejected

**Treating this as already covered by ADR-0002.** It was assumed, not
decided, until a review pointed out the record didn't exist. Precedent
("we already did this once") is not a decision.

**One ADR per settlement class.** Fragmenting the same mechanism across a
new file for every finding class governs the wrong unit — the *mechanism*
is what needs rules, not each instance of it. A new class that satisfies
the five rules above needs a `settle.py` change, a `REVIEWING.md` entry,
and `findings-log.jsonl` evidence, not a new ADR. (Same shape as ADR-0011
governing "destructive migration cleanup" as a class, not one ADR per
migration.)

**Letting settlement rewrite `risk_score` or `band` when a class is
"obviously" disproved.** The frozen instrument's score is what the soak
and derangement checks are measuring the honesty of. Letting settlement
adjust it would let filtering accumulate as untracked score drift — the
exact quantity the rest of this project's measurement apparatus exists to
keep honest.

## Consequences

- A settlement PR now has a checklist to be reviewed against — Doug's own
  review, or a human's — instead of an ad hoc judgment call each time.
- `settled-<rule>` weight-0 reasons in a check run are now a documented,
  permanent part of the render contract, not `settle.py`'s private choice.
- Does not retroactively re-validate the two shipped classes' own
  correctness — it records that both satisfy the five rules by inspection,
  not that either is bug-free going forward.
- Opens settlement to future classes (e.g. the build-crew design's
  falsifier-derived kinds) without re-litigating whether output-side
  filtering is legitimate at all — only a new class's own falsifiability
  argument stays open per addition.
