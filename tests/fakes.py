"""A hand-built catalog for testing Layer 1 without Docker."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from plumbline.catalog import ColumnInfo, Downstream, TableSchema


def _urn(platform: str, name: str, env: str = "PROD") -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"


class FakeCatalog:
    """In-memory catalog.

    Tables registered here "exist". Anything else resolves to exists=False,
    which is exactly how a real uningested table behaves.
    """

    platform = "snowflake"
    env = "PROD"

    def __init__(
        self,
        tables: Optional[Dict[str, Dict[str, str]]] = None,
        *,
        deprecated: Optional[set] = None,
        pii_columns: Optional[Dict[str, set]] = None,
        downstreams: Optional[Dict[str, List[Downstream]]] = None,
        queries: Optional[Dict[str, List[str]]] = None,
        has_query_history: bool = True,
    ) -> None:
        self.tables = {k.lower(): v for k, v in (tables or {}).items()}
        self.deprecated = {d.lower() for d in (deprecated or set())}
        self.pii_columns = {k.lower(): v for k, v in (pii_columns or {}).items()}
        self.downstreams = downstreams or {}
        self.queries = queries or {}
        self._has_query_history = has_query_history
        self.resolve_calls: List[Tuple] = []
        # Set to True to simulate DataHub being down, which the real catalog
        # detects when a lookup comes back empty.
        self.unreachable = False

    def _key(
        self, database: Optional[str], db_schema: Optional[str], table: str
    ) -> str:
        parts = [p for p in (database, db_schema, table) if p]
        return ".".join(parts).lower()

    def resolve_table(
        self,
        *,
        database: Optional[str],
        db_schema: Optional[str],
        table: str,
    ) -> TableSchema:
        key = self._key(database, db_schema, table)
        self.resolve_calls.append((database, db_schema, table))
        urn = _urn(self.platform, key)

        cols = self.tables.get(key)
        if cols is None:
            if self.unreachable:
                from plumbline.catalog import CatalogUnavailable

                raise CatalogUnavailable("simulated outage")
            return TableSchema(urn=urn, exists=False, name=table)

        pii = {c.lower() for c in self.pii_columns.get(key, set())}
        columns = {
            name.lower(): ColumnInfo(
                name=name,
                native_type=native,
                tags=frozenset({"PII"}) if name.lower() in pii else frozenset(),
            )
            for name, native in cols.items()
        }
        return TableSchema(
            urn=urn,
            exists=True,
            columns=columns,
            deprecated=key in self.deprecated,
            name=table,
        )

    def get_downstreams(self, urn: str, max_hops: int = 1) -> List[Downstream]:
        return self.downstreams.get(urn, [])

    def get_queries(self, urn: str, limit: int = 50) -> List[str]:
        return self.queries.get(urn, [])

    def supports_query_history(self) -> bool:
        return self._has_query_history

    def find_similar_tables(
        self,
        table: str,
        limit: int = 10,
        *,
        database: Optional[str] = None,
        db_schema: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for key in self.tables:
            leaf = key.split(".")[-1]
            out.append((leaf, _urn(self.platform, key)))
        return out[:limit]
