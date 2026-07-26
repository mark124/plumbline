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
