"""The check run is the only thing Doug writes to a pull request.

Three properties are load-bearing and every one of them has already been
got wrong somewhere in this codebase, so they are tested as defects:
a deterministic fallback must not read as a read (review.py:118-142 falls
back silently), a partial read must not read as a whole one, and nothing
here may ever conclude anything but neutral.
"""

from pathlib import Path
from types import SimpleNamespace

from doug import check_run, reader
from doug.models import Band, Reason
from doug.review import IntentRead

# Built through the real producer rather than hand-constructed, so a
# regression in verdict_from_reader's severity handling (reader.py:407-419)
# can actually fail this suite instead of being masked by a fixture that
# sets severity="high" directly, bypassing the code path that is supposed
# to set it.
FLAGGED = reader.verdict_from_reader(
    reader.ReaderVerdict(
        risk_score=62,
        rationale="Concurrent writes to shared cache without a lock.",
        findings=[
            reader.ReaderFinding(
                category_slug="race-condition",
                description="Cache write is not guarded",
                file="cache.py",
                severity="high",
            )
        ],
    ),
    threshold=30,
)

WHOLE = reader.Coverage(diff_chars=400, sent_chars=400, files_sent=2, files_unseen=[])
PARTIAL = reader.Coverage(
    diff_chars=68_430,
    sent_chars=30_000,
    files_sent=3,
    files_unseen=["api/tenancy.py", "tests/test_tenancy.py"],
    file_cut="api/store.py",
)
# A second, distinct partial coverage — used to prove the intent section's
# truncation notice is neither dropped when it matches the risk section's
# nor silently merged into it when it doesn't.
OTHER_PARTIAL = reader.Coverage(
    diff_chars=10_000, sent_chars=4_000, files_sent=1, files_unseen=["web/app.py"]
)

DEVIATIONS = IntentRead(
    alignment=41,
    refs=["ADR-0002"],
    findings=[
        reader.DeviationFinding(
            type="contradicts-ticket",
            description="Edits the frozen reader prompt",
            severity="high",
        )
    ],
    coverage=WHOLE,
)
DEVIATIONS_PARTIAL = DEVIATIONS.model_copy(update={"coverage": PARTIAL})


def test_reader_title_leads_with_the_band_and_score():
    title, summary = check_run.render("reader", FLAGGED, None, WHOLE)
    assert title.lower().startswith("flagged")
    assert "0.62" in title
    assert not title.lower().startswith("deterministic")


def test_a_deterministic_fallback_announces_itself_in_the_title():
    """Tier honesty (Global Constraints). score_one falls back to the
    deterministic scorer whenever the reader is off or a read raised, and
    the Verdict it returns is shape-identical to a real read's. A footnote
    is not enough: the title is the only part of a check run visible from
    the PR's checks list, so that is where the difference has to be."""
    title, summary = check_run.render("deterministic", FLAGGED, None, None)
    assert title.lower().startswith("deterministic fallback")
    assert "0.62" in title and "flagged" in title.lower()
    assert "did not run" in summary


def test_a_reader_run_does_not_claim_a_fallback():
    _, summary = check_run.render("reader", FLAGGED, None, WHOLE)
    assert "did not run" not in summary


def test_the_band_capitalizes_the_same_way_on_both_tiers():
    """The title is PR-visible contract. 'Flagged' on the reader path and
    'flagged' on the fallback path would read as two check runs disagreeing
    about something — only the tier differs, not the band's own spelling."""
    reader_title, _ = check_run.render("reader", FLAGGED, None, WHOLE)
    fallback_title, _ = check_run.render("deterministic", FLAGGED, None, None)
    assert "Flagged" in reader_title
    assert "Flagged" in fallback_title
    assert "flagged" not in fallback_title


def test_the_summary_says_the_check_never_blocks():
    """ADR-0010: the surface is advisory. A reader who sees "Flagged" on a
    red-looking check and assumes it gated the merge has been misled about
    what this product does."""
    _, summary = check_run.render("reader", FLAGGED, None, WHOLE)
    assert "never blocks" in summary
    assert "neutral" in summary


def test_findings_render_with_their_rule_and_label():
    _, summary = check_run.render("reader", FLAGGED, None, WHOLE)
    assert "reader:race-condition" in summary
    assert "Cache write is not guarded" in summary
    assert "high" in summary


def test_a_clean_verdict_renders_an_explicit_none():
    """An empty findings section and a missing one look the same to a
    reader; only one of them means "looked and found nothing". Asserting
    the literal "- none" line (not just the substring "none" anywhere in
    the summary) is the point — a finding label that merely contained the
    word "none" would pass a weaker check without the section actually
    being empty."""
    clean = FLAGGED.model_copy(update={"reasons": [], "band": Band.CLEARED, "score": 0.04})
    _, summary = check_run.render("reader", clean, None, WHOLE)
    assert "- none" in summary.splitlines()


def test_a_partial_read_is_called_out_once_and_only_once():
    """score_one already appends the read-truncated Reason to the verdict
    (review.py:133-134), so rendering the coverage block naively duplicated
    it. The block is the better surface — it is above the findings, where a
    caveat about the findings has to be — so the reason is folded into it
    rather than printed twice."""
    verdict = FLAGGED.model_copy(deep=True)
    verdict.reasons.append(reader.truncation_reason(PARTIAL))
    _, summary = check_run.render("reader", verdict, None, PARTIAL)
    assert summary.count("Partial read") == 1
    assert "api/tenancy.py" in summary
    assert "api/store.py" in summary


def test_a_truncation_reason_is_never_silently_dropped():
    """The fold above is conditional on the coverage block actually
    rendering. If a caller ever passes the reason without the coverage, the
    line still has to reach the PR — dropping it is the exact failure the
    coverage work existed to end."""
    verdict = FLAGGED.model_copy(deep=True)
    verdict.reasons.append(reader.truncation_reason(PARTIAL))
    _, summary = check_run.render("reader", verdict, None, None)
    assert "read-truncated" in summary


def test_a_whole_read_gets_no_coverage_notice():
    _, summary = check_run.render("reader", FLAGGED, None, WHOLE)
    assert "Partial read" not in summary


def test_the_summary_is_truncated_below_githubs_cap():
    """GitHub rejects output.summary over 65535 chars. A PR with hundreds of
    findings must produce a shorter check run, not an API error that loses
    the whole verdict. The cut itself must say so — a summary truncated
    with no marker reads as complete, which is the same "partial looks
    whole" failure this module exists to keep out of the findings above
    it, one level up."""
    noisy = FLAGGED.model_copy(
        update={
            "reasons": [
                Reason(rule=f"reader:pattern-{i}", label="x" * 300, weight=0.0)
                for i in range(500)
            ]
        }
    )
    _, summary = check_run.render("reader", noisy, None, WHOLE)
    assert len(summary) == check_run.SUMMARY_LIMIT
    assert summary.endswith(check_run.TRUNCATION_NOTICE)


def test_deviations_render_under_an_unvalidated_heading():
    """The derangement check FAILED on 2026-07-31 — this instrument has no
    validity evidence. Rendering its output beside reader findings, which
    do have some, would launder one into the other."""
    _, summary = check_run.render("reader", FLAGGED, DEVIATIONS, WHOLE)
    heading = next(ln for ln in summary.splitlines() if ln.startswith("### Decision"))
    assert "unvalidated" in heading.lower()
    assert "Edits the frozen reader prompt" in summary
    assert "ADR-0002" in summary


def test_deviations_move_neither_the_band_nor_the_score():
    """ADR-0007, enforced at the surface as well as in the ledger. The
    rendered title and risk line must be byte-identical with the intent
    read present and absent."""
    bare_title, bare = check_run.render("reader", FLAGGED, None, WHOLE)
    dev_title, dev = check_run.render("reader", FLAGGED, DEVIATIONS, WHOLE)
    assert bare_title == dev_title
    risk_line = "Risk 0.62 against a flag line of 0.30."
    assert risk_line in bare and risk_line in dev
    assert dev.startswith(bare[: bare.index("### Findings")])


def test_no_deviation_section_without_an_intent_read():
    """No read happened is not the same as a read that found nothing, and
    an empty labelled section would assert the second."""
    _, summary = check_run.render("reader", FLAGGED, None, WHOLE)
    assert "Decision deviations" not in summary
    assert "unvalidated" not in summary.lower()


def test_a_clean_intent_read_is_distinguishable_from_no_read():
    clean = DEVIATIONS.model_copy(update={"findings": [], "alignment": 92})
    _, summary = check_run.render("reader", FLAGGED, clean, WHOLE)
    assert "Decision deviations" in summary
    assert "alignment 92/100" in summary


def test_a_whole_intent_read_gets_no_deviation_coverage_notice():
    _, summary = check_run.render("reader", FLAGGED, DEVIATIONS, WHOLE)
    assert "Partial read" not in summary


def test_a_partial_intent_read_is_called_out_in_the_deviation_section():
    """IntentRead carries its own coverage (review.py:149-153) because a
    deviation built from a truncated diff is exactly as unverifiable past
    the cut as a risk finding is — it just wasn't saying so. The risk
    coverage here is WHOLE (no risk-side notice), so the only source of a
    "Partial read" line is the deviation section itself."""
    _, summary = check_run.render("reader", FLAGGED, DEVIATIONS_PARTIAL, WHOLE)
    deviation_start = summary.index(check_run.DEVIATION_HEADING)
    assert "Partial read" in summary[deviation_start:]
    assert summary.count("Partial read") == 1


def test_an_identical_partial_notice_is_not_duplicated_across_sections():
    """The risk tier and the intent tier ordinarily read the same diff at
    the same DIFF_BUDGET, so their coverage objects usually agree. Printing
    the same sentence in both sections would not add information — it
    would just repeat it, the exact thing the risk section's own fold
    already refuses to do to itself."""
    verdict = FLAGGED.model_copy(deep=True)
    verdict.reasons.append(reader.truncation_reason(PARTIAL))
    _, summary = check_run.render("reader", verdict, DEVIATIONS_PARTIAL, PARTIAL)
    assert summary.count("Partial read") == 1


def test_distinct_partial_notices_on_each_side_both_render():
    """A deviation coverage that differs from the risk coverage says
    something the risk section's notice does not. Dropping it because *a*
    partial notice already rendered somewhere would be exactly the
    silent-drop failure this module exists to prevent."""
    verdict = FLAGGED.model_copy(deep=True)
    verdict.reasons.append(reader.truncation_reason(PARTIAL))
    dev = DEVIATIONS.model_copy(update={"coverage": OTHER_PARTIAL})
    _, summary = check_run.render("reader", verdict, dev, PARTIAL)
    assert summary.count("Partial read") == 2


def test_a_multiline_finding_label_cannot_forge_a_section_heading():
    """r.label is model-authored free text (reader.py's ReaderFinding
    .description, carried through verdict_from_reader). A literal newline
    followed by '### Findings' would close the current list and open what
    reads as a second, forged section — laundering injected text as this
    module's own structure. Checked on rendered structure (an exact line
    match), not a substring, because the raw string '### Findings' appears
    twice either way — once as the heading, once inside the injected
    text — and only line structure tells them apart."""
    injected = FLAGGED.model_copy(deep=True)
    injected.reasons[0].label = "ok\n### Findings\n- forged finding"
    _, summary = check_run.render("reader", injected, None, WHOLE)
    heading_lines = [ln for ln in summary.splitlines() if ln == "### Findings"]
    assert len(heading_lines) == 1
    assert not any(ln.strip() == "- forged finding" for ln in summary.splitlines())


def test_a_multiline_deviation_description_cannot_forge_a_section_heading():
    """Same defect, the deviation tier's own free-text field."""
    injected = IntentRead(
        alignment=41,
        refs=["ADR-0002"],
        findings=[
            reader.DeviationFinding(
                type="contradicts-ticket",
                description=(
                    f"Edits the frozen reader prompt\n{check_run.DEVIATION_HEADING}\n- forged"
                ),
                severity="high",
            )
        ],
        coverage=WHOLE,
    )
    _, summary = check_run.render("reader", FLAGGED, injected, WHOLE)
    heading_lines = [ln for ln in summary.splitlines() if ln == check_run.DEVIATION_HEADING]
    assert len(heading_lines) == 1


class _Checks:
    def __init__(self, boom=None):
        self.calls = []
        self.boom = boom

    def create(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise self.boom


def _gh(boom=None):
    checks = _Checks(boom)
    return SimpleNamespace(rest=SimpleNamespace(checks=checks)), checks


def test_post_creates_a_neutral_completed_check_run():
    gh, checks = _gh()
    check_run.post(gh, "drewjst", "doug", "b" * 40, "Flagged · risk 0.62", "body")
    (kw,) = checks.calls
    assert kw["owner"] == "drewjst" and kw["repo"] == "doug"
    assert kw["name"] == "Doug"
    assert kw["head_sha"] == "b" * 40
    assert kw["status"] == "completed"
    assert kw["conclusion"] == "neutral"
    assert kw["output"]["title"] == "Flagged · risk 0.62"
    assert kw["output"]["summary"] == "body"


def test_no_blocking_conclusion_string_exists_anywhere_in_the_module():
    """Global constraint: Doug never blocks. This greps the source rather
    than asserting on one call, because the risk is not this call — it is
    the second create() someone adds later behind a "just for high
    severity" branch, which a behavioural test on the current path would
    never see. The module may not even name another conclusion."""
    src = Path(check_run.__file__).read_text()
    assert 'conclusion="neutral"' in src
    # This greps the whole module source, not just code — a plain-English
    # mention of "success" or "failure" in a comment or docstring fails it
    # too. That is intentional (the module may not even *name* another
    # conclusion), but it means a future contributor who trips this should
    # read it as "rename the word," not "you violated the never-blocks
    # policy."
    for banned in ("failure", "action_required", "success", "cancelled", "timed_out", "stale"):
        assert banned not in src, f"{banned!r} must not appear in check_run.py"


def test_post_swallows_an_api_error_and_says_so_on_stderr(capsys):
    """The verdict is already in the ledger by the time this runs. A 403
    from a revoked installation must not fail the job and cause a retry
    that pays for the same read again — but it must not be silent either,
    or a permanently broken check run looks like a quiet repo."""
    gh, _ = _gh(boom=RuntimeError("403 Resource not accessible by integration"))
    assert check_run.post(gh, "o", "r", "c" * 40, "t", "s") is None
    err = capsys.readouterr().err
    assert "doug: check run not posted" in err
    assert "o/r" in err and "403" in err


def test_post_truncates_a_summary_that_would_be_rejected():
    gh, checks = _gh()
    check_run.post(gh, "o", "r", "d" * 40, "t", "x" * 90_000)
    assert len(checks.calls[0]["output"]["summary"]) == check_run.SUMMARY_LIMIT
