"""The check families.

Each check turns facts from the catalog into findings. The rule every check
follows: severity reflects the strength of the evidence, not how alarming the
problem would be if it were real.

A missing column in a table we have the schema for is an ERROR, because the
catalog can prove it. A table we have never heard of is an UNKNOWN, because
the catalog cannot tell the difference between a hallucinated name and a
table nobody ingested. Promoting the second to an error is how catalog tools
lose their users' trust in the first week.
"""

from __future__ import annotations

import difflib
from typing import List, Optional, Sequence

from .catalog import Catalog
from .findings import Check, Finding, Report, Severity
from .parse import ParsedSql, TableRef

# How close two identifiers must be before we call one a likely typo of the
# other. 0.82 keeps `order_ttl` -> `order_total` and rejects unrelated pairs
# like `country` -> `customer_id`.
NEAR_MISS_RATIO = 0.82


def _closest(name: str, candidates: Sequence[str]) -> Optional[str]:
    """Return the nearest candidate to `name`, or None if nothing is close."""
    if not candidates:
        return None
    matches = difflib.get_close_matches(
        name.lower(), [c.lower() for c in candidates], n=1, cutoff=NEAR_MISS_RATIO
    )
    if not matches:
        return None
    # Map back to the candidate's original spelling.
    for c in candidates:
        if c.lower() == matches[0]:
            return c
    return matches[0]


def check_phantom_columns(parsed: ParsedSql, report: Report) -> None:
    """Columns referenced on a table whose schema we have, that are not in it.

    This is the only check that can be certain, and it is the one the
    benchmark measures.
    """
    for use in parsed.phantom_columns:
        ref = use.table_ref
        searched = use.searched
        real_columns = sorted(
            {c.name for t in searched for c in t.schema.columns.values()}
        )
        suggestion = _closest(use.column, real_columns)

        if len(searched) == 1:
            where = f"`{searched[0].raw}`"
        else:
            where = "any of " + ", ".join(f"`{t.raw}`" for t in searched)

        detail = (
            f"The catalog has a schema for {where} and it contains no column "
            f"named `{use.column}`."
        )
        if suggestion:
            detail += f" The closest real column is `{suggestion}`."
        else:
            shown = real_columns[:12]
            detail += " Real columns: " + ", ".join(f"`{c}`" for c in shown)
            if len(real_columns) > len(shown):
                detail += f", and {len(real_columns) - len(shown)} more"
            detail += "."

        report.add(
            Finding(
                check=Check.PHANTOM_COLUMN,
                severity=Severity.ERROR,
                summary=f"Column `{use.column}` does not exist",
                detail=detail,
                file=parsed.file,
                line=use.line,
                subject=use.column,
                evidence_urn=ref.urn,
                suggestion=suggestion,
            )
        )

    for derived in parsed.derived_phantoms:
        suggestion = _closest(derived.column, derived.available)
        detail = (
            f"`{derived.source_alias}` is defined in this query and returns "
            + ", ".join(f"`{c}`" for c in derived.available[:12])
            + f". It does not return `{derived.column}`."
        )
        if suggestion:
            detail += f" The closest name it does return is `{suggestion}`."
        report.add(
            Finding(
                check=Check.PHANTOM_COLUMN,
                severity=Severity.ERROR,
                summary=f"Column `{derived.column}` does not exist",
                detail=detail,
                file=parsed.file,
                line=derived.line,
                subject=derived.column,
                suggestion=suggestion,
            )
        )


def check_phantom_tables(
    parsed: ParsedSql, catalog: Catalog, report: Report
) -> None:
    """Tables that the catalog has never heard of.

    Reported as UNKNOWN by default. It is promoted to ERROR only when the
    catalog contains a name close enough to be the one that was meant, which
    is real evidence of a typo rather than an ingestion gap.
    """
    written = {t.urn for t in parsed.out_tables}

    for ref in parsed.tables:
        if ref.exists:
            continue
        # A table this statement creates is supposed to be new.
        if ref.urn in written:
            continue

        candidates = []
        try:
            candidates = catalog.find_similar_tables(
                ref.table, database=ref.database, db_schema=ref.db_schema
            )
        except Exception:  # noqa: BLE001
            candidates = []

        names = [name for name, _urn in candidates]
        suggestion = _closest(ref.table, names)
        suggestion_urn = None
        if suggestion:
            for name, urn in candidates:
                if name == suggestion:
                    suggestion_urn = urn
                    break

        if suggestion:
            report.add(
                Finding(
                    check=Check.PHANTOM_TABLE,
                    severity=Severity.ERROR,
                    summary=f"Table `{ref.raw}` does not exist",
                    detail=(
                        f"No dataset named `{ref.table}` is in the catalog, but "
                        f"`{suggestion}` is, and the names differ by very little. "
                        "This reads as a typo rather than an uningested table."
                    ),
                    file=parsed.file,
                    line=ref.line,
                    subject=ref.table,
                    evidence_urn=suggestion_urn,
                    suggestion=suggestion,
                )
            )
        else:
            report.add(
                Finding(
                    check=Check.PHANTOM_TABLE,
                    severity=Severity.UNKNOWN,
                    summary=f"Table `{ref.raw}` is not in the catalog",
                    detail=(
                        f"No dataset matching `{ref.raw}` was found, and nothing "
                        "in the catalog has a similar name. This is reported as "
                        "unknown, not as an error: the table may simply not be "
                        "ingested. Its columns were not checked."
                    ),
                    file=parsed.file,
                    line=ref.line,
                    subject=ref.table,
                )
            )


def check_deprecated_sources(parsed: ParsedSql, report: Report) -> None:
    """Reading from an asset the organisation has marked deprecated."""
    written = {t.urn for t in parsed.out_tables}
    for ref in parsed.tables:
        if not ref.exists or not ref.schema.deprecated:
            continue
        if ref.urn in written:
            continue
        report.add(
            Finding(
                check=Check.DEPRECATED_SOURCE,
                severity=Severity.WARN,
                summary=f"`{ref.raw}` is marked deprecated",
                detail=(
                    f"The catalog marks `{ref.raw}` as deprecated. New code "
                    "should not take a dependency on it. Check the asset's "
                    "documentation for the intended replacement."
                ),
                file=parsed.file,
                line=ref.line,
                subject=ref.table,
                evidence_urn=ref.urn,
            )
        )


def check_pii(parsed: ParsedSql, report: Report) -> None:
    """PII-tagged columns being read into a new output."""
    if not parsed.out_tables:
        return

    target_is_marked = any(
        any("pii" in t.lower() for t in ref.schema.tags) for ref in parsed.out_tables
    )
    if target_is_marked:
        return

    seen = set()
    for use in parsed.column_uses:
        ref = use.table_ref
        if not ref.exists:
            continue
        col = ref.schema.column(use.column)
        if col is None or not col.is_pii:
            continue
        key = (ref.urn, col.name.lower())
        if key in seen:
            continue
        seen.add(key)

        target = parsed.out_tables[0]
        report.add(
            Finding(
                check=Check.PII_PROPAGATION,
                severity=Severity.WARN,
                summary=f"PII column `{col.name}` flows into `{target.raw}`",
                detail=(
                    f"`{ref.raw}`.`{col.name}` is tagged "
                    + ", ".join(sorted(col.tags | col.terms))
                    + f" in the catalog, and this statement writes it into "
                    f"`{target.raw}`, which carries no such tag. Either tag the "
                    "output, mask the column, or drop it from the select list."
                ),
                file=parsed.file,
                line=use.line,
                subject=col.name,
                evidence_urn=ref.urn,
            )
        )


def check_joins(parsed: ParsedSql, catalog: Catalog, report: Report) -> None:
    """Joins on key pairs that appear in no observed production query.

    This is a heuristic and is always a WARN. A novel join is not a wrong
    join; it is a join no one has made before, which is worth a human glance
    when a model wrote it. If the catalog has no query history at all the
    check cannot run, and the report says so rather than passing silently.
    """
    if not parsed.joins:
        return

    if not catalog.supports_query_history():
        report.degrade(
            "No query history in this catalog, so the unvetted-join check did "
            "not run. Joins were not validated."
        )
        return

    observed = set()
    checked_any = False
    for ref in parsed.tables:
        if not ref.exists:
            continue
        statements = catalog.get_queries(ref.urn)
        if statements:
            checked_any = True
        for stmt in statements:
            for pair in _join_pairs(stmt, parsed.dialect):
                observed.add(pair)
                observed.add((pair[1], pair[0]))

    if not checked_any:
        report.degrade(
            "None of the tables in this statement have query history, so the "
            "unvetted-join check did not run for it."
        )
        return

    for left, right in parsed.joins:
        if (left, right) in observed or (right, left) in observed:
            continue
        report.add(
            Finding(
                check=Check.UNVETTED_JOIN,
                severity=Severity.WARN,
                summary=f"Join on `{left}` = `{right}` is not seen in query history",
                detail=(
                    f"No query recorded against these tables joins `{left}` to "
                    f"`{right}`. That does not make it wrong, but it is a join "
                    "pattern nobody in the organisation has used, which is worth "
                    "confirming when the code was machine-written. Check the "
                    "cardinality before merging."
                ),
                file=parsed.file,
                line=None,
                subject=f"{left}={right}",
            )
        )


def check_blast_radius(parsed: ParsedSql, catalog: Catalog, report: Report) -> None:
    """Who depends on the thing this statement rewrites."""
    for ref in parsed.out_tables:
        if not ref.exists:
            continue
        downstreams = catalog.get_downstreams(ref.urn)
        if not downstreams:
            continue

        by_type = {}
        for d in downstreams:
            by_type.setdefault(d.entity_type, []).append(d)
        parts = ", ".join(
            f"{len(v)} {k.lower()}{'s' if len(v) != 1 else ''}"
            for k, v in sorted(by_type.items())
        )
        named = ", ".join(f"`{d.name}`" for d in downstreams[:5] if d.name)
        detail = f"`{ref.raw}` has {len(downstreams)} downstream consumers ({parts})."
        if named:
            detail += f" Including: {named}."
        detail += " Changing its schema affects all of them."

        report.add(
            Finding(
                check=Check.BLAST_RADIUS,
                severity=Severity.INFO,
                summary=(
                    f"`{ref.raw}` has {len(downstreams)} downstream "
                    f"consumer{'s' if len(downstreams) != 1 else ''}"
                ),
                detail=detail,
                file=parsed.file,
                line=ref.line,
                subject=ref.table,
                evidence_urn=ref.urn,
            )
        )


def _join_pairs(sql: str, dialect: str) -> List:
    from .parse import _extract_joins

    return _extract_joins(sql, dialect)


ALL_CHECKS = (
    "phantom_column",
    "phantom_table",
    "deprecated_source",
    "pii_propagation",
    "unvetted_join",
    "blast_radius",
)


def run_all(
    parsed: ParsedSql,
    catalog: Catalog,
    report: Report,
    *,
    enabled: Sequence[str] = ALL_CHECKS,
) -> None:
    """Run every enabled check against one parsed statement."""
    if parsed.parse_error:
        report.add(
            Finding(
                check=Check.PARSE_FAILURE,
                severity=Severity.UNKNOWN,
                summary="SQL could not be parsed",
                detail=(
                    f"{parsed.parse_error}. Nothing in this file was checked. "
                    "A parse failure is not evidence that the code is correct."
                ),
                file=parsed.file,
            )
        )
        return

    for reason in parsed.degraded:
        report.degrade(reason)

    if "phantom_column" in enabled:
        check_phantom_columns(parsed, report)
    if "phantom_table" in enabled:
        check_phantom_tables(parsed, catalog, report)
    if "deprecated_source" in enabled:
        check_deprecated_sources(parsed, report)
    if "pii_propagation" in enabled:
        check_pii(parsed, report)
    if "unvetted_join" in enabled:
        check_joins(parsed, catalog, report)
    if "blast_radius" in enabled:
        check_blast_radius(parsed, catalog, report)
