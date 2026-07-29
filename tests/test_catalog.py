"""Tests for surviving what DataHub actually sends back.

Every GraphQL response in `catalog.py` is walked with chained lookups that
assume one nesting shape. That shape came from one DataHub version, on one
instance, with one datapack loaded. GraphQL answers a partially failed query
with nulls inside `data` alongside an `errors` array, so a field the caller
lacks permission on arrives as None where a dict was expected. Losing every
phantom-column finding because a tag lookup met a null would be a very silly
way to fail.
"""

from __future__ import annotations

import pytest

from plumbline.catalog import DataHubCatalog

URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,db.s.t,PROD)"


class _Resolver:
    def resolve_table_parts(self, database, db_schema, table):
        return URN, {"order_id": "NUMBER"}


class _Graph:
    """A graph that answers every query with one fixed shape."""

    _gms_server = "http://localhost:8081"

    def __init__(self, response):
        self.response = response

    def _make_schema_resolver(self, **kwargs):
        return _Resolver()

    def execute_graphql(self, query, variables=None):
        return self.response


# Shapes a real deployment produces: nulls where a dict is expected, a field
# the user cannot read, a renamed block, an empty edge list, a scalar where an
# object belongs.
SHAPES = [
    ("null response", None),
    ("empty dict", {}),
    ("dataset is null", {"dataset": None}),
    (
        "every block null",
        {
            "dataset": {
                "deprecation": None,
                "tags": None,
                "glossaryTerms": None,
                "schemaMetadata": None,
                "editableSchemaMetadata": None,
            }
        },
    ),
    ("inner tag list null", {"dataset": {"tags": {"tags": None}}}),
    ("tag entry is null", {"dataset": {"tags": {"tags": [None]}}}),
    (
        "tag without properties",
        {"dataset": {"tags": {"tags": [{"tag": {"urn": "urn:li:tag:PII"}}]}}},
    ),
    ("fields list contains null", {"dataset": {"schemaMetadata": {"fields": [None]}}}),
    (
        "field without a fieldPath",
        {"dataset": {"schemaMetadata": {"fields": [{"globalTags": None}]}}},
    ),
    ("scalar where an object belongs", {"dataset": {"deprecation": "yes"}}),
    ("list where a dict belongs", {"dataset": []}),
    ("lineage results null", {"searchAcrossLineage": {"searchResults": None}}),
    (
        "lineage entity null",
        {"searchAcrossLineage": {"searchResults": [{"entity": None}]}},
    ),
    ("queries null", {"listQueries": {"queries": None}}),
    (
        "query statement null",
        {"listQueries": {"queries": [{"properties": {"statement": None}}]}},
    ),
    (
        "search entity without a name",
        {"searchAcrossEntities": {"searchResults": [{"entity": {"urn": "urn:x"}}]}},
    ),
    ("total is null", {"searchAcrossEntities": {"total": None}}),
]


@pytest.mark.parametrize("label,response", SHAPES, ids=[s[0] for s in SHAPES])
def test_malformed_response_does_not_crash_the_run(label, response):
    catalog = DataHubCatalog(_Graph(response), platform="snowflake")
    catalog.resolve_table(database="db", db_schema="s", table="t")
    catalog.get_downstreams(URN)
    catalog.get_queries(URN)
    catalog.find_similar_tables("t", db_schema="s")
    catalog.supports_query_history()


WELL_FORMED = {
    "dataset": {
        "deprecation": {"deprecated": True},
        "tags": {"tags": [{"tag": {"urn": "urn:li:tag:Gold", "properties": {"name": "Gold"}}}]},
        "glossaryTerms": {
            "terms": [{"term": {"urn": "urn:li:glossaryTerm:x", "properties": {"name": "Revenue"}}}]
        },
        "schemaMetadata": {
            "fields": [
                {
                    "fieldPath": "[version=2.0].[type=string].cust_email",
                    "globalTags": {
                        "tags": [{"tag": {"urn": "urn:li:tag:PII", "properties": {"name": "PII"}}}]
                    },
                    "glossaryTerms": None,
                }
            ]
        },
        "editableSchemaMetadata": {
            "editableSchemaFieldInfo": [
                {
                    "fieldPath": "order_id",
                    "globalTags": None,
                    "glossaryTerms": {
                        "terms": [{"term": {"urn": "urn:li:glossaryTerm:k", "properties": {"name": "Key"}}}]
                    },
                }
            ]
        },
    }
}


class _SchemaGraph(_Graph):
    """Resolves a table whose columns match the governance response above."""

    def _make_schema_resolver(self, **kwargs):
        class R:
            def resolve_table_parts(self, database, db_schema, table):
                return URN, {"cust_email": "VARCHAR", "order_id": "NUMBER"}

        return R()


def test_a_well_formed_governance_response_is_read_correctly():
    """The null-tolerance above must not have changed what a good response
    means. This is the case every live run actually takes."""
    catalog = DataHubCatalog(_SchemaGraph(WELL_FORMED), platform="snowflake")
    schema = catalog.resolve_table(database="db", db_schema="s", table="t")

    assert schema.deprecated
    assert schema.tags == frozenset({"Gold", "Revenue"})

    email = schema.column("cust_email")
    assert email is not None and email.is_pii, "v2 fieldPath must reduce to the leaf"

    order_id = schema.column("order_id")
    assert order_id is not None
    assert "Key" in order_id.tags, "editableSchemaMetadata terms must be picked up"
    assert not order_id.is_pii


def test_a_deprecation_hint_in_a_tag_name_counts_as_deprecated():
    response = {
        "dataset": {
            "deprecation": {"deprecated": False},
            "tags": {"tags": [{"tag": {"urn": "urn:li:tag:legacy"}}]},
        }
    }
    catalog = DataHubCatalog(_Graph(response), platform="snowflake")
    schema = catalog.resolve_table(database="db", db_schema="s", table="t")
    assert schema.deprecated, "a tag named `legacy` with no properties block"


@pytest.mark.parametrize("label,response", SHAPES, ids=[s[0] for s in SHAPES])
def test_the_schema_still_resolves_when_governance_is_unreadable(label, response):
    """A governance lookup that cannot be parsed must cost only the governance
    metadata, never the columns. The phantom-column check is the one that can
    prove something, and it does not need a single tag to do it."""
    catalog = DataHubCatalog(_Graph(response), platform="snowflake")
    schema = catalog.resolve_table(database="db", db_schema="s", table="t")
    assert schema.exists
    assert schema.has_column("order_id")
