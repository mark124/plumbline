"""Seed the two conditions the showcase datapack does not contain.

`datahub datapack load showcase-ecommerce` gives you 67 datasets, real
lineage, and column-level PII tags. It contains no deprecated assets and no
query history, so two of the six checks have nothing to fire on and cannot be
demonstrated or reproduced without this.

This script adds exactly two things and is safe to re-run:

1. Marks ORDER_DETAILS_REPLICA deprecated, so `deprecated_source` has a
   genuine deprecation to find.
2. Emits one Query entity recording a join that was actually run, so
   `unvetted_join` can tell a seen join from a novel one. Without any query
   history the check correctly refuses to run and says so, which is honest but
   makes it invisible.

Usage:
    python demo/seed_demo_catalog.py --server http://localhost:8080
"""

from __future__ import annotations

import argparse
import sys

PLATFORM = "snowflake"
INSTANCE = "b2fd91"


def dataset_urn(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{PLATFORM},{INSTANCE}.{name},PROD)"


REPLICA = dataset_urn("order_entry_db.analytics.order_details_replica")
ORDERS = dataset_urn("order_entry_db.order_entry.orders")

OBSERVED_SQL = """SELECT o.order_id, c.cust_email
FROM ORDER_ENTRY_DB.ORDER_ENTRY.ORDERS o
JOIN ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS c
  ON o.customer_id = c.customer_id"""

DEPRECATE = """
mutation deprecate($urn: String!, $note: String!) {
  updateDeprecation(input: {urn: $urn, deprecated: true, note: $note})
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:8080")
    ap.add_argument("--token", default=None)
    args = ap.parse_args()

    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
    from datahub.metadata.schema_classes import (
        AuditStampClass,
        QueryLanguageClass,
        QueryPropertiesClass,
        QuerySourceClass,
        QueryStatementClass,
        QuerySubjectClass,
        QuerySubjectsClass,
    )

    graph = DataHubGraph(DatahubClientConfig(server=args.server, token=args.token))

    print(f"connected to {args.server}")

    # 1. deprecation
    try:
        graph.execute_graphql(
            DEPRECATE,
            variables={
                "urn": REPLICA,
                "note": "Superseded by ANALYTICS.ORDER_DETAILS. Do not build on this.",
            },
        )
        print("  marked ORDER_DETAILS_REPLICA deprecated")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not mark deprecated: {exc}")
        print("  (is the showcase-ecommerce datapack loaded?)")
        return 1

    # 2. one observed query, so the join check has a baseline
    stamp = AuditStampClass(time=1785000000000, actor="urn:li:corpuser:datahub")
    query_urn = "urn:li:query:plumbline-demo-observed-join"
    aspects = [
        QueryPropertiesClass(
            statement=QueryStatementClass(
                value=OBSERVED_SQL, language=QueryLanguageClass.SQL
            ),
            source=QuerySourceClass.SYSTEM,
            created=stamp,
            lastModified=stamp,
        ),
        QuerySubjectsClass(
            subjects=[QuerySubjectClass(entity=ORDERS), QuerySubjectClass(entity=REPLICA)]
        ),
    ]
    for aspect in aspects:
        graph.emit(MetadataChangeProposalWrapper(entityUrn=query_urn, aspect=aspect))
    print("  emitted one observed query joining ORDERS to CUSTOMERS on customer_id")

    print(
        "\nDone. Indexing takes a few seconds; if the join check still reports\n"
        "that it did not run, wait and try again."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
