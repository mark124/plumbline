"""Rendering findings for humans and for CI.

Two audiences. A person skimming a pull request comment wants the verdict
first and the reasoning underneath. A CI job wants an exit code. Both want to
be told when a check did not actually run.
"""

from __future__ import annotations

import json
from typing import List

from .findings import Check, Finding, Report, Severity

SEVERITY_ORDER = [Severity.ERROR, Severity.WARN, Severity.UNKNOWN, Severity.INFO]

SEVERITY_LABEL = {
    Severity.ERROR: "Error",
    Severity.WARN: "Warning",
    Severity.UNKNOWN: "Unknown",
    Severity.INFO: "Info",
}

SEVERITY_MARK = {
    Severity.ERROR: "[x]",
    Severity.WARN: "[!]",
    Severity.UNKNOWN: "[?]",
    Severity.INFO: "[i]",
}

CHECK_LABEL = {
    Check.PHANTOM_TABLE: "Phantom table",
    Check.PHANTOM_COLUMN: "Phantom column",
    Check.DEPRECATED_SOURCE: "Deprecated source",
    Check.UNVETTED_JOIN: "Unvetted join",
    Check.PII_PROPAGATION: "PII propagation",
    Check.BLAST_RADIUS: "Blast radius",
    Check.PARSE_FAILURE: "Parse failure",
}


def _location(f: Finding) -> str:
    if f.file and f.line:
        return f"{f.file}:{f.line}"
    if f.file:
        return f.file
    return ""


def _sorted(findings: List[Finding]) -> List[Finding]:
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_ORDER.index(f.severity),
            f.file or "",
            f.line or 0,
        ),
    )


def render_text(report: Report) -> str:
    """Terminal output."""
    lines: List[str] = []
    counts = report.counts

    if not report.findings:
        lines.append(f"Plumbline: nothing to report across {report.files_checked} file(s).")
    else:
        lines.append(
            f"Plumbline: {counts['error']} error, {counts['warn']} warning, "
            f"{counts['unknown']} unknown, {counts['info']} info "
            f"across {report.files_checked} file(s)."
        )
        lines.append("")
        for f in _sorted(report.findings):
            loc = _location(f)
            head = f"  {SEVERITY_MARK[f.severity]} {f.summary}"
            if loc:
                head += f"  ({loc})"
            lines.append(head)
            lines.append(f"      {f.detail}")
            if f.suggestion:
                lines.append(f"      Suggested: {f.suggestion}")
            if f.evidence_urn:
                lines.append(f"      Evidence: {f.evidence_urn}")
            lines.append("")

    if report.degraded:
        lines.append("Checks that did not run:")
        for d in report.degraded:
            lines.append(f"  - {d}")
        lines.append("")

    lines.append(
        "Errors block. Warnings and unknowns do not. "
        "An unknown means the catalog could not answer, not that the code is wrong."
    )
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    """A pull request comment."""
    counts = report.counts
    out: List[str] = ["## Plumbline"]

    if report.blocking_count:
        out.append(
            f"**{counts['error']} reference{'s' if counts['error'] != 1 else ''} in this "
            "change cannot be resolved against the catalog.**"
        )
    elif report.findings:
        out.append("**No blocking problems.** Some things are worth a look.")
    else:
        out.append(
            f"**Clean.** Every table and column in {report.files_checked} "
            "file(s) resolves against the catalog."
        )

    out.append("")
    out.append(
        f"| {counts['error']} error | {counts['warn']} warning | "
        f"{counts['unknown']} unknown | {counts['info']} info |"
    )
    out.append("| --- | --- | --- | --- |")
    out.append("")

    for severity in SEVERITY_ORDER:
        group = [f for f in report.findings if f.severity is severity]
        if not group:
            continue
        out.append(f"### {SEVERITY_LABEL[severity]}")
        out.append("")
        for f in _sorted(group):
            loc = _location(f)
            title = f"**{f.summary}**"
            if loc:
                title += f" `{loc}`"
            out.append(f"- {title}")
            out.append(f"  {f.detail}")
            if f.suggestion:
                out.append(f"  Suggested replacement: `{f.suggestion}`")
            if f.evidence_urn:
                out.append(f"  Catalog evidence: `{f.evidence_urn}`")
            out.append("")

    if report.degraded:
        out.append("### Checks that did not run")
        out.append("")
        for d in report.degraded:
            out.append(f"- {d}")
        out.append("")

    out.append("---")
    out.append(
        "Errors are references the catalog can disprove. Unknowns are references "
        "the catalog cannot speak to, usually because the asset is not ingested; "
        "they never block."
    )
    return "\n".join(out)


def render_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2)
