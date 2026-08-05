"""Post-read import settlements — REVIEWING.md resolution rule, no prompt edit."""

from doug import settle
from doug.reader import ReaderFinding, ReaderVerdict

FILE = """\
import threading
from pathlib import Path

def ready():
    threading.Thread(target=lambda: None).start()
    return Path('.')
"""

TYPE_CHECKING_ONLY = """\
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import threading

def ready():
    threading.Thread(target=lambda: None).start()
"""


def _f(slug="missing-import", desc="uses `threading` with no import", file="api.py"):
    return ReaderFinding(
        category_slug=slug, description=desc, file=file, severity="medium"
    )


def test_drops_missing_import_when_runtime_import_exists():
    rv = ReaderVerdict(risk_score=40, rationale="x", findings=[_f()])
    out, dropped = settle.drop_disproved_import_findings(rv, lambda p: FILE)
    assert len(dropped) == 1
    assert out.findings == []


def test_keeps_type_checking_only_import():
    """REVIEWING.md residual-real: TYPE_CHECKING import + runtime use."""
    rv = ReaderVerdict(risk_score=40, rationale="x", findings=[_f()])
    out, dropped = settle.drop_disproved_import_findings(
        rv, lambda p: TYPE_CHECKING_ONLY
    )
    assert dropped == []
    assert len(out.findings) == 1


def test_does_not_settle_undefined_name_via_later_assign():
    """Use-before-assign must not be cleared by a later binding."""
    src = "x = 1\n"  # name bound, but slug is undefined-name — out of scope
    f = ReaderFinding(
        category_slug="undefined-name",
        description="`x` is used before it is defined",
        file="api.py",
        severity="medium",
    )
    rv = ReaderVerdict(risk_score=40, rationale="x", findings=[f])
    out, dropped = settle.drop_disproved_import_findings(rv, lambda p: src)
    assert dropped == []
    assert out.findings == [f]


def test_keeps_finding_when_name_truly_absent():
    f = _f(desc="uses `asyncio` with no import")
    rv = ReaderVerdict(risk_score=40, rationale="x", findings=[f])
    out, dropped = settle.drop_disproved_import_findings(rv, lambda p: FILE)
    assert dropped == []
    assert out.findings == [f]


def test_keeps_finding_when_file_unreadable():
    rv = ReaderVerdict(risk_score=40, rationale="x", findings=[_f()])
    out, dropped = settle.drop_disproved_import_findings(rv, lambda p: None)
    assert dropped == []
    assert len(out.findings) == 1


def test_does_not_touch_non_resolution_findings():
    f = ReaderFinding(
        category_slug="race-condition",
        description="shared state without a lock",
        file="api.py",
        severity="high",
    )
    rv = ReaderVerdict(risk_score=70, rationale="x", findings=[f])
    out, dropped = settle.drop_disproved_import_findings(rv, lambda p: FILE)
    assert dropped == []
    assert out.findings == [f]


def test_settlement_notice_explains_empty_findings_list():
    notice = settle.settlement_notice([_f()])
    assert notice is not None
    assert notice.rule == "settled-missing-import"
    assert notice.weight == 0.0


def _sf(
    slug="unmigrated-column",
    desc="`installations.token_hash` has no migration and will raise on a deployed database",
    file="api/doug/store.py",
):
    return ReaderFinding(
        category_slug=slug, description=desc, file=file, severity="high"
    )


def test_claimed_columns_extracts_table_dot_column_from_backticks():
    assert settle.claimed_columns(_sf()) == [("installations", "token_hash")]


def test_claimed_columns_ignores_a_bare_table_name_with_no_column():
    f = _sf(desc="`deep_read_counters` looks unmigrated")
    assert settle.claimed_columns(f) == []


def test_claimed_columns_is_empty_when_a_bare_table_mention_accompanies_a_real_pair():
    """Doug's review of PR #49 (reader:overly-broad-regex-match): a
    whole-new-table claim (`deep_read_counters`, out of scope by design — no
    dot) must not be settled on an unrelated real `table.column` mentioned
    in the same description for context. The presence of ANY bare
    backtick-quoted name means we cannot tell which mention is the actual
    disputed claim, so we settle nothing rather than guess.
    """
    f = _sf(
        desc=(
            "`deep_read_counters` looks unmigrated; it extends verdicts.id "
            "via a foreign key and will raise on a deployed database"
        )
    )
    assert settle.claimed_columns(f) == []


def test_claimed_columns_ignores_dotted_mentions_without_backticks():
    """Doug's second review of PR #49 (reader:overly-broad-regex-match,
    residual): plain prose like 'migrations.py' or 'review.score_one' must
    not be read as a table.column claim just because it has a dot — every
    real historical instance quoted the claim in backticks."""
    f = _sf(
        desc=(
            "review.score_one calls settle without checking migrations.py "
            "first; verdicts.head_sha has no migration entry"
        )
    )
    assert settle.claimed_columns(f) == []


def test_does_not_classify_a_non_column_migration_finding_via_free_text():
    """Doug's second review of PR #49 (reader:heuristic-false-positive):
    the free-text fallback ('migrat' + 'missing') can classify a finding
    that has nothing to do with column existence — e.g. a concurrency bug
    in a migration script — into the settlement path, where an unrelated
    real `table.column` mention in the same text drops it."""
    f = ReaderFinding(
        category_slug="race-condition",
        description=(
            "The migration script is missing a WHERE clause guard; "
            "concurrent runs can double-apply it. Unrelated: "
            "`verdicts.head_sha` is a real column."
        ),
        file="api/doug/migrations.py",
        severity="high",
    )
    assert settle.looks_like_schema_dependency_finding(f) is False
    rv = ReaderVerdict(risk_score=70, rationale="x", findings=[f])
    out, dropped = settle.drop_disproved_schema_findings(
        rv, lambda table: {"head_sha"} if table == "verdicts" else None
    )
    assert dropped == []
    assert out.findings == [f]


def test_does_not_settle_a_whole_table_claim_via_an_incidental_column_mention():
    """Same case as above, through the public entry point."""
    f = _sf(
        desc=(
            "`deep_read_counters` looks unmigrated; it extends verdicts.id "
            "via a foreign key and will raise on a deployed database"
        )
    )
    rv = ReaderVerdict(risk_score=60, rationale="x", findings=[f])
    out, dropped = settle.drop_disproved_schema_findings(
        rv, lambda table: {"id"} if table == "verdicts" else None
    )
    assert dropped == []
    assert out.findings == [f]


def test_drops_schema_finding_when_column_exists_in_live_schema():
    rv = ReaderVerdict(risk_score=60, rationale="x", findings=[_sf()])
    out, dropped = settle.drop_disproved_schema_findings(
        rv, lambda table: frozenset({"id", "token_hash"})
    )
    assert len(dropped) == 1
    assert out.findings == []


def test_keeps_schema_finding_when_column_truly_absent():
    f = _sf()
    rv = ReaderVerdict(risk_score=60, rationale="x", findings=[f])
    out, dropped = settle.drop_disproved_schema_findings(
        rv, lambda table: frozenset({"id"})
    )
    assert dropped == []
    assert out.findings == [f]


def test_keeps_schema_finding_when_table_unresolvable():
    """None means "can't tell" (no DB, table not found) — never settle on that."""
    f = _sf()
    rv = ReaderVerdict(risk_score=60, rationale="x", findings=[f])
    out, dropped = settle.drop_disproved_schema_findings(rv, lambda table: None)
    assert dropped == []
    assert out.findings == [f]


def test_does_not_settle_non_schema_findings_via_schema_filter():
    f = ReaderFinding(
        category_slug="race-condition",
        description="shared state without a lock",
        file="api.py",
        severity="high",
    )
    rv = ReaderVerdict(risk_score=70, rationale="x", findings=[f])
    out, dropped = settle.drop_disproved_schema_findings(
        rv, lambda table: frozenset({"anything"})
    )
    assert dropped == []
    assert out.findings == [f]


def test_schema_settlement_notice_explains_empty_findings_list():
    notice = settle.schema_settlement_notice([_sf()])
    assert notice is not None
    assert notice.rule == "settled-schema-dependency"
    assert notice.weight == 0.0


def test_score_one_applies_schema_settlement_and_keeps_score(monkeypatch):
    from doug import reader, review
    from doug.models import AuthorType, Band, PRMetadata

    monkeypatch.setenv("DOUG_READER", "1")
    monkeypatch.setattr(reader, "enabled", lambda: True)
    monkeypatch.setattr(
        reader,
        "read_diff",
        lambda meta, diff, *, scope, client=None: ReaderVerdict(
            risk_score=60,
            rationale="x",
            findings=[_sf()],
        ),
    )
    meta = PRMetadata(
        number=1,
        title="t",
        author="a",
        author_type=AuthorType.HUMAN,
        additions=1,
        deletions=0,
        files=["api/doug/store.py"],
        approvals=0,
        approval_latency_s=None,
        days_since_last_human_commit=None,
        files_added=0,
        files_modified=1,
        url="https://example.test/1",
        head_sha="abc",
        changed_files=1,
        files_dropped=[],
    )
    tier, verdict, rv, _cov = review.score_one(
        meta,
        "+ x",
        scope=reader.SENTINEL_SCOPE,
        resolve_schema=lambda table: frozenset({"id", "token_hash"}),
    )
    assert tier == "reader"
    assert rv is not None
    assert rv.findings == []
    assert verdict.score == 0.6
    assert verdict.band is Band.FLAGGED
    assert any(r.rule == "settled-schema-dependency" for r in verdict.reasons)
    assert all(r.rule != "reader:unmigrated-column" for r in verdict.reasons)


def test_score_one_applies_settlement_and_keeps_score(monkeypatch):
    from doug import reader, review
    from doug.models import AuthorType, Band, PRMetadata

    monkeypatch.setenv("DOUG_READER", "1")
    monkeypatch.setattr(reader, "enabled", lambda: True)
    monkeypatch.setattr(
        reader,
        "read_diff",
        lambda meta, diff, *, scope, client=None: ReaderVerdict(
            risk_score=40,
            rationale="x",
            findings=[_f()],
        ),
    )
    meta = PRMetadata(
        number=1,
        title="t",
        author="a",
        author_type=AuthorType.HUMAN,
        additions=1,
        deletions=0,
        files=["api.py"],
        approvals=0,
        approval_latency_s=None,
        days_since_last_human_commit=None,
        files_added=0,
        files_modified=1,
        url="https://example.test/1",
        head_sha="abc",
        changed_files=1,
        files_dropped=[],
    )
    tier, verdict, rv, _cov = review.score_one(
        meta, "+ x", scope=reader.SENTINEL_SCOPE, resolve_file=lambda p: FILE
    )
    assert tier == "reader"
    assert rv is not None
    assert rv.findings == []
    # Score is the instrument's; we do not rewrite it after settlement.
    assert verdict.score == 0.4
    assert verdict.band is Band.FLAGGED
    assert any(r.rule == "settled-missing-import" for r in verdict.reasons)
    assert all(r.rule != "reader:missing-import" for r in verdict.reasons)
