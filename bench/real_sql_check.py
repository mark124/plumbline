"""Run Plumbline over SQL that Plumbline did not write.

The generated benchmark measures whether the resolution logic is sound on SQL
built to be valid. It cannot tell us how the tool behaves on real code, which
is messier: dbt Jinja, LookML, PowerBI M expressions, dialect drift.

This script pulls every view definition the catalog holds and runs the checker
over it. These are the definitions of assets that actually exist, so a
blocking error here is a claim that a shipped asset is broken, and deserves to
be looked at by hand.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plumbline.catalog import DataHubCatalog  # noqa: E402
from plumbline.checks import run_all  # noqa: E402
from plumbline.findings import Report, Severity  # noqa: E402
from plumbline.parse import parse_sql  # noqa: E402

VIEWS = """query v($start: Int!) {
  searchAcrossEntities(input: {types: [DATASET], query: "*", start: $start, count: 50}) {
    total
    searchResults {
      entity {
        urn
        ... on Dataset {
          name
          platform { name }
          properties { qualifiedName }
          viewProperties { logic language }
        }
      }
    }
  }
}"""

JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def looks_like_sql(text: str) -> bool:
    head = text.lstrip()[:400].lower()
    if head.startswith(("let ", "row(", "#", "view:", "{")):
        return False
    return "select" in head or head.startswith("with")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8081"))
    ap.add_argument("--platform", default="snowflake")
    ap.add_argument("--platform-instance", default=None)
    ap.add_argument("--dialect", default="snowflake")
    ap.add_argument("--database", default=None)
    ap.add_argument("--schema", default=None)
    args = ap.parse_args()

    from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph

    graph = DataHubGraph(DatahubClientConfig(server=args.server, token=None))
    catalog = DataHubCatalog(
        graph, platform=args.platform, platform_instance=args.platform_instance
    )

    views, start = [], 0
    while True:
        sa = graph.execute_graphql(VIEWS, variables={"start": start})["searchAcrossEntities"]
        batch = sa["searchResults"]
        if not batch:
            break
        for x in batch:
            e = x["entity"]
            logic = (e.get("viewProperties") or {}).get("logic")
            if logic:
                views.append(
                    {
                        "urn": e["urn"],
                        "name": (e.get("properties") or {}).get("qualifiedName") or e.get("name"),
                        "platform": (e.get("platform") or {}).get("name"),
                        "logic": logic,
                    }
                )
        start += len(batch)
        if start >= sa["total"]:
            break

    print(f"datasets carrying view logic: {len(views)}\n")

    sql_views = [v for v in views if looks_like_sql(v["logic"])]
    skipped = [v for v in views if v not in sql_views]

    print(f"not SQL (LookML / PowerBI M / DAX), skipped: {len(skipped)}")
    for v in skipped:
        print(f"    {v['platform']:9s} {str(v['name'])[:70]}")

    print(f"\nSQL definitions to check: {len(sql_views)}\n")

    totals = {"error": 0, "warn": 0, "unknown": 0, "info": 0}
    parse_failures = 0
    jinja_count = 0

    for v in sql_views:
        logic = v["logic"]
        had_jinja = bool(JINJA.search(logic))
        if had_jinja:
            jinja_count += 1
        report = Report()
        report.files_checked = 1
        parsed = parse_sql(
            logic,
            catalog,
            dialect=args.dialect,
            default_db=args.database,
            default_schema=args.schema,
            file=str(v["name"]),
        )
        run_all(parsed, catalog, report)
        counts = report.counts
        for k in totals:
            totals[k] += counts[k]
        if parsed.parse_error:
            parse_failures += 1

        flag = " [contains dbt Jinja]" if had_jinja else ""
        print(f"--- {v['platform']} | {str(v['name'])[:66]}{flag}")
        print(
            f"    tables={len(parsed.tables)} resolved="
            f"{sum(1 for t in parsed.tables if t.exists)} "
            f"error={counts['error']} warn={counts['warn']} unknown={counts['unknown']}"
        )
        if parsed.parse_error:
            print(f"    PARSE FAILED: {parsed.parse_error[:120]}")
        for f in report.by_severity(Severity.ERROR):
            print(f"    ERROR: {f.summary} ({f.file}:{f.line})")
            print(f"           {f.detail[:160]}")

    print("\n=== totals over real view SQL ===")
    print(f"  definitions checked:   {len(sql_views)}")
    print(f"  containing dbt Jinja:  {jinja_count}")
    print(f"  parse failures:        {parse_failures}")
    print(f"  blocking errors:       {totals['error']}")
    print(f"  warnings:              {totals['warn']}")
    print(f"  unknowns:              {totals['unknown']}")


if __name__ == "__main__":
    main()
