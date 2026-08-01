"""LLM diff-reader — the tier the Phase-1 probes validated.

Prompt, schema, and read parameters are byte-identical to
scripts/llm_probe.py as of commit 0064e6b, where they were validated
pre-registered on two repos (AUC 0.687 sentry / 0.668 grafana against best
deterministic baselines of 0.591 / 0.518, ReDef polarity counterfactual
passed on both). They are load-bearing evidence — a change here is a new
experiment, not a tweak.

Opt-in twice over: DOUG_READER=1 AND a resolvable Anthropic credential.
Callers fall back to the deterministic score when either is missing or a
read fails, and the fallback verdict says so in its reasons. The flag
threshold (default 30) sits at the ~75-80th percentile of clean-PR risk
scores on both probe repos — roughly the top quarter gets flagged.
"""

import os
import re

from pydantic import BaseModel

from .models import Band, Reason, Verdict

MODEL = "claude-opus-5"
MAX_TOKENS = 6000
EFFORT = "medium"
DIFF_BUDGET = 30_000  # chars
DEFAULT_READER_THRESHOLD = 30  # risk_score points, 0-100
DEFAULT_READ_TIMEOUT_S = 120  # seconds, whole read incl. retries' backoff

SYSTEM = (
    "You are reviewing a single pull request diff from a large production "
    "codebase. Judge the risk that this specific change introduces a defect "
    "that will later be reverted or hot-fixed. Flag concrete defect risks in "
    "the change itself — logic errors, unsafe migrations, concurrency "
    "hazards, error-handling gaps, contract mismatches — not style, tests, "
    "or hypothetical improvements. Most changes in a healthy repo are safe; "
    "score accordingly."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "risk_score": {
            "type": "integer",
            "description": (
                "0-100 risk that this change causes a revert or hotfix. "
                "Most PRs deserve <30; reserve >70 for changes you would block."
            ),
        },
        "rationale": {"type": "string", "description": "One or two sentences."},
        "findings": {
            "type": "array",
            "description": "Concrete defect risks in this change; empty if none.",
            "items": {
                "type": "object",
                "properties": {
                    "category_slug": {
                        "type": "string",
                        "description": (
                            "Short kebab-case defect pattern, reusable across "
                            "PRs — e.g. unsafe-migration, race-condition, "
                            "missing-null-check, api-contract-change."
                        ),
                    },
                    "description": {"type": "string"},
                    "file": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["category_slug", "description", "file", "severity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["risk_score", "rationale", "findings"],
    "additionalProperties": False,
}


# --- Intent tier -----------------------------------------------------------
#
# A second, separate read: the diff judged against the decisions the team
# already recorded. INTENT_SCHEMA is verbatim from scripts/intent_probe.py,
# the Experiment B v2 shape that passed (HIGH-severity deviations on 4% of
# matched PRs vs 100% of mismatched, alignment 80 vs 2).
#
# The system prompt is NOT verbatim, and the difference matters. B v2's
# prompt says "the issue/ticket this PR claims to resolve" and defines
# missing-from-pr as "things the ticket asks for that the PR does not do".
# A recorded decision asks nothing of a PR. Reusing that wording would be
# false on its face, so this is a sibling prompt — frozen from creation on
# the same terms as SYSTEM (ADR-0002), and unvalidated until the
# derangement check runs. B v2 is prior evidence the capability is real,
# not evidence that this prompt works.

DECISION_INTENT_SYSTEM = (
    "You are reviewing a single pull request diff from a large production "
    "codebase, together with the architecture decisions this team has "
    "already recorded and still considers binding. Judge whether the change "
    "departs from those decisions. Report a deviation when the diff makes a "
    "material change a recorded decision does not sanction (beyond-ticket), "
    "when it contradicts a recorded decision outright (contradicts-ticket), "
    "or when it claims to implement a decision but leaves a required part "
    "undone (missing-from-pr). Routine implementation detail the decisions "
    "leave open is NOT a deviation, and neither is work that is simply "
    "unrelated to every decision you were given — most changes touch none "
    "of them. Judge only against the decisions provided; do not invent "
    "policy. Also report defect risks in the change itself, as usual."
)

INTENT_SCHEMA = {
    **{k: v for k, v in SCHEMA.items() if k != "properties"},
    "properties": {
        **SCHEMA["properties"],
        "intent_alignment": {
            "type": "integer",
            "description": (
                "0-100: how fully and faithfully the diff implements the ticket's intent."
            ),
        },
        "deviation_findings": {
            "type": "array",
            "description": (
                "Gaps between ticket intent and diff behavior; empty if the PR "
                "does what the ticket asks."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["missing-from-pr", "beyond-ticket", "contradicts-ticket"],
                    },
                    "description": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["type", "description", "severity"],
                "additionalProperties": False,
            },
        },
    },
    "required": [*SCHEMA["required"], "intent_alignment", "deviation_findings"],
}


class ReaderError(RuntimeError):
    """A read failed (refusal, truncation, transport) — fall back and say so."""


class ReaderFinding(BaseModel):
    category_slug: str
    description: str
    file: str
    severity: str


class ReaderVerdict(BaseModel):
    risk_score: int
    rationale: str
    findings: list[ReaderFinding]


def enabled() -> bool:
    return os.environ.get("DOUG_READER") == "1"


def reader_threshold() -> float:
    return float(os.environ.get("DOUG_READER_THRESHOLD", DEFAULT_READER_THRESHOLD))


def read_timeout() -> float:
    return float(os.environ.get("DOUG_READ_TIMEOUT_S", DEFAULT_READ_TIMEOUT_S))


def _client():
    """A client with a bounded timeout, never the SDK default.

    The SDK defaults to a 600s timeout, and both read entry points run
    synchronously on Starlette's shared request thread pool (~40 workers,
    /healthz included). At the default, one stalled upstream connection
    parks a worker for ten minutes; forty of them and the whole service
    reads as down. 120s is well above any legitimate read and turns the
    same stall into a contained ReaderError fallback instead.
    """
    import anthropic

    return anthropic.Anthropic(timeout=read_timeout())


def _sent_slice(diff: str) -> str:
    """The exact bytes DIFF_BUDGET admits — the one place this slice happens.

    coverage() re-derives what a read saw from this same function, so it can
    never drift from what _user_text actually sent to the model.
    """
    return diff[:DIFF_BUDGET]


def _user_text(pr, diff: str) -> str:
    sent = _sent_slice(diff)
    truncated = len(diff) > len(sent)
    return (
        f"Title: {pr.title}\n"
        f"Files changed: {', '.join(pr.files)}\n"
        + ("[diff truncated at budget]\n" if truncated else "")
        + f"\n{sent}"
    )


# --- what the read actually saw ------------------------------------------
#
# _user_text cuts the diff at DIFF_BUDGET and moves on. That cut is silent
# everywhere downstream: a verdict from a fully-read PR and a verdict from a
# 44%-read PR are the same shape, store the same columns, and render the
# same way. lemahq/lema#643 cleared at 0.26 having been shown 30,000 of
# 68,430 chars; the tenancy leak a human later found was 2,266 chars past
# the cut, and the mutation-verified test file that would have deduped two
# of its other findings was never sent at all.
#
# These functions do not change what the model is given — DIFF_BUDGET and
# _user_text are frozen probe parameters (ADR-0002). They only make the cut
# observable, so a partial read stops looking like a complete one.


def diff_chunk(filename: str, status: str, additions: int, deletions: int, patch: str) -> str:
    """One file's block, in the one shape review.py is allowed to build it.

    review.py used to write this f-string twice (fetch_pr, fetch_open_prs)
    and _FILE_HEADER re-derived the same shape a third time, independently.
    A format change in any one of the three would have silently broken
    coverage() — files_sent dropping to 0, a complete read reporting itself
    as fully unseen — without an error anywhere. One function, used
    everywhere the shape is needed, is what makes that impossible now.
    """
    return f"### {filename} ({status}, +{additions}/-{deletions})\n{patch}"


CHUNK_SEPARATOR = "\n\n"

_FILE_HEADER = re.compile(r"^### (.+) \([a-z]+, \+\d+/-\d+\)$", re.M)


class Coverage(BaseModel):
    """How much of a PR's diff reached the model.

    `file_cut` is the file the budget landed inside — seen in part, and the
    most dangerous case, because the model has enough of it to reason about
    and not enough to be right.
    """

    diff_chars: int
    sent_chars: int
    files_sent: int
    files_unseen: list[str]
    file_cut: str | None = None

    @property
    def complete(self) -> bool:
        return self.sent_chars >= self.diff_chars

    @property
    def fraction(self) -> float:
        return 1.0 if not self.diff_chars else self.sent_chars / self.diff_chars


def coverage(diff: str) -> Coverage:
    """Observe the truncation _user_text performs. Pure; sends nothing.

    Files are counted from the diff's own `### path (status, +a/-d)` headers
    rather than from a PR's file list, because fetch_pr drops files GitHub
    returns without a patch (binary, or too large to inline). Those never
    had a chance to be read, which is a different hole from this one.
    """
    sent = _sent_slice(diff)
    matches = list(_FILE_HEADER.finditer(diff))
    all_files = [m.group(1) for m in matches]
    # A header counts as sent only if it arrived in full — a header cut
    # mid-line never matches _FILE_HEADER's `$` at all, so it is correctly
    # absent from `seen` and its file lands in files_unseen below.
    seen = [m for m in matches if m.end() <= len(sent)]
    names = [m.group(1) for m in seen]
    cut = None
    if len(sent) < len(diff) and seen:
        last = len(seen) - 1
        # review.py joins chunks with exactly CHUNK_SEPARATOR, so the last
        # seen file's real content ends CHUNK_SEPARATOR chars before the
        # next header starts (or at len(diff), if it's the final file).
        # Only call this file "cut" when the missing span is bigger than
        # that separator — otherwise the budget landed cleanly between two
        # whole files, and nothing about this one was actually partial.
        next_start = matches[last + 1].start() if last + 1 < len(matches) else len(diff)
        if next_start - len(sent) > len(CHUNK_SEPARATOR):
            cut = names[-1]
    return Coverage(
        diff_chars=len(diff),
        sent_chars=len(sent),
        files_sent=len(names),
        files_unseen=[f for f in all_files if f not in names],
        file_cut=cut,
    )


def truncation_reason(cov: Coverage) -> Reason | None:
    """A loud line on the verdict when the read was partial, or None.

    Deliberately outside the `reader:` namespace: patterns.from_rule only
    canonicalises `reader:` rules, so this shares the findings table with
    real defect patterns without ever being counted as one. A meta-fact
    about the read is not a defect pattern, and pooling the two would
    corrupt the precision table it feeds.
    """
    if cov.complete:
        return None
    unseen = cov.files_unseen[:3]
    tail = f" (+{len(cov.files_unseen) - 3} more)" if len(cov.files_unseen) > 3 else ""
    label = (
        f"Partial read: {cov.fraction:.0%} of the diff "
        f"({cov.sent_chars:,} of {cov.diff_chars:,} chars)."
        + (f" Cut inside {cov.file_cut}." if cov.file_cut else "")
        + (f" Never sent: {', '.join(unseen)}{tail}." if unseen else "")
        + " Findings below cover only what was sent; a clear is not evidence"
        " about the rest."
    )
    return Reason(rule="read-truncated", label=label, weight=0.0)


def read_diff(pr, diff: str, client=None) -> ReaderVerdict:
    if client is None:
        client = _client()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": _user_text(pr, diff)}],
        )
    except Exception as e:  # noqa: BLE001 — every transport failure is a ReaderError
        # Anything the SDK raises — billing, rate limit, timeout, 5xx — is a
        # failed read, and this module's contract is that a failed read falls
        # back loudly rather than propagating. Letting these escape meant one
        # exhausted balance 500'd every customer's CI, reported as success
        # because the workflow step is continue-on-error.
        raise ReaderError(f"{type(e).__name__}: {e}") from e
    if response.stop_reason != "end_turn":
        raise ReaderError(f"read stopped with {response.stop_reason}")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return ReaderVerdict.model_validate_json(text)
    except ValueError as e:
        raise ReaderError(f"unparseable reader output: {e}") from e


class DeviationFinding(BaseModel):
    type: str
    description: str
    severity: str


class IntentReaderVerdict(ReaderVerdict):
    intent_alignment: int
    deviation_findings: list[DeviationFinding]


def intent_enabled() -> bool:
    return os.environ.get("DOUG_INTENT") == "1"


def _intent_text(pr, diff: str, docs) -> str:
    """Decisions first, then the diff — same ordering the probe validated."""
    block = "\n\n".join(
        f"[{d.id}] {d.title}\n{d.body}" for d in docs
    )
    return (
        "Recorded architecture decisions this team considers binding:\n"
        f"{block}\n\n---\n" + _user_text(pr, diff)
    )


def read_with_decisions(pr, diff: str, docs, client=None) -> IntentReaderVerdict:
    """The intent read. Never called with an empty `docs` — a read with no
    decisions in it is the diff-only read, and asking the model to compare
    against nothing invites invented findings."""
    if not docs:
        raise ReaderError("no decision records to read against")
    if client is None:
        client = _client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": INTENT_SCHEMA},
        },
        system=DECISION_INTENT_SYSTEM,
        messages=[{"role": "user", "content": _intent_text(pr, diff, docs)}],
    )
    if response.stop_reason != "end_turn":
        raise ReaderError(f"intent read stopped with {response.stop_reason}")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return IntentReaderVerdict.model_validate_json(text)
    except ValueError as e:
        raise ReaderError(f"unparseable intent output: {e}") from e


def verdict_from_reader(rv: ReaderVerdict, threshold: float | None = None) -> Verdict:
    thr = reader_threshold() if threshold is None else threshold
    band = Band.FLAGGED if rv.risk_score >= thr else Band.CLEARED
    reasons = [
        Reason(
            rule=f"reader:{f.category_slug}",
            label=f.description,
            weight=0.0,
            severity=f.severity,
        )
        for f in rv.findings
    ]
    return Verdict(
        score=round(rv.risk_score / 100, 2),
        band=band,
        threshold=thr / 100,
        reasons=reasons,
    )
