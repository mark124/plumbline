"""Tests for the check families.

Most of these assert on *severity*, not just on whether something was found.
Getting the finding right and the confidence wrong is still getting it wrong.
"""

from __future__ import annotations

import pytest

from plumbline.catalog import Downstream
from plumbline.checks import run_all
from plumbline.findings import Check, Report, Severity
from plumbline.parse import parse_sql

from .fakes import FakeCatalog, _urn

ORDERS = {
    "order_id": "NUMBER",
    "customer_id": "NUMBER",
    "order_total": "NUMBER",
    "created_at": "TIMESTAMP",
}
CUSTOMERS = {"customer_id": "NUMBER", "email": "VARCHAR", "country": "VARCHAR"}


def build(**kwargs) -> FakeCatalog:
    base = {
        "analytics.public.orders": ORDERS,
        "analytics.public.customers": CUSTOMERS,
    }
    base.update(kwargs.pop("extra_tables", {}))
    return FakeCatalog(tables=base, **kwargs)


def run(sql: str, catalog: FakeCatalog) -> Report:
    report = Report()
    parsed = parse_sql(
        sql,
        catalog,
        dialect="snowflake",
        default_db="analytics",
        default_schema="public",
        file="model.sql",
    )
    run_all(parsed, catalog, report)
    return report


def findings_of(report: Report, check: Check):
    return [f for f in report.findings if f.check is check]


def test_clean_sql_produces_no_blocking_findings():
    report = run(
        "SELECT order_id, order_total FROM analytics.public.orders", build()
    )
    assert report.blocking_count == 0
    assert report.exit_code == 0


def test_phantom_column_is_an_error_with_suggestion():
    report = run(
        "SELECT order_id, order_ttl FROM analytics.public.orders", build()
    )
    f = findings_of(report, Check.PHANTOM_COLUMN)
    assert len(f) == 1
    assert f[0].severity is Severity.ERROR
    assert f[0].suggestion == "order_total"
    assert report.exit_code == 1


def test_unknown_table_is_unknown_not_error():
    """The core honesty rule.

    `shipments` is nothing like any table we hold, so we must not claim it is
    wrong. An uningested table looks exactly like this.
    """
    report = run("SELECT a FROM analytics.public.shipments", build())
    f = findings_of(report, Check.PHANTOM_TABLE)
    assert len(f) == 1
    assert f[0].severity is Severity.UNKNOWN
    assert report.exit_code == 0, "an unknown table must not fail the build"


def test_near_miss_table_is_promoted_to_error():
    """`custmers` is one letter from `customers`, which is real evidence."""
    report = run("SELECT customer_id FROM analytics.public.custmers", build())
    f = findings_of(report, Check.PHANTOM_TABLE)
    assert len(f) == 1
    assert f[0].severity is Severity.ERROR
    assert f[0].suggestion == "customers"


def test_unknown_table_does_not_emit_column_findings():
    report = run("SELECT a, b, c FROM analytics.public.shipments", build())
    assert findings_of(report, Check.PHANTOM_COLUMN) == []


def test_created_table_is_not_reported_as_phantom():
    sql = """
    CREATE TABLE analytics.public.brand_new AS
    SELECT order_id FROM analytics.public.orders
    """
    report = run(sql, build())
    assert findings_of(report, Check.PHANTOM_TABLE) == []


def test_deprecated_source_is_a_warning():
    catalog = build(deprecated={"analytics.public.orders"})
    report = run("SELECT order_id FROM analytics.public.orders", catalog)
    f = findings_of(report, Check.DEPRECATED_SOURCE)
    assert len(f) == 1
    assert f[0].severity is Severity.WARN
    assert report.exit_code == 0


def test_pii_column_into_untagged_output_warns():
    catalog = build(pii_columns={"analytics.public.customers": {"email"}})
    sql = """
    CREATE TABLE analytics.public.customer_export AS
    SELECT c.customer_id, c.email FROM analytics.public.customers c
    """
    report = run(sql, catalog)
    f = findings_of(report, Check.PII_PROPAGATION)
    assert len(f) == 1
    assert f[0].severity is Severity.WARN
    assert f[0].subject == "email"


def test_pii_not_flagged_when_not_written_anywhere():
    catalog = build(pii_columns={"analytics.public.customers": {"email"}})
    report = run("SELECT email FROM analytics.public.customers", catalog)
    assert findings_of(report, Check.PII_PROPAGATION) == []


def test_join_seen_in_query_history_is_not_flagged():
    catalog = build(
        queries={
            _urn("snowflake", "analytics.public.orders"): [
                "SELECT 1 FROM analytics.public.orders o "
                "JOIN analytics.public.customers c ON o.customer_id = c.customer_id"
            ]
        }
    )
    sql = """
    SELECT o.order_id FROM analytics.public.orders o
    JOIN analytics.public.customers c ON o.customer_id = c.customer_id
    """
    report = run(sql, catalog)
    assert findings_of(report, Check.UNVETTED_JOIN) == []


def test_novel_join_is_flagged_as_warning():
    catalog = build(
        queries={
            _urn("snowflake", "analytics.public.orders"): [
                "SELECT 1 FROM analytics.public.orders o "
                "JOIN analytics.public.customers c ON o.customer_id = c.customer_id"
            ]
        }
    )
    sql = """
    SELECT o.order_id FROM analytics.public.orders o
    JOIN analytics.public.customers c ON o.order_total = c.country
    """
    report = run(sql, catalog)
    f = findings_of(report, Check.UNVETTED_JOIN)
    assert len(f) == 1
    assert f[0].severity is Severity.WARN


def test_no_query_history_degrades_instead_of_passing():
    """A check that cannot run must say so."""
    catalog = build(has_query_history=False)
    sql = """
    SELECT o.order_id FROM analytics.public.orders o
    JOIN analytics.public.customers c ON o.customer_id = c.customer_id
    """
    report = run(sql, catalog)
    assert findings_of(report, Check.UNVETTED_JOIN) == []
    assert any("did not run" in d for d in report.degraded)


def test_blast_radius_lists_downstream_consumers():
    urn = _urn("snowflake", "analytics.public.orders")
    catalog = build(
        downstreams={
            urn: [
                Downstream(urn="urn:li:dashboard:(looker,exec_kpis)", name="exec_kpis", entity_type="DASHBOARD"),
                Downstream(urn="urn:li:dataset:(x,finance.revenue,PROD)", name="revenue", entity_type="DATASET"),
            ]
        }
    )
    sql = """
    CREATE TABLE analytics.public.orders AS
    SELECT order_id FROM analytics.public.customers
    """
    report = run(sql, catalog)
    f = findings_of(report, Check.BLAST_RADIUS)
    assert len(f) == 1
    assert f[0].severity is Severity.INFO
    assert "2 downstream consumers" in f[0].summary


def test_parse_failure_is_reported_and_not_silent():
    report = run("SELECT FROM WHERE ((", build())
    f = findings_of(report, Check.PARSE_FAILURE)
    assert len(f) == 1
    assert f[0].severity is Severity.UNKNOWN
    assert "not evidence that the code is correct" in f[0].detail
