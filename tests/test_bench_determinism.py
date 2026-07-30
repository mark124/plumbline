"""The benchmark must measure the code, not the catalog's mood.

The published precision and recall numbers are only meaningful if the case set
is a function of *which* tables and columns exist, never of the order the
catalog happened to return them in. That has been wrong twice:

  1. Table selection used `hash(template_name)`, and Python randomizes string
     hashing per process, so the fixed seed did nothing and two runs of
     identical code scored 100% and 98.4%.
  2. Column order came straight from the catalog and fed `rng.sample`, so
     rebuilding the search index changed which columns the templates picked
     and therefore the mix of injected defect kinds. The totals happened not
     to move, so comparing only the headline numbers missed it.

Both were caught by luck rather than by a test. This is that test: it shuffles
the input and asserts the output is byte-identical, which is the property the
numbers actually depend on.
"""

from __future__ import annotations

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))

from generate import Table, build, tables_from_catalog_dump  # noqa: E402


def _tables():
    return [
        Table(
            database="ANALYTICS",
            schema="PUBLIC",
            name=f"TABLE_{i}",
            columns=[f"col_{j}" for j in range(8)] + [f"t{i}_id", "customer_id"],
        )
        for i in range(6)
    ]


def _signature(valid, defective):
    """Everything about the case set that could change a reported number."""
    return (
        [(c.case_id, c.sql) for c in valid],
        [(c.case_id, c.sql, c.defect_kind, c.defect_token) for c in defective],
    )


def test_case_set_is_identical_across_processes():
    """The seed must actually be doing something.

    `hash()` of a str is salted per process, so this is the property that a
    seeded harness is supposed to have and did not.
    """
    tables = _tables()
    assert _signature(*build(tables)) == _signature(*build(tables))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_case_set_does_not_depend_on_table_order(seed):
    tables = _tables()
    shuffled = list(tables)
    random.Random(seed).shuffle(shuffled)
    assert _signature(*build(tables)) == _signature(*build(shuffled))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_case_set_does_not_depend_on_column_order(seed):
    """The one that actually bit. A search reindex reordered fields, which
    reordered `rng.sample`, which changed the defect-kind mix."""
    rng = random.Random(seed)
    tables = _tables()
    reordered = [
        Table(
            database=t.database,
            schema=t.schema,
            name=t.name,
            columns=rng.sample(t.columns, len(t.columns)),
        )
        for t in tables
    ]
    assert _signature(*build(tables)) == _signature(*build(reordered))


def test_the_dump_reader_sorts_columns():
    """Order independence is enforced where the catalog is read, so it holds
    for every caller rather than only inside build()."""
    records = [
        {
            "platform": "snowflake",
            "qualifiedName": "inst.DB.SCHEMA.ORDERS",
            "columns": [{"path": p} for p in ("zulu", "alpha", "mike")],
        }
    ]
    assert tables_from_catalog_dump(records)[0].columns == ["alpha", "mike", "zulu"]


def test_the_dump_reader_drops_duplicate_leaf_names():
    """A v2 fieldPath and a plain one can reduce to the same leaf."""
    records = [
        {
            "platform": "snowflake",
            "qualifiedName": "inst.DB.SCHEMA.ORDERS",
            "columns": [
                {"path": "[version=2.0].[type=string].email"},
                {"path": "email"},
                {"path": "order_id"},
            ],
        }
    ]
    assert tables_from_catalog_dump(records)[0].columns == ["email", "order_id"]
