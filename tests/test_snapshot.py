"""Tests for the bundled catalog snapshot.

The snapshot exists to solve a first-impression problem: pointed at a sparse
DataHub, every table comes back Unknown and the tool looks like it does
nothing. That is the tool being honest and still the wrong first experience.

So these tests care about two things. That the snapshot behaves like a real
catalog through the same protocol, and that it is never mistaken for one.
"""

from __future__ import annotations

import json
import os

import pytest

from plumbline.checks import run_all
from plumbline.findings import Report, Severity
from plumbline.parse import parse_sql
from plumbline.snapshot import DEFAULT_SNAPSHOT, SnapshotCatalog

pytestmark = pytest.mark.skipif(
    not os.path.exists(DEFAULT_SNAPSHOT),
    reason="bundled snapshot not present",
)


@pytest.fixture(scope="module")
def catalog():
    return SnapshotCatalog.load()


def _check(sql, catalog):
    report = Report(files_checked=1)
    parsed = parse_sql(sql, catalog, dialect="snowflake", file="x.sql")
    run_all(parsed, catalog, report)
    return report


def test_the_snapshot_ships_with_the_repository():
    """The whole point is that `--demo` works from a clean clone."""
    assert os.path.exists(DEFAULT_SNAPSHOT)
    with open(DEFAULT_SNAPSHOT, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["tables"], "an empty snapshot would make every table Unknown"


def test_it_says_what_it_is(catalog):
    """Never presented as a live catalog."""
    described = catalog.describe()
    assert "snapshot" in described
    assert "datasets" in described


def test_a_real_column_resolves(catalog):
    schema = catalog.resolve_table(
        database="ORDER_ENTRY_DB", db_schema="ORDER_ENTRY", table="CUSTOMERS"
    )
    assert schema.exists
    assert schema.has_column("credit_limit")


def test_case_does_not_matter(catalog):
    lower = catalog.resolve_table(
        database="order_entry_db", db_schema="order_entry", table="customers"
    )
    assert lower.exists


def test_a_table_outside_the_snapshot_is_absent_not_invented(catalog):
    schema = catalog.resolve_table(
        database="ORDER_ENTRY_DB", db_schema="ORDER_ENTRY", table="NOT_A_TABLE"
    )
    assert not schema.exists
    assert schema.urn, "an absent table still needs a URN to report against"


def test_a_phantom_column_is_caught_through_the_snapshot(catalog):
    report = _check(
        "SELECT credit_limt FROM ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS", catalog
    )
    errors = report.by_severity(Severity.ERROR)
    assert [f.subject for f in errors] == ["credit_limt"]
    assert errors[0].suggestion == "credit_limit"


def test_a_valid_query_is_still_silent(catalog):
    """The demo must not cry wolf either, or it teaches the wrong lesson."""
    report = _check(
        "SELECT credit_limit, cust_email FROM ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS",
        catalog,
    )
    assert report.by_severity(Severity.ERROR) == []


def test_an_uningested_table_is_unknown_and_does_not_block(catalog):
    """The single most important behaviour to preserve in the demo path."""
    report = _check("SELECT a FROM ORDER_ENTRY_DB.ORDER_ENTRY.SHIPMENTS", catalog)
    assert report.by_severity(Severity.ERROR) == []
    assert report.exit_code == 0
    assert report.by_severity(Severity.UNKNOWN)


def test_column_tags_survive_the_freeze(catalog):
    """PII propagation cannot be demonstrated without column-level tags."""
    schema = catalog.resolve_table(
        database="ORDER_ENTRY_DB", db_schema="ORDER_ENTRY", table="CUSTOMERS"
    )
    assert schema.pii_columns, "no PII-tagged column survived the export"


def test_query_history_survives_the_freeze(catalog):
    """Without it the join check degrades and the demo silently loses a check."""
    assert catalog.supports_query_history()


def test_lineage_survives_the_freeze(catalog):
    """Blast radius is the check that needs downstream edges."""
    schema = catalog.resolve_table(
        database="ORDER_ENTRY_DB", db_schema="ANALYTICS", table="ORDER_DETAILS"
    )
    assert catalog.get_downstreams(schema.urn)


def test_similar_table_lookup_is_ordered(catalog):
    """The near-miss suggestion must not wobble between runs."""
    a = catalog.find_similar_tables("CUSTOMERS", limit=0)
    b = catalog.find_similar_tables("CUSTOMERS", limit=0)
    assert a == b
