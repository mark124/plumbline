"""Tests for the HTML report.

The report is opened as a CI artifact by people deciding whether to merge, so
the things that matter are: it is self-contained, it escapes content that came
from a SQL file, and the evidence tier is encoded in more than colour.
"""

from __future__ import annotations

from plumbline.findings import Check, Finding, Report, Severity
from plumbline.report import render_html, render_json, render_markdown, render_text


def _report(*findings: Finding, degraded=(), files=1) -> Report:
    r = Report(files_checked=files)
    for f in findings:
        r.add(f)
    for d in degraded:
        r.degrade(d)
    return r


ERROR = Finding(
    check=Check.PHANTOM_COLUMN,
    severity=Severity.ERROR,
    summary="Column `order_ttl` does not exist",
    detail="No column named `order_ttl`.",
    file="model.sql",
    line=4,
    subject="order_ttl",
    suggestion="order_total",
    evidence_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.t,PROD)",
)

UNKNOWN = Finding(
    check=Check.PHANTOM_TABLE,
    severity=Severity.UNKNOWN,
    summary="Table `shipments` is not in the catalog",
    detail="It may simply not be ingested.",
    file="model.sql",
    line=2,
    subject="shipments",
)


def test_self_contained_no_external_requests():
    """It has to render off a network share or a CI artifact viewer."""
    out = render_html(_report(ERROR))
    for bad in ("http://", "https://", "<script", "cdn.", "@import"):
        assert bad not in out.replace("urn:li:", ""), f"found {bad!r}"


def test_escapes_content_that_came_from_a_sql_file():
    nasty = Finding(
        check=Check.PHANTOM_COLUMN,
        severity=Severity.ERROR,
        summary="Column `<img src=x onerror=alert(1)>` does not exist",
        detail="Injected via a file name or identifier.",
        file="<script>alert(1)</script>.sql",
        subject="<img src=x>",
    )
    out = render_html(_report(nasty))
    assert "<img src=x onerror" not in out
    assert "<script>alert(1)</script>.sql" not in out
    assert "&lt;" in out


def test_unknown_is_marked_structurally_not_only_by_colour():
    """A colourblind reader, or a greyscale print, must still see the tier."""
    out = render_html(_report(UNKNOWN))
    assert "line silent" in out, "unknown findings need the dashed treatment"


def test_error_and_unknown_get_different_treatments():
    out = render_html(_report(ERROR, UNKNOWN))
    assert "line err" in out
    assert "line silent" in out


def test_blocking_verdict_says_it_blocks():
    out = render_html(_report(ERROR))
    assert "Blocks merge" in out


def test_non_blocking_verdict_does_not_claim_to_block():
    out = render_html(_report(UNKNOWN))
    assert "Blocks merge" not in out
    assert "Does not block" in out


def test_clean_report_states_what_was_actually_verified():
    out = render_html(_report(files=3))
    assert "resolves against the catalog" in out
    assert "3 files" in out


def test_checks_that_did_not_run_are_shown():
    out = render_html(_report(ERROR, degraded=["No query history, joins not checked."]))
    assert "Checks that did not run" in out
    assert "joins not checked" in out


def test_backticks_become_code_spans():
    out = render_html(_report(ERROR))
    assert "<code>order_ttl</code>" in out


def test_verified_fix_is_rendered_when_present():
    fixed = Finding(
        check=Check.PHANTOM_COLUMN,
        severity=Severity.ERROR,
        summary="Column `x` does not exist",
        detail="d",
        fixed_sql="SELECT order_total FROM t",
    )
    out = render_html(_report(fixed))
    assert "Verified fix" in out
    assert "SELECT order_total FROM t" in out


# --- identifiers that are hostile to the output, not to the parser --------
#
# A warehouse will accept a quoted column name containing almost anything.
# The name is then copied into a terminal, a GitHub comment and a browser,
# each with different rules about what is data and what is instruction.


def _named(name: str) -> Report:
    return _report(
        Finding(
            check=Check.PHANTOM_COLUMN,
            severity=Severity.ERROR,
            summary=f"Column `{name}` does not exist",
            detail=f"No column named `{name}`.",
            subject=name,
            suggestion=name,
        )
    )


def test_carriage_return_cannot_overwrite_the_terminal_report():
    """A CR returns the cursor to the start of the line, so what follows
    overwrites what was just printed. A column named with one could erase the
    finding that reports it, which makes the report editable by its input."""
    out = render_text(_named("hidden\r" + " " * 80))
    assert "\r" not in out
    assert "\\r" in out


def test_escape_sequences_cannot_repaint_the_terminal():
    out = render_text(_named("red\x1b[31mshift"))
    assert "\x1b" not in out
    assert "\\x1b" in out


def test_a_newline_in_an_identifier_does_not_break_the_markdown_bullet():
    """One finding must render as exactly one list item."""
    out = render_markdown(_named("two\nlines"))
    body = out.split("### Error", 1)[1]
    assert body.count("\n- ") + body.startswith("- ") == 1
    assert "\\n" in out


def test_control_characters_are_escaped_in_html_too():
    out = render_html(_named("a\x1b[2Jb"))
    assert "\x1b" not in out


def test_json_keeps_the_true_identifier():
    """The data format must stay faithful: JSON escapes control characters
    itself, and a consumer needs the real name, not a display form."""
    import json

    name = "odd\rname"
    data = json.loads(render_json(_named(name)))
    assert data["findings"][0]["subject"] == name


def test_a_fix_block_keeps_its_newlines():
    """The escaping must not reach multi-line SQL, which is rendered as a
    block and needs its line breaks."""
    fixed = Finding(
        check=Check.PHANTOM_COLUMN,
        severity=Severity.ERROR,
        summary="Column `x` does not exist",
        detail="d",
        fixed_sql="SELECT a,\n       b\nFROM t",
    )
    text = render_text(_report(fixed))
    # Each SQL line is indented into the finding, so the check is that the
    # lines are still separate lines rather than one escaped string.
    assert [ln.strip() for ln in text.splitlines() if ln.startswith("        ")] == [
        "SELECT a,",
        "b",
        "FROM t",
    ]
    assert "\\n" not in render_html(_report(fixed))
