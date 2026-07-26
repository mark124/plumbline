"""Run the benchmark against a live DataHub instance and print the results.

Usage:
    python bench/run_bench.py --server http://localhost:8081 \
        --platform snowflake --platform-instance b2fd91

Two numbers come out of this:

  Precision  On SQL that is valid by construction, how often does Plumbline
             raise a blocking error? Every such error is a false positive.
  Recall     On SQL with exactly one injected defect, how often does it catch
             the defect that was injected?

Precision is the number that decides whether a tool like this survives contact
with a real team, so it is reported first.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from generate import build, tables_from_catalog_dump  # noqa: E402

from plumbline.catalog import DataHubCatalog  # noqa: E402
from plumbline.checks import run_all  # noqa: E402
from plumbline.findings import Report, Severity  # noqa: E402
from plumbline.parse import parse_sql  # noqa: E402


def fetch_tables(graph, platform):
    """Pull every dataset with a schema straight from the catalog."""
    LIST = """query d($start: Int!) {
      searchAcrossEntities(input: {types: [DATASET], query: "*", start: $start, count: 50}) {
        total searchResults { entity { urn ... on Dataset { name platform { name } } } }
      }
    }"""
    DETAIL = """query s($urn: String!) {
      dataset(urn: $urn) {
        urn name platform { name }
        properties { qualifiedName }
        schemaMetadata { fields { fieldPath } }
      }
    }"""
    urns, start = [], 0
    while True:
        sa = graph.execute_graphql(LIST, variables={"start": start})["searchAcrossEntities"]
        batch = sa["searchResults"]
        if not batch:
            break
        urns += [x["entity"]["urn"] for x in batch]
        start += len(batch)
        if start >= sa["total"]:
            break

    records = []
    for urn in urns:
        d = graph.execute_graphql(DETAIL, variables={"urn": urn}).get("dataset")
        if not d:
            continue
        records.append(
            {
                "platform": (d.get("platform") or {}).get("name"),
                "name": d.get("name"),
                "qualifiedName": (d.get("properties") or {}).get("qualifiedName"),
                "columns": [
                    {"path": f["fieldPath"]}
                    for f in ((d.get("schemaMetadata") or {}).get("fields") or [])
                ],
            }
        )
    return tables_from_catalog_dump(records, platform=platform)


def check_one(sql, catalog, dialect):
    report = Report()
    report.files_checked = 1
    parsed = parse_sql(sql, catalog, dialect=dialect, file="bench.sql")
    run_all(parsed, catalog, report)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8081"))
    ap.add_argument("--token", default=os.environ.get("DATAHUB_GMS_TOKEN"))
    ap.add_argument("--platform", default="snowflake")
    ap.add_argument("--platform-instance", default=None)
    ap.add_argument("--env", default="PROD")
    ap.add_argument("--dialect", default="snowflake")
    ap.add_argument("--per-template", type=int, default=6)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph

    graph = DataHubGraph(DatahubClientConfig(server=args.server, token=args.token))
    catalog = DataHubCatalog(
        graph,
        platform=args.platform,
        env=args.env,
        platform_instance=args.platform_instance,
    )

    tables = fetch_tables(graph, args.platform)
    print(f"catalog: {len(tables)} {args.platform} tables with schemas, "
          f"{sum(len(t.columns) for t in tables)} columns")

    valid, defective = build(tables, per_template=args.per_template)
    print(f"generated: {len(valid)} valid cases, {len(defective)} defective cases\n")

    # -- precision ------------------------------------------------------
    false_positives = []
    for case in valid:
        report = check_one(case.sql, catalog, args.dialect)
        errors = report.by_severity(Severity.ERROR)
        if errors:
            false_positives.append((case, errors))

    fp_rate = len(false_positives) / len(valid) if valid else 0.0
    print("=== Precision: valid SQL that was wrongly blocked ===")
    print(f"  valid cases:          {len(valid)}")
    print(f"  cases with an ERROR:  {len(false_positives)}")
    print(f"  false positive rate:  {fp_rate:.1%}")
    for case, errors in false_positives[:10]:
        print(f"    - {case.case_id} ({case.template}): {errors[0].summary}")
        print(f"      {case.sql.splitlines()[0][:100]}")

    # -- recall ---------------------------------------------------------
    caught = 0
    missed = []
    by_kind = collections.Counter()
    kind_total = collections.Counter()
    for case in defective:
        kind_total[case.defect_kind] += 1
        report = check_one(case.sql, catalog, args.dialect)
        hit = any(
            f.severity is Severity.ERROR
            and f.check.value == case.expected_check
            and (f.subject or "").lower() == (case.defect_token or "").lower()
            for f in report.findings
        )
        if hit:
            caught += 1
            by_kind[case.defect_kind] += 1
        else:
            missed.append((case, report))

    recall = caught / len(defective) if defective else 0.0
    print("\n=== Recall: injected defects that were caught as blocking errors ===")
    print(f"  defective cases:      {len(defective)}")
    print(f"  caught:               {caught}")
    print(f"  recall:               {recall:.1%}")
    for kind in sorted(kind_total):
        n, d = by_kind[kind], kind_total[kind]
        print(f"    {kind:18s} {n}/{d}  ({n / d:.0%})")

    if missed:
        print(f"\n  missed ({len(missed)}), first 10:")
        for case, report in missed[:10]:
            sevs = ",".join(sorted({f.severity.value for f in report.findings})) or "none"
            print(f"    - {case.case_id}: broke `{case.defect_token}`, got [{sevs}]")

    results = {
        "catalog": {
            "tables": len(tables),
            "columns": sum(len(t.columns) for t in tables),
            "platform": args.platform,
        },
        "precision": {
            "valid_cases": len(valid),
            "false_positives": len(false_positives),
            "false_positive_rate": fp_rate,
            "examples": [
                {"case": c.case_id, "summary": e[0].summary, "sql": c.sql}
                for c, e in false_positives[:20]
            ],
        },
        "recall": {
            "defective_cases": len(defective),
            "caught": caught,
            "recall": recall,
            "by_kind": {k: {"caught": by_kind[k], "total": kind_total[k]} for k in kind_total},
            "missed": [
                {"case": c.case_id, "token": c.defect_token, "sql": c.sql}
                for c, _ in missed[:20]
            ],
        },
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
