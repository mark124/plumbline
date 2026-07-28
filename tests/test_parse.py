"""Tests for reference extraction.

The bar for these tests is not "does it find the bug" but "does it stay quiet
when it should". A checker that flags correct code is worse than no checker,
because people turn it off.
"""

from __future__ import annotations

import pytest

from plumbline.parse import parse_sql

from .fakes import FakeCatalog

ORDERS = {
    "order_id": "NUMBER",
    "customer_id": "NUMBER",
    "order_total": "NUMBER",
    "created_at": "TIMESTAMP",
}
CUSTOMERS = {
    "customer_id": "NUMBER",
    "email": "VARCHAR",
    "country": "VARCHAR",
}


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog(
        tables={
            "analytics.public.orders": ORDERS,
            "analytics.public.customers": CUSTOMERS,
        }
    )


def _parse(sql: str, catalog: FakeCatalog):
    return parse_sql(
        sql,
        catalog,
        dialect="snowflake",
        default_db="analytics",
        default_schema="public",
    )


def test_resolves_real_tables(catalog):
    r = _parse("SELECT order_id FROM analytics.public.orders", catalog)
    assert r.ok
    assert len(r.tables) == 1
    assert r.tables[0].exists
    assert r.phantom_columns == []


def test_detects_phantom_column(catalog):
    r = _parse("SELECT order_id, order_ttl FROM analytics.public.orders", catalog)
    assert r.ok
    names = [c.column for c in r.phantom_columns]
    assert names == ["order_ttl"]


def test_detects_phantom_table(catalog):
    r = _parse("SELECT id FROM analytics.public.ordrs", catalog)
    assert r.ok
    assert len(r.tables) == 1
    assert not r.tables[0].exists


def test_missing_table_does_not_produce_phantom_columns(catalog):
    """The central honesty guarantee.

    An uningested table must yield exactly one finding (the table), never one
    finding per column. Otherwise a catalog with partial coverage buries the
    user in noise.
    """
    r = _parse("SELECT a, b, c, d FROM analytics.public.not_ingested", catalog)
    assert r.ok
    assert not r.tables[0].exists
    assert r.phantom_columns == []


def test_cte_columns_are_not_catalog_columns(catalog):
    sql = """
    WITH recent AS (
        SELECT order_id, order_total AS revenue
        FROM analytics.public.orders
    )
    SELECT order_id, revenue FROM recent
    """
    r = _parse(sql, catalog)
    assert r.ok
    # `revenue` exists only in the CTE. It must not be reported against orders.
    assert r.phantom_columns == []
    assert "recent" not in [t.table.lower() for t in r.tables]


def test_subquery_alias_columns_are_not_flagged(catalog):
    sql = """
    SELECT s.total_spend
    FROM (
        SELECT customer_id, SUM(order_total) AS total_spend
        FROM analytics.public.orders
        GROUP BY customer_id
    ) AS s
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert r.phantom_columns == []


def test_join_across_two_real_tables(catalog):
    sql = """
    SELECT o.order_id, c.email
    FROM analytics.public.orders o
    JOIN analytics.public.customers c ON o.customer_id = c.customer_id
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert r.phantom_columns == []
    assert ("customer_id", "customer_id") in r.joins


def test_phantom_column_in_join_condition(catalog):
    sql = """
    SELECT o.order_id
    FROM analytics.public.orders o
    JOIN analytics.public.customers c ON o.cust_id = c.customer_id
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert [c.column for c in r.phantom_columns] == ["cust_id"]


def test_select_star_is_not_a_phantom(catalog):
    r = _parse("SELECT * FROM analytics.public.orders", catalog)
    assert r.ok
    assert r.phantom_columns == []


def test_qualified_star_is_not_a_phantom(catalog):
    """Regression: `o.*` parses as a column literally named `*`.

    Found by red-teaming with valid-but-awkward SQL. Plain `SELECT *` was
    handled and the qualified form was not, so ordinary queries raised a
    blocking error.
    """
    r = _parse("SELECT o.* FROM analytics.public.orders o", catalog)
    assert r.ok
    assert r.phantom_columns == []


def test_qualified_star_alongside_a_real_phantom(catalog):
    """The star must be ignored without suppressing a genuine defect."""
    sql = """
    SELECT o.*, o.order_ttl
    FROM analytics.public.orders o
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert [c.column for c in r.phantom_columns] == ["order_ttl"]


def test_unparseable_sql_reports_error(catalog):
    r = _parse("SELECT FROM WHERE ((", catalog)
    assert not r.ok or r.phantom_columns == []


def test_select_alias_referenced_in_order_by(catalog):
    """Regression: a SELECT-list alias is not a column reference.

    Found by running the checker over real Tableau custom SQL in the catalog,
    where it wrongly flagged four shipped, working queries.
    """
    sql = """
    SELECT
        order_id,
        SUM(order_total) AS total_revenue
    FROM analytics.public.orders
    GROUP BY order_id
    ORDER BY total_revenue DESC
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert r.phantom_columns == []


def test_select_alias_referenced_in_having(catalog):
    sql = """
    SELECT customer_id, COUNT(*) AS order_count
    FROM analytics.public.orders
    GROUP BY customer_id
    HAVING order_count > 5
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert r.phantom_columns == []


def test_alias_shielding_does_not_hide_a_real_phantom(catalog):
    """The alias rule must not become a blanket amnesty."""
    sql = """
    SELECT
        order_ttl,
        SUM(order_total) AS total_revenue
    FROM analytics.public.orders
    ORDER BY total_revenue DESC
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert [c.column for c in r.phantom_columns] == ["order_ttl"]


def test_subquery_alias_column_not_projected_is_caught(catalog):
    sql = """
    SELECT s.total_amount
    FROM (
        SELECT customer_id, SUM(order_total) AS total_spend
        FROM analytics.public.orders
        GROUP BY customer_id
    ) AS s
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert [d.column for d in r.derived_phantoms] == ["total_amount"]


def test_subquery_with_star_is_not_judged(catalog):
    """A derived table selecting * can return anything, so stay quiet."""
    sql = """
    SELECT s.anything_at_all
    FROM (SELECT * FROM analytics.public.orders) AS s
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert r.derived_phantoms == []


def test_byte_order_mark_does_not_break_parsing(catalog):
    """Windows editors and shells write BOMs by default.

    A leading BOM made the whole statement unparseable, so the file was
    reported as unchecked for a reason with nothing to do with its SQL.
    """
    r = _parse("﻿SELECT order_ttl FROM analytics.public.orders", catalog)
    assert r.ok, r.parse_error
    assert [c.column for c in r.phantom_columns] == ["order_ttl"]


def test_update_with_a_bad_column_is_caught(catalog):
    """UPDATE has no SELECT scope, so its columns were silently unchecked."""
    r = _parse(
        "UPDATE analytics.public.orders SET order_ttl = 1 WHERE order_id = 2",
        catalog,
    )
    assert r.ok
    assert [c.column for c in r.phantom_columns] == ["order_ttl"]


def test_update_with_good_columns_is_quiet(catalog):
    r = _parse(
        "UPDATE analytics.public.orders SET order_total = 1 WHERE order_id = 2",
        catalog,
    )
    assert r.ok
    assert r.phantom_columns == []


def test_delete_with_a_bad_column_is_caught(catalog):
    r = _parse(
        "DELETE FROM analytics.public.orders WHERE order_ttl > 5", catalog
    )
    assert r.ok
    assert [c.column for c in r.phantom_columns] == ["order_ttl"]


def test_update_touching_two_tables_stays_quiet(catalog):
    """With a second table in play a bare column is ambiguous.

    Staying silent is the right answer: guessing which table a column belongs
    to is how a checker starts inventing findings.
    """
    sql = (
        "UPDATE analytics.public.orders SET order_total = c.customer_id "
        "FROM analytics.public.customers c WHERE c.customer_id = 1"
    )
    r = _parse(sql, catalog)
    assert r.phantom_columns == []


def test_same_missing_table_in_two_letter_cases_is_one_finding(catalog):
    """Only the leaf name was case-folded, so the database and schema parts
    made `ANALYTICS.PUBLIC.ORDRS` a different table from `analytics.public.ordrs`
    and the same typo was reported twice."""
    sql = (
        "SELECT a.x FROM analytics.public.ordrs a "
        "JOIN ANALYTICS.PUBLIC.ORDRS b ON a.x = b.x"
    )
    r = _parse(sql, catalog)
    assert r.ok
    assert len(r.tables) == 1


def test_quoted_and_upper_case_identifiers_resolve(catalog):
    r = _parse('SELECT "ORDER_ID" FROM "ANALYTICS"."PUBLIC"."ORDERS"', catalog)
    assert r.ok
    assert r.phantom_columns == []


def test_deeply_nested_statement_is_reported_not_fatal(catalog):
    """Past a certain depth sqlglot exhausts the C stack and takes the whole
    process with it: no traceback, no report, an exit code that reads as
    infrastructure failure. The depth is measured off the token stream, which
    is the one stage that does not recurse, before the parser ever runs."""
    sql = "SELECT order_id FROM analytics.public.orders"
    for _ in range(200):
        sql = f"SELECT order_id FROM ({sql}) AS t"

    r = _parse(sql, catalog)
    assert r.degraded, "an unchecked statement must say so"
    assert "nests" in r.degraded[0]
    assert r.phantom_columns == []


def test_ordinary_nesting_is_still_checked(catalog):
    """The guard must not fire on a query anyone would actually write."""
    sql = "SELECT order_id, order_ttl FROM analytics.public.orders"
    for _ in range(5):
        sql = f"SELECT order_id, order_ttl FROM ({sql}) AS t"

    r = _parse(sql, catalog)
    assert r.degraded == []
    assert [c.column for c in r.phantom_columns] == ["order_ttl"]


def test_a_huge_flat_predicate_is_not_mistaken_for_deep_nesting(catalog):
    """An ORM emitting a 2000-term IN list is normal, not pathological."""
    items = ", ".join(str(i) for i in range(2000))
    r = _parse(
        f"SELECT order_ttl FROM analytics.public.orders WHERE order_id IN ({items})",
        catalog,
    )
    assert r.degraded == []
    assert [c.column for c in r.phantom_columns] == ["order_ttl"]


def test_output_table_detected(catalog):
    sql = """
    CREATE TABLE analytics.public.order_summary AS
    SELECT customer_id, SUM(order_total) AS total
    FROM analytics.public.orders
    GROUP BY customer_id
    """
    r = _parse(sql, catalog)
    assert r.ok
    assert [t.table.lower() for t in r.out_tables] == ["order_summary"]
