"""Rendering findings for humans and for CI.

Two audiences. A person skimming a pull request comment wants the verdict
first and the reasoning underneath. A CI job wants an exit code. Both want to
be told when a check did not actually run.
"""

from __future__ import annotations

import html
import json
import re
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
            if f.fixed_sql:
                lines.append("      Verified fix:")
                for sql_line in f.fixed_sql.splitlines():
                    lines.append(f"        {sql_line}")
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
            if f.fixed_sql:
                out.append("")
                out.append(
                    "  <details><summary>Verified fix (re-checked against the "
                    "catalog)</summary>"
                )
                out.append("")
                out.append("  ```sql")
                for sql_line in f.fixed_sql.splitlines():
                    out.append(f"  {sql_line}")
                out.append("  ```")
                out.append("")
                out.append("  </details>")
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


# --- HTML -----------------------------------------------------------------
#
# A self-contained page, because this is meant to survive being uploaded as a
# CI artifact or opened off a network share. No webfonts, no CDN, no script.
#
# The design carries one idea: severity is a statement about evidence, not
# about alarm. A finding the catalog can disprove and a finding the catalog
# could not speak to are different in kind, so they are drawn differently in
# kind. The rule down the left is a plumb line. Where a reading was taken it
# is solid with a filled bob; where the catalog was silent it goes dashed and
# hollow. Colour alone would not survive a colourblind reader or a greyscale
# print, and would quietly turn "I don't know" into "this is wrong".

_TIER_COPY = {
    Severity.ERROR: ("Error", "the catalog disproves this"),
    Severity.WARN: ("Warning", "true, and a judgment call"),
    Severity.UNKNOWN: ("Unknown", "the catalog could not answer"),
    Severity.INFO: ("Info", "context, not a defect"),
}

_CSS = """
:root {
  --paper: #e7eaee;
  --card: #f4f6f8;
  --ink: #12161b;
  --ink-soft: #4a5560;
  --ink-faint: #77828e;
  --rule: #c2c9d1;
  --chalk: #1e5f8c;
  --brass: #a8792f;
  --oxide: #9d3024;
  --amber: #96631a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #10141a; --card: #171d25; --ink: #e6eaef; --ink-soft: #a8b3bf;
    --ink-faint: #78828d; --rule: #2b333d; --chalk: #6fb2dd;
    --brass: #d3a24e; --oxide: #e2705f; --amber: #d9a441;
  }
}
:root[data-theme="dark"] {
  --paper: #10141a; --card: #171d25; --ink: #e6eaef; --ink-soft: #a8b3bf;
  --ink-faint: #78828d; --rule: #2b333d; --chalk: #6fb2dd;
  --brass: #d3a24e; --oxide: #e2705f; --amber: #d9a441;
}
:root[data-theme="light"] {
  --paper: #e7eaee; --card: #f4f6f8; --ink: #12161b; --ink-soft: #4a5560;
  --ink-faint: #77828e; --rule: #c2c9d1; --chalk: #1e5f8c;
  --brass: #a8792f; --oxide: #9d3024; --amber: #96631a;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font-family: ui-monospace, "SF Mono", "Cascadia Mono", "JetBrains Mono",
               Consolas, "Liberation Mono", monospace;
  font-size: 14px; line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 60rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
.masthead {
  display: flex; flex-wrap: wrap; gap: .75rem 1.5rem;
  align-items: baseline; justify-content: space-between;
  border-bottom: 2px solid var(--ink); padding-bottom: .6rem;
}
.wordmark { font-size: 1.05rem; font-weight: 700; letter-spacing: .18em; text-transform: uppercase; }
.wordmark span { color: var(--brass); }
.run { color: var(--ink-faint); font-size: .8rem; letter-spacing: .04em; }

.verdict { margin: 2rem 0 .5rem; }
.verdict h1 {
  font-size: clamp(1.3rem, 3.4vw, 2rem); line-height: 1.25;
  margin: 0 0 .5rem; font-weight: 700; letter-spacing: -.01em;
}
.gate {
  display: inline-block; font-size: .72rem; letter-spacing: .16em;
  text-transform: uppercase; padding: .3rem .6rem; border: 1.5px solid currentColor;
}
.gate.blocks { color: var(--oxide); }
.gate.passes { color: var(--chalk); }

.tiers { display: flex; flex-wrap: wrap; gap: 1.75rem; margin: 1.75rem 0 2.5rem;
         padding-top: 1.25rem; border-top: 1px solid var(--rule); }
.tier .n { font-size: 1.65rem; font-weight: 700; line-height: 1; }
.tier .k { font-size: .72rem; letter-spacing: .13em; text-transform: uppercase; color: var(--ink-soft); }
.tier .why { font-size: .7rem; color: var(--ink-faint); }
.tier.is-zero .n, .tier.is-zero .k { color: var(--ink-faint); font-weight: 400; }

/* The plumb line. */
.line { position: relative; padding-left: 2.1rem; }
.line::before {
  content: ""; position: absolute; left: .42rem; top: .35rem; bottom: .35rem;
  border-left: 2px solid var(--chalk);
}
.line.silent::before { border-left-style: dashed; border-left-color: var(--ink-faint); }
.bob {
  position: absolute; left: 0; top: .42rem; width: .88rem; height: .88rem;
  border-radius: 50%; border: 2px solid var(--chalk); background: var(--chalk);
}
.line.silent .bob { background: transparent; border-color: var(--ink-faint); border-style: dashed; }
.line.err .bob, .line.err::before { border-color: var(--oxide); }
.line.err .bob { background: var(--oxide); }
.line.warn .bob, .line.warn::before { border-color: var(--amber); }
.line.warn .bob { background: var(--amber); }

.f { margin: 0 0 1.6rem; }
.f h3 { margin: 0 0 .3rem; font-size: .97rem; font-weight: 700; letter-spacing: -.005em; }
.f .meta {
  font-size: .72rem; letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-faint); margin-bottom: .45rem;
}
.f .meta b { color: var(--ink-soft); font-weight: 700; }
.f p { margin: 0 0 .5rem; color: var(--ink-soft); max-width: 62ch; }
/* The whole page is monospace, so identifiers do not need a border as well
   as a tint to read as code. One mark is enough. */
.f code { background: var(--card); padding: .1em .34em; border-radius: 2px; }
.f h3 code { background: transparent; padding: 0; text-decoration: underline;
             text-decoration-color: var(--rule); text-underline-offset: .22em; }
.urn { font-size: .68rem; color: var(--ink-faint); word-break: break-all; }
.suggest { color: var(--chalk); }

details { margin-top: .6rem; border: 1px solid var(--rule); background: var(--card); }
summary { cursor: pointer; padding: .45rem .7rem; font-size: .78rem; letter-spacing: .06em;
          text-transform: uppercase; color: var(--ink-soft); }
details pre { margin: 0; padding: .7rem; overflow-x: auto; font-size: .8rem;
              border-top: 1px solid var(--rule); }

.didnot { margin-top: 2.5rem; border: 1.5px dashed var(--ink-faint); padding: 1rem 1.15rem; }
.didnot h2 { margin: 0 0 .5rem; font-size: .76rem; letter-spacing: .15em;
             text-transform: uppercase; color: var(--ink-faint); }
.didnot li { color: var(--ink-soft); margin-bottom: .3rem; }
.didnot ul { margin: 0; padding-left: 1.1rem; }

.clean { border: 1.5px solid var(--chalk); padding: 1.25rem; color: var(--ink-soft); }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--rule);
         font-size: .74rem; color: var(--ink-faint); max-width: 70ch; }
@media (max-width: 34rem) { .tiers { gap: 1.1rem; } }
"""


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _inline(text: str) -> str:
    """Escape, then promote `backticked` spans to <code>."""
    out = _esc(text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", out)


def _tier_class(sev: Severity) -> str:
    return {
        Severity.ERROR: "err",
        Severity.WARN: "warn",
        Severity.UNKNOWN: "silent",
        Severity.INFO: "info",
    }[sev]


def render_html(report: Report, title: str = "Plumbline report") -> str:
    counts = report.counts
    blocking = report.blocking_count

    if blocking:
        headline = (
            f"{blocking} reference{'s' if blocking != 1 else ''} in this change "
            "cannot be resolved against the catalog."
        )
        gate = '<span class="gate blocks">Blocks merge</span>'
    elif report.findings:
        headline = "Nothing here blocks the merge. Some of it is worth a look."
        gate = '<span class="gate passes">Does not block</span>'
    else:
        headline = (
            f"Every table and column in {report.files_checked} "
            f"file{'s' if report.files_checked != 1 else ''} resolves against the catalog."
        )
        gate = '<span class="gate passes">Does not block</span>'

    tiers = []
    for sev in SEVERITY_ORDER:
        label, why = _TIER_COPY[sev]
        n = counts[sev.value]
        tiers.append(
            f'<div class="tier{" is-zero" if not n else ""}">'
            f'<div class="n">{n}</div>'
            f'<div class="k">{label}</div>'
            f'<div class="why">{_esc(why)}</div>'
            f"</div>"
        )

    body = []
    for sev in SEVERITY_ORDER:
        group = [f for f in report.findings if f.severity is sev]
        if not group:
            continue
        for f in _sorted(group):
            loc = _location(f)
            bits = [f'<b>{_esc(CHECK_LABEL.get(f.check, f.check.value))}</b>']
            if loc:
                bits.append(_esc(loc))
            parts = [
                f'<div class="line {_tier_class(sev)}">',
                '<span class="bob"></span>',
                '<div class="f">',
                f'<div class="meta">{" &middot; ".join(bits)}</div>',
                f"<h3>{_inline(f.summary)}</h3>",
                f"<p>{_inline(f.detail)}</p>",
            ]
            if f.suggestion:
                parts.append(
                    f'<p class="suggest">Closest real name: '
                    f"<code>{_esc(f.suggestion)}</code></p>"
                )
            if f.evidence_urn:
                parts.append(f'<p class="urn">{_esc(f.evidence_urn)}</p>')
            if f.fixed_sql:
                parts.append(
                    "<details><summary>Verified fix, re-checked against the "
                    f"catalog</summary><pre>{_esc(f.fixed_sql)}</pre></details>"
                )
            parts.append("</div></div>")
            body.append("".join(parts))

    if not report.findings:
        body.append(
            '<div class="clean">No table or column in this change is '
            "contradicted by the catalog.</div>"
        )

    didnot = ""
    if report.degraded:
        items = "".join(f"<li>{_inline(d)}</li>" for d in report.degraded)
        didnot = (
            '<section class="didnot"><h2>Checks that did not run</h2>'
            f"<ul>{items}</ul></section>"
        )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
</head><body><div class="wrap">
<header class="masthead">
  <div class="wordmark">Plumb<span>line</span></div>
  <div class="run">{report.files_checked} file{"s" if report.files_checked != 1 else ""} checked
    &middot; resolved against DataHub</div>
</header>
<section class="verdict">
  <h1>{_esc(headline)}</h1>
  {gate}
</section>
<section class="tiers">{"".join(tiers)}</section>
<main>{"".join(body)}</main>
{didnot}
<footer>An error is a reference the catalog contradicts. An unknown is one it
could not speak to, usually because the asset is not ingested; unknowns never
block. The line is solid where a reading was taken and dashed where it was
not.</footer>
</div></body></html>"""
