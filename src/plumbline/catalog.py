"""Catalog access for Plumbline.

Everything Layer 1 needs to know about the world comes through the `Catalog`
protocol. There are two implementations: `DataHubCatalog`, which talks to a
live DataHub instance, and `FakeCatalog` (in tests), which lets the entire
deterministic layer be tested without Docker.

Layer 1 deliberately uses the DataHub Python SDK rather than the MCP server.
MCP is a protocol for letting a *model* choose what to look up. Layer 1 does
not have a model in it and must not: it asks a fixed set of questions and
gets facts back. The MCP server is used by the agent layer, where choosing
what to look up is the entire job.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Dict, FrozenSet, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

# How long a successful reachability probe is trusted for.
REACHABILITY_CACHE_SECONDS = 5.0

# DataHub marks deprecation in more than one way depending on how metadata was
# ingested, so we check a tag/term name against this set as well as the
# dedicated deprecation aspect.
DEPRECATION_HINTS = frozenset({"deprecated", "deprecate", "legacy", "do_not_use"})

# Tag/term fragments that indicate a column carries personal data. Matched
# case-insensitively as substrings of the tag name.
PII_HINTS = frozenset({"pii", "personal", "sensitive", "gdpr", "phi", "confidential"})


@dataclasses.dataclass(frozen=True)
class ColumnInfo:
    name: str
    native_type: str = ""
    tags: FrozenSet[str] = frozenset()
    terms: FrozenSet[str] = frozenset()

    @property
    def is_pii(self) -> bool:
        labels = {t.lower() for t in (self.tags | self.terms)}
        return any(hint in label for label in labels for hint in PII_HINTS)


@dataclasses.dataclass(frozen=True)
class TableSchema:
    """What the catalog knows about one table.

    `exists` is the single most important field in this codebase. It is False
    when DataHub returned no schema for the table, which means we cannot say
    anything about that table's columns. Callers must branch on it before
    reporting any column as missing, otherwise an uningested table produces a
    page of false accusations.
    """

    urn: str
    exists: bool
    columns: Dict[str, ColumnInfo] = dataclasses.field(default_factory=dict)
    deprecated: bool = False
    tags: FrozenSet[str] = frozenset()
    name: str = ""

    def column(self, name: str) -> Optional[ColumnInfo]:
        return self.columns.get(name.lower())

    def has_column(self, name: str) -> bool:
        return name.lower() in self.columns

    @property
    def pii_columns(self) -> List[ColumnInfo]:
        return [c for c in self.columns.values() if c.is_pii]


class CatalogUnavailable(RuntimeError):
    """Raised when DataHub could not be reached.

    This exists because the SDK's schema resolver swallows transport errors
    and caches the URN as unresolved, which is indistinguishable from "this
    table does not exist". Left alone, a DataHub outage would make every table
    in a file look like a phantom, and the report would present that as a
    finding about the code. An unreachable catalog is not an empty catalog,
    and the difference has to be visible.
    """


@dataclasses.dataclass(frozen=True)
class Downstream:
    """One consumer of an asset."""

    urn: str
    name: str
    entity_type: str


class Catalog(Protocol):
    """The questions Layer 1 is allowed to ask."""

    platform: str
    env: str

    def resolve_table(
        self,
        *,
        database: Optional[str],
        db_schema: Optional[str],
        table: str,
    ) -> TableSchema: ...

    def get_downstreams(self, urn: str, max_hops: int = 1) -> List[Downstream]: ...

    def get_queries(self, urn: str, limit: int = 50) -> List[str]: ...

    def supports_query_history(self) -> bool: ...

    def find_similar_tables(
        self,
        table: str,
        limit: int = 10,
        *,
        database: Optional[str] = None,
        db_schema: Optional[str] = None,
    ) -> List[Tuple[str, str]]: ...


class DataHubCatalog:
    """Catalog backed by a live DataHub instance."""

    def __init__(
        self,
        graph,
        platform: str = "snowflake",
        env: str = "PROD",
        platform_instance: Optional[str] = None,
    ) -> None:
        self._graph = graph
        self.platform = platform
        self.env = env
        self.platform_instance = platform_instance
        self._schema_resolver = graph._make_schema_resolver(
            platform=platform,
            platform_instance=platform_instance,
            env=env,
        )
        self._table_cache: Dict[Tuple, TableSchema] = {}
        self._sibling_cache: Dict[Tuple, List[Tuple[str, str]]] = {}
        self._query_history_checked = False
        self._query_history_available = False
        self._last_reachable_at = float("-inf")

    # -- tables ---------------------------------------------------------

    def resolve_table(
        self,
        *,
        database: Optional[str],
        db_schema: Optional[str],
        table: str,
    ) -> TableSchema:
        key = (database, db_schema, table)
        if key in self._table_cache:
            return self._table_cache[key]

        try:
            urn, schema_info = self._schema_resolver.resolve_table_parts(
                database=database, db_schema=db_schema, table=table
            )
        except CatalogUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            # The resolver raises raw transport errors. Establish whether the
            # catalog is actually down before letting a stack trace out, so
            # the user gets "DataHub is unreachable" rather than a urllib3
            # traceback they have to interpret.
            self._assert_reachable()
            raise CatalogUnavailable(
                f"Resolving {'.'.join(p for p in (database, db_schema, table) if p)} "
                f"failed ({type(exc).__name__}: {exc})."
            ) from exc

        # The resolver always hands back a synthesized URN, even for a table it
        # has never heard of. `schema_info is None` is the only trustworthy
        # signal that the table is absent. Testing `urn is None` here would
        # silently mark every phantom table as real.
        if schema_info is None:
            # ...but a transport failure looks exactly the same from here, so
            # confirm the catalog is actually answering before believing a
            # negative. Only negatives pay for this check.
            self._assert_reachable()
            result = TableSchema(urn=urn, exists=False, name=table)
            self._table_cache[key] = result
            return result

        columns = {
            col.lower(): ColumnInfo(name=col, native_type=native or "")
            for col, native in schema_info.items()
        }
        deprecated, tags, col_tags = self._fetch_governance(urn)
        for col_name, labels in col_tags.items():
            existing = columns.get(col_name.lower())
            if existing is not None:
                columns[col_name.lower()] = dataclasses.replace(
                    existing, tags=frozenset(labels)
                )

        result = TableSchema(
            urn=urn,
            exists=True,
            columns=columns,
            deprecated=deprecated,
            tags=frozenset(tags),
            name=table,
        )
        self._table_cache[key] = result
        return result

    def _assert_reachable(self) -> None:
        """Raise CatalogUnavailable if DataHub is not answering.

        Memoized briefly: a file with fifty unresolvable tables should not
        cost fifty probes, but the window is short enough that an outage
        starting mid-run is still caught.
        """
        now = time.monotonic()
        if now - self._last_reachable_at < REACHABILITY_CACHE_SECONDS:
            return
        try:
            self._graph.execute_graphql("query plumblinePing { __typename }")
        except Exception as exc:  # noqa: BLE001
            raise CatalogUnavailable(
                f"DataHub at {getattr(self._graph, '_gms_server', 'the configured URL')} "
                f"is not reachable ({type(exc).__name__}). Refusing to report, "
                "because an unreachable catalog would make every table look "
                "like it does not exist."
            ) from exc
        self._last_reachable_at = now

    def _fetch_governance(self, urn: str):
        """Fetch deprecation status and tag names for a dataset and its columns.

        Returns (deprecated, dataset_tags, {column_name: [tag names]}).
        Failures here degrade to "no governance metadata" rather than raising,
        because a governance lookup failing must not stop us reporting the
        phantom-column findings we already have.
        """
        query = """
        query plumblineGovernance($urn: String!) {
          dataset(urn: $urn) {
            deprecation { deprecated }
            tags { tags { tag { urn properties { name } } } }
            glossaryTerms { terms { term { urn properties { name } } } }
            editableSchemaMetadata {
              editableSchemaFieldInfo {
                fieldPath
                globalTags { tags { tag { urn properties { name } } } }
                glossaryTerms { terms { term { urn properties { name } } } }
              }
            }
            schemaMetadata {
              fields {
                fieldPath
                globalTags { tags { tag { urn properties { name } } } }
                glossaryTerms { terms { term { urn properties { name } } } }
              }
            }
          }
        }
        """
        try:
            res = self._graph.execute_graphql(query, variables={"urn": urn})
        except Exception as exc:  # noqa: BLE001
            logger.debug("governance lookup failed for %s: %s", urn, exc)
            return False, set(), {}

        ds = (res or {}).get("dataset") or {}
        deprecated = bool((ds.get("deprecation") or {}).get("deprecated"))

        ds_tags = _tag_names(ds.get("tags")) | _term_names(ds.get("glossaryTerms"))
        if any(hint in t.lower() for t in ds_tags for hint in DEPRECATION_HINTS):
            deprecated = True

        col_tags: Dict[str, set] = {}
        for block_key, list_key in (
            ("schemaMetadata", "fields"),
            ("editableSchemaMetadata", "editableSchemaFieldInfo"),
        ):
            block = ds.get(block_key) or {}
            for field in block.get(list_key) or []:
                path = _leaf_field_path(field.get("fieldPath") or "")
                if not path:
                    continue
                labels = _tag_names(field.get("globalTags")) | _term_names(
                    field.get("glossaryTerms")
                )
                if labels:
                    col_tags.setdefault(path, set()).update(labels)

        return deprecated, ds_tags, col_tags

    # -- lineage --------------------------------------------------------

    def get_downstreams(self, urn: str, max_hops: int = 1) -> List[Downstream]:
        query = """
        query plumblineDownstream($urn: String!, $count: Int!) {
          searchAcrossLineage(
            input: {urn: $urn, direction: DOWNSTREAM, start: 0, count: $count}
          ) {
            searchResults {
              entity {
                urn
                type
                ... on Dataset { name properties { name } }
                ... on Dashboard { properties { name } }
                ... on Chart { properties { name } }
                ... on DataJob { properties { name } }
              }
            }
          }
        }
        """
        try:
            res = self._graph.execute_graphql(
                query, variables={"urn": urn, "count": 100}
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("downstream lookup failed for %s: %s", urn, exc)
            return []

        out: List[Downstream] = []
        results = ((res or {}).get("searchAcrossLineage") or {}).get(
            "searchResults"
        ) or []
        for r in results:
            ent = r.get("entity") or {}
            if not ent.get("urn") or ent["urn"] == urn:
                continue
            name = (ent.get("properties") or {}).get("name") or ent.get("name") or ""
            out.append(
                Downstream(
                    urn=ent["urn"],
                    name=name or ent["urn"].split(",")[-2:-1][0]
                    if "," in ent["urn"]
                    else ent["urn"],
                    entity_type=ent.get("type") or "UNKNOWN",
                )
            )
        return out

    # -- query history --------------------------------------------------

    def get_queries(self, urn: str, limit: int = 50) -> List[str]:
        query = """
        query plumblineQueries($urn: String!, $count: Int!) {
          listQueries(input: {datasetUrn: $urn, start: 0, count: $count}) {
            queries { properties { statement { value } } }
          }
        }
        """
        try:
            res = self._graph.execute_graphql(
                query, variables={"urn": urn, "count": limit}
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("query history lookup failed for %s: %s", urn, exc)
            return []

        out = []
        for q in ((res or {}).get("listQueries") or {}).get("queries") or []:
            stmt = ((q.get("properties") or {}).get("statement") or {}).get("value")
            if stmt:
                out.append(stmt)
        return out

    def find_similar_tables(
        self,
        table: str,
        limit: int = 10,
        *,
        database: Optional[str] = None,
        db_schema: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """Find catalog datasets with names close to `table`.

        Used only to decide whether an unknown table looks like a typo of a
        real one. Returns (bare table name, urn) pairs.

        Keyword search alone is not enough here: DataHub's search will not
        match `ORDRS` to `ORDERS`, and a misspelling is exactly the case we
        care about. So we also list the tables that sit in the same schema and
        let the caller compare names directly. That listing is what actually
        catches typos; the search pass catches renames and moves.
        """
        out: List[Tuple[str, str]] = []
        seen = set()

        def add(name: str, urn: str) -> None:
            leaf = name.split(".")[-1] if name else ""
            if leaf and urn not in seen:
                seen.add(urn)
                out.append((leaf, urn))

        query = """
        query plumblineSimilar($q: String!, $count: Int!) {
          searchAcrossEntities(
            input: {types: [DATASET], query: $q, start: 0, count: $count}
          ) {
            searchResults { entity { urn ... on Dataset { name } } }
          }
        }
        """

        def search(q: str, count: int) -> None:
            try:
                res = self._graph.execute_graphql(
                    query, variables={"q": q, "count": count}
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("similar-table search failed for %r: %s", q, exc)
                return
            results = ((res or {}).get("searchAcrossEntities") or {}).get(
                "searchResults"
            ) or []
            for r in results:
                ent = r.get("entity") or {}
                if ent.get("urn"):
                    add(ent.get("name") or "", ent["urn"])

        search(table, limit)

        # Siblings: everything the catalog holds under the same schema.
        if db_schema:
            for sibling in self._schema_siblings(database, db_schema):
                add(*sibling)

        return out

    def _schema_siblings(
        self, database: Optional[str], db_schema: str
    ) -> List[Tuple[str, str]]:
        """List datasets whose qualified name sits under database.schema."""
        key = (database or "", db_schema)
        if key in self._sibling_cache:
            return self._sibling_cache[key]

        prefix_parts = [p for p in (database, db_schema) if p]
        prefix = ".".join(prefix_parts).lower()

        query = """
        query plumblineSiblings($q: String!, $count: Int!) {
          searchAcrossEntities(
            input: {types: [DATASET], query: $q, start: 0, count: $count}
          ) {
            searchResults {
              entity { urn ... on Dataset { name properties { qualifiedName } } }
            }
          }
        }
        """
        found: List[Tuple[str, str]] = []
        try:
            res = self._graph.execute_graphql(
                query, variables={"q": db_schema, "count": 200}
            )
            results = ((res or {}).get("searchAcrossEntities") or {}).get(
                "searchResults"
            ) or []
            for r in results:
                ent = r.get("entity") or {}
                urn = ent.get("urn")
                if not urn:
                    continue
                # `name` on a Dataset is the leaf (CUSTOMERS); the full path
                # lives on properties.qualifiedName. Filtering on `name` here
                # would silently match nothing.
                qualified = ((ent.get("properties") or {}).get("qualifiedName") or "").lower()
                leaf = ent.get("name") or (qualified.split(".")[-1] if qualified else "")
                if not leaf:
                    continue
                # Keep only datasets actually under this schema. The search is
                # a keyword match, so it returns near misses on other schemas
                # too, and those would produce nonsense suggestions.
                if prefix and prefix not in qualified:
                    continue
                found.append((leaf, urn))
        except Exception as exc:  # noqa: BLE001
            logger.debug("sibling listing failed for %s: %s", db_schema, exc)

        self._sibling_cache[key] = found
        return found

    def supports_query_history(self) -> bool:
        """Whether this catalog has any query history at all.

        If it does not, the join check cannot run, and the report says so
        rather than reporting every join as fine.
        """
        if self._query_history_checked:
            return self._query_history_available
        self._query_history_checked = True
        query = """
        query plumblineAnyQueries {
          searchAcrossEntities(input: {types: [QUERY], query: "*", start: 0, count: 1}) {
            total
          }
        }
        """
        try:
            res = self._graph.execute_graphql(query)
            total = ((res or {}).get("searchAcrossEntities") or {}).get("total") or 0
            self._query_history_available = total > 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("query history probe failed: %s", exc)
            self._query_history_available = False
        return self._query_history_available


def _tag_names(block) -> set:
    out = set()
    for t in (block or {}).get("tags") or []:
        tag = t.get("tag") or {}
        name = (tag.get("properties") or {}).get("name")
        if not name and tag.get("urn"):
            name = tag["urn"].split(":")[-1].rstrip(")")
        if name:
            out.add(name)
    return out


def _term_names(block) -> set:
    out = set()
    for t in (block or {}).get("terms") or []:
        term = t.get("term") or {}
        name = (term.get("properties") or {}).get("name")
        if not name and term.get("urn"):
            name = term["urn"].split(":")[-1].rstrip(")")
        if name:
            out.add(name)
    return out


def _leaf_field_path(field_path: str) -> str:
    """Reduce a DataHub v2 fieldPath to its leaf column name.

    DataHub encodes nested and versioned field paths like
    `[version=2.0].[type=struct].[type=string].user_email`. Column names in
    SQL are the last segment.
    """
    if not field_path:
        return ""
    return field_path.split(".")[-1].strip()
