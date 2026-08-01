# Roadmap: The Outcome Loop

**The tracking document.** Check boxes in the PR that completes them; a milestone closes only
when its exit gate is verifiably true (the gate is the definition of done, not the checklist).
Sequencing logic: clear the decks → land the reviewed ingest plan once → make spend/auth safe →
build the loop → prove it on ourselves → let outsiders in → gated tracks. Nothing outside
M0 starts before M0 closes; M1–M3 are strictly ordered; M4/M5 overlap M3's calendar time
(the 14-day clock runs while we work); M6 tracks fire on triggers, not dates.

Effort marks are engineer-days of focused work, not calendar days.

---

## M0 — Clear the decks *(mostly decisions; ~1d)*

The items that make every later step messier the longer they wait.

- [x] **Merge PR #15** (`fix/reliability-review`) — lands the gated-traffic deploy + web timeout fix; merging FIRST avoids the known collision with step-2 Tasks 9/10 (else: deliberate rebase, its `/v1/review` idempotency work dies with the endpoint). Merged `0d95884`.
- [x] **Commit the design docs + landing-page section** as a PR (Doug reviews it, ADR-0008) — merged `240caf5` (#16).
- [x] **Decide** `workflow-summary-test-fidelity` branch: merge or drop (~49 test lines) — dropped; its only real content was already on main byte-identical, branch deleted.
- [ ] **Rotate + delete** the local key at `api/.backtest-cache/llm-probe/api-key` (long-standing) — confirmed NOT in the public repo (full-history pickaxe search across all branches, file never tracked, covered by `.gitignore`), so no public exposure; Andrew rotates the live prod key via gcloud on his own schedule, then deletes this local file.
- [x] **Confirm intent-stream posture** (design already assumes it): per-installation flag, default OFF for tenants, ON for dogfood, labeled experimental — confirmed in `design-lock.md:62`.
- [x] Fix stale `.env.example` (`MAGPIE_*` → current names) — trivial, stops onboarding confusion. Shipped in PR #16.

**Exit gate:** main contains #15 + the design docs; no undecided branches; no live credentials on disk.

---

## M1 — App ingest: execute step 2 with the amendments *(~10–14d)*

The reviewed 10-task TDD plan (`docs/superpowers/plans/2026-07-31-step-2-github-app-webhook-ingest.md`)
executed as written — amendments folded in at their task, never as a second pass.

- [x] Tasks 1–2: app_auth + migration runner — **with migration 002 in the same sitting**:
  `outcome_jobs` (+ UNIQUE(inst, repo, pr, merge_sha, window)), `verdicts.source` (String(64)) + `verdicts.prompt_hash`,
  `outcomes` identity columns (github_repo_id, installation_id, window_days, detail JSON),
  `installations.token_hash`. Note: the Task 6 `installation.created` token mint is superseded —
  hash-only storage makes an install-time mint unrecoverable; M2's dispense endpoint mints.
- [x] Task 3: durable job queue (`ingest.py`) — **plus** `reclaim_stalled()`, a lease-based sweep
  for claims stranded `'running'` by an instance that died mid-review. Without it such a row is
  never revivable, so `enqueue` collides forever and that SHA is silently never reviewed; adding
  `'running'` to the revivable set instead would buy a second paid read on a job still in flight.
- [ ] Task 4: check run (`check_run.py`) — deviations render `unvalidated` (ADR-0007 + the
  2026-07-31 derangement FAIL); fallback tier visible in the **title**, never a footnote
- [ ] Task 5: worker drain — **must call `reclaim_stalled()` once per pass before the first claim**
  (the other half of Task 3's fix; a young claim must be left strictly alone — that test is the
  anti-double-spend guarantee)
- [ ] Task 6: webhook dispatch — **plus** the clock-start branch (`closed && merged` → outcome_jobs,
  never through review-enqueue; closed-unmerged-writes-nothing test), **plus** `pull_request_review`
  ingest → third-party verdict rows (`source='review:<login>'`, no score, no model call). The
  `installation.created` token mint that used to sit here is superseded (see Tasks 1–2); note the
  GitHub App needs its "Pull request review" event subscription enabled at the Task 10 cutover.
- [ ] Task 7: reconcile-on-startup by head sha — **must call `reclaim_stalled()` before the enqueue
  sweep** (startup path only, never per-installation: the sweep is queue-wide, not per-tenant)
- [ ] Task 8: ADR-0010 (neutral check run) supersedes ADR-0003 in the same commit
- [ ] Tasks 9–10: delete CI token path, cutover deploy (rebase vs. merged #15 done deliberately)
- [ ] Research-corpus quarantine: sentinel installation UPDATE + `source='research'`

**Exit gate:** webhook-driven review live on `drewjst/doug` — deliveries 202 with dedup proven
(same delivery twice → one job), check run rendering, full suite green, CI token path gone.

---

## M2 — Safe to point at strangers *(~3–4d; blocks ANY outside install)*

- [ ] Spend caps wrapping **both** model calls — timeout, retry cap, per-installation monthly cap;
  the second intent read (`reader.py:372`) is currently uncapped and unmetered: close it
- [ ] `/v1/score/read`: authed or deleted
- [ ] Coverage integrity: paginate `list_files`, carry `changed_files` + `files_dropped`,
  `complete` ⇔ every changed file seen (a partial read can no longer render as a clean one)
- [ ] `fetch_pr` fetches review state (approvals no longer hardcoded 0 — live scorer matches the backtested one)
- [ ] ADR-0002 made real: cross-pin test (reader constants ≡ `llm_probe.py`), `prompt_hash` written per verdict
- [ ] Fork-PR + bot-author exclusion from deep reads
- [ ] Per-installation token dispense endpoint (GitHub-token-verified); scoped `/v1/queue` + receipt reads; cross-tenant read attempt → 404 (test pinned)

**Exit gate:** the attacker math closes — no unauthenticated paid endpoint, no uncapped spend
path, no cross-tenant read, no silent partial reads.

---

## M3 — The loop itself *(~5–7d build + 14d calendar)*

- [ ] `doug/adjudicate.py` as a pure function over (job rows, revert map) + fixtures that run
  `git_labels` cases through the **live** path (live label ≡ backtest label, pinned by test)
- [ ] Cloud Run Job (2Gi) + Cloud Scheduler; claim `due_at <= now()` FOR UPDATE SKIP LOCKED
- [ ] `base_ref` censoring: merge to non-default branch → `censored`, never `clean`
- [ ] Receipts: `GET /v1/prs/{n}/receipt` (verdict + threshold-at-scoring + findings + inputs-seen + adjudication block + hashes)
- [ ] Check-run footer: `adjudicated N · pending M · as of <date>` + `deep reads x/200`
- [ ] Public Doug-on-Doug scoreboard page (dogfood proof, no auth)
- [ ] **Pre-registration document published + hashed** (metrics, denominator, both windows,
  right-censoring, cadence) — hash lands in receipts; 60-day-backfill runbook written
  (must run before the first 14-day publication)

**Exit gate = Phase 0 dogfood gate:** drewjst/doug's own history backfilled and adjudicated with
**100% agreement vs. a manual `git log` audit** (any disagreement = detector bug = stop); one real
receipt correct end-to-end; scoreboard rendering live counts; then one full webhook-started
14-day cycle observed in prod.

---

## M4 — Onboarding + the kill-criterion interviews *(~3d + calendar; overlaps M3's clock)*

- [ ] 90-day replay productized: harvest/replay against an installation, `source='replay'`,
  structurally excluded from prospective counters; replay panel on the scoreboard, visually distinct
- [ ] Install/welcome: the dated IOU + merge-volume projection ("N≥30 lands ~<date>")
- [ ] **The 3 prospect interviews**, pitched off the live dogfood scoreboard + a replay of *their*
  public repo where possible — THESIS.md standing kill criterion: **2 of 3 "that's not right" halts productization**; outcome recorded either way

**Exit gate:** three interviews done, verdict written down.

---

## M5 — First design partners *(calendar-gated)*

- [ ] App visibility → "Any account"
- [ ] Onboard 2–3 design partners: $99/installation hand-invoiced, allowance rows, meter visible day 1
- [ ] 30 days of fill: prospective counters ticking on a real tenant, zero cross-tenant reads
- [ ] 60-day backfill run; **first pre-committed publication ships on its date, good or bad**

**Exit gate:** the first published number exists with N + CI + censoring rate, on schedule.

---

## M6 — Gated tracks *(each fires on its trigger — no dates)*

| Track | Trigger | Ref |
|---|---|---|
| MCP garden service (`doug.check`, AGENTS.md fragment export) | adjudicated rows ≥ min-n on ≥1 tenant | design-lock T4, addendum A3 |
| Tenant dashboard (WorkOS, tenancy steps 3–4) | >3 tenants or first tenant ask | tenancy spec |
| Evidence refinery (offline council) | enough adjudicated data to mine; becomes the panel-experiment harness | addendum A1 |
| Live specialist panel — **pre-registered experiment** | refinery harness ready; bar: beats single-read on flagged-band outcome capture | addendum G1 |
| Champion–challenger shadow models | model price/retirement event, or matured outcome set | addendum A2 |
| Staging GCP project | tenant #2 or first deploy-caused incident | design-lock open risk 1 |
| Derangement **positive control** (decision records) | pre-registered before any further intent investment; passes → deviations believed, fails → pull the stream | HANDOFF 2026-07-31 |
| Underwriter shadow probe (loss-ratio SQL) | ≥2 quarters adjudicated data on real tenants | IDEAS.md 2026-07-31 |
| Public cross-repo garden | its own design pass; never before the private garden earns "pattern" | design-lock non-goals |

---

## PC — Public-corpus track *(parallel; touches scripts and spend, never the product path)*

Leverages the built-and-tested backtest machinery (`harvest`/`git_labels`/`replay`) and the
existing 653-PR corpus. Inherits the standing discipline without exception: **bars pre-registered
before any spend, failures recorded, spent holdouts stay spent, frozen prompt only** (reads stay
comparable), permissive-license filter + citations for anything served onward, and public-corpus
rows live in their own store — quarantined from every tenant counter and every published tenant rate.

- [ ] **PC1 — Repo #3–#5 replication** (~$40 in batch reads; can start now): pre-register AUC
  bars, harvest three diverse permissively-licensed repos (incl. one non-Python, one monorepo),
  read newer slices, publish the replication table. Each survivor strengthens the core claim;
  a failure is recorded and caps the transfer story honestly.
- [ ] **PC2 — Garden evidence base** (~$250–500; **gated on PC1 showing transfer**): scale to
  10–20k read PRs across 10–15 permissive repos filtered to the observable entry domain (schema
  migrations first), then run the three pre-registered garden probes from IDEAS.md — survival-signal
  separation, variant separation, migration-episode reconstruction. Pass → the public garden tier
  has its evidence basis and the private garden's cold start gets a day-1 fallback; fail → the
  word "pattern" stays locked and we say so.
- [ ] **PC3 — Prospect replays** (≈ free; lands with M4): replay each interview prospect's public
  repo beforehand; walk in with their own 90 days adjudicated.
- [ ] **PC4 — Bureau seed** (research; no bar yet): harvest bot-authored PRs at scale across
  popular public repos; measure per-author-type revert rates with stratified base-rate controls;
  pre-register before any claim. Feeds the bureau → underwriter staircase (IDEAS.md 2026-07-31).

**Existing-asset conversions (no new spend):** the 653-corpus becomes the refinery dry-run fixture
set and the champion–challenger evaluation set; the hand-audit files seed receipt content; the
replication table is the honest sales one-pager.

---

*Sequencing rationale in one line: every milestone makes the next one's failure mode impossible —
#15 before ingest (collision), migration before columns (no second mechanism), caps before
strangers (spend), dogfood before partners (the gate), publication before scale (the promise) —
and the PC track runs beside it all, spending only against pre-registered bars.*
