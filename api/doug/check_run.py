"""The check run — the one thing Doug writes to a pull request.

Advisory by construction: the conclusion is always neutral, so a Doug run
can never gate a merge. ADR-0010 replaces ADR-0003 and keeps its argument
intact — a router that blocks needs precision this evidence base does not
have, and the honest surface for a judgment that might be wrong is one
that costs nothing to ignore.

Two things this surface must never smooth over:

  * A deterministic fallback is not a read. review.score_one falls back
    silently when the reader is off or a read raised, and the Verdict is
    shape-identical either way — so the tier goes in the title, which is
    the only part visible from the PR's checks list.
  * Deviation findings come from the intent tier, whose derangement check
    did not pass (2026-07-31). The instrument is not validated, so they
    render in their own labelled section and never touch band or score
    (ADR-0007).
"""

import sys

from .models import Verdict
from .reader import Coverage, truncation_reason
from .review import IntentRead

NAME = "Doug"
# GitHub caps output.summary at 65535 chars and rejects the whole call over
# it. Leave headroom rather than discovering the cap on a 400-finding PR.
SUMMARY_LIMIT = 60_000
TITLE_LIMIT = 255

NEUTRAL_NOTE = (
    "Doug is advisory: this check is always neutral and never blocks a "
    "merge, whatever the band says."
)
FALLBACK_NOTE = (
    "**The validated diff-reader did not run.** This band and score come "
    "from the deterministic scorer, which never opens the diff — it scores "
    "PR shape (size, paths, authorship) alone. Read it as routing, not as "
    "a judgment about this change."
)
DEVIATION_HEADING = "### Decision deviations (unvalidated)"
DEVIATION_NOTE = (
    "The instrument behind this section has not passed its derangement "
    "check (2026-07-31), so these are unvalidated observations. They do "
    "not contribute to the band or score above (ADR-0007)."
)


def _headline(tier: str, verdict: Verdict) -> str:
    band = verdict.band.value
    if tier == "reader":
        return f"{band.capitalize()} · risk {verdict.score:.2f} · diff read"
    return f"Deterministic fallback · {band} · risk {verdict.score:.2f}"


def render(
    tier: str,
    verdict: Verdict,
    intent_read: IntentRead | None,
    coverage: Coverage | None,
) -> tuple[str, str]:
    """(title, summary_md) for one verdict."""
    title = _headline(tier, verdict)
    partial = truncation_reason(coverage) if coverage is not None else None

    lines = [
        f"**{title}**",
        "",
        f"Risk {verdict.score:.2f} against a flag line of {verdict.threshold:.2f}.",
        NEUTRAL_NOTE,
    ]
    if tier != "reader":
        lines += ["", FALLBACK_NOTE]
    if partial is not None:
        # The label already opens "Partial read:" — reader.truncation_reason
        # writes the whole sentence. Adding a heading of our own printed the
        # words twice and broke the caveat's own once-and-only-once rule.
        lines += ["", f"> {partial.label}"]

    # Folded into the block above, so it is stated once — but only when that
    # block rendered, so it can never be lost instead.
    skip = {"read-truncated"} if partial is not None else set()
    risks = [r for r in verdict.reasons if r.rule not in skip]
    lines += ["", "### Findings", ""]
    if risks:
        lines += [
            f"- `{r.rule}` — {r.label}" + (f" _({r.severity})_" if r.severity else "")
            for r in risks
        ]
    else:
        lines.append("- none")

    if intent_read is not None:
        lines += ["", DEVIATION_HEADING, "", DEVIATION_NOTE, ""]
        if intent_read.findings:
            lines += [
                f"- `{d.type}` — {d.description} _({d.severity})_"
                for d in intent_read.findings
            ]
        else:
            lines.append(f"- none (alignment {intent_read.alignment}/100)")
        lines += ["", f"Judged against: {', '.join(intent_read.refs) or 'no records'}."]

    return title[:TITLE_LIMIT], "\n".join(lines)[:SUMMARY_LIMIT]


def post(gh, owner: str, repo: str, head_sha: str, title: str, summary: str) -> None:
    """Create the check run. Never raises.

    This is an advisory surface hanging off work that is already durable —
    the verdict is in the ledger before this runs. A GitHub outage, a
    revoked installation or a force-pushed-away SHA must not turn a good
    verdict into a retried job.
    """
    try:
        gh.rest.checks.create(
            owner=owner,
            repo=repo,
            name=NAME,
            head_sha=head_sha,
            status="completed",
            conclusion="neutral",
            output={"title": title[:TITLE_LIMIT], "summary": summary[:SUMMARY_LIMIT]},
        )
    except Exception as e:  # noqa: BLE001 — advisory surface, never fails a job
        print(
            f"doug: check run not posted for {owner}/{repo}@{head_sha[:12]} "
            f"({type(e).__name__}: {e})",
            file=sys.stderr,
        )
