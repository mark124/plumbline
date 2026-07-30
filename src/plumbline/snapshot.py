"""A frozen catalog, so the tool can be tried without standing one up.

Plumbline's recall is bounded by catalog coverage, which creates a nasty first
impression problem: point it at a sparse or empty DataHub and every table
comes back Unknown, nothing blocks, and it looks like the tool does nothing.
That is the tool being honest, and it is still the wrong first experience.

So the repository ships a snapshot of the public `showcase-ecommerce` catalog,
and `--demo` checks against that. Same checker, same checks, same report: the
only thing replaced is where the facts come from. Nothing here is simulated,
because a faked demo is exactly the dishonesty this project exists to attack.

A snapshot is genuinely weaker than a live catalog, and the difference is
stated rather than hidden: it cannot see your warehouse, and it is as stale as
the day it was taken.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Dict, List, Optional, Tuple

from .catalog import ColumnInfo, Downstream, TableSchema

DEFAULT_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "demo",
    "catalog-snapshot.json",
)


class SnapshotCatalog:
    """A `Catalog` backed by a JSON file instead of a live DataHub."""

    def __init__(self, data: Dict) -> None:
        self._data = data
        self.platform = data.get("platform", "snowflake")
        self.env = data.get("env", "PROD")
        self.platform_instance = data.get("platform_instance")
        self.source = data.get("source", "unknown")
        self.taken_at = data.get("taken_at", "unknown")
        self._tables: Dict[str, Dict] = data.get("tables", {})
        self._downstreams: Dict[str, List[Dict]] = data.get("downstreams", {})
        self._queries: Dict[str, List[str]] = data.get("queries", {})

    @classmethod
    def load(cls, path: Optional[str] = None) -> "SnapshotCatalog":
        with open(path or DEFAULT_SNAPSHOT, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    def describe(self) -> str:
        return (
            f"frozen snapshot of {self.source}, taken {self.taken_at}: "
            f"{len(self._tables)} datasets, "
            f"{sum(len(t.get('columns', {})) for t in self._tables.values())} columns"
        )

    # -- the Catalog protocol -------------------------------------------

    def _key(
        self, database: Optional[str], db_schema: Optional[str], table: str
    ) -> str:
        return ".".join(p for p in (database, db_schema, table) if p).lower()

    def resolve_table(
        self, *, database: Optional[str], db_schema: Optional[str], table: str
    ) -> TableSchema:
        key = self._key(database, db_schema, table)
        record = self._tables.get(key)
        if record is None:
            return TableSchema(
                urn=self._synthetic_urn(key), exists=False, name=table
            )
        columns = {
            name.lower(): ColumnInfo(
                name=col.get("name", name),
                native_type=col.get("type", ""),
                tags=frozenset(col.get("tags", ())),
                terms=frozenset(col.get("terms", ())),
            )
            for name, col in record.get("columns", {}).items()
        }
        return TableSchema(
            urn=record["urn"],
            exists=True,
            columns=columns,
            deprecated=bool(record.get("deprecated")),
            tags=frozenset(record.get("tags", ())),
            name=table,
        )

    def _synthetic_urn(self, key: str) -> str:
        instance = f"{self.platform_instance}." if self.platform_instance else ""
        return (
            f"urn:li:dataset:(urn:li:dataPlatform:{self.platform},"
            f"{instance}{key},{self.env})"
        )

    def get_downstreams(self, urn: str, max_hops: int = 1) -> List[Downstream]:
        return [
            Downstream(
                urn=d["urn"], name=d.get("name", ""), entity_type=d.get("type", "UNKNOWN")
            )
            for d in self._downstreams.get(urn, [])
        ]

    def get_queries(self, urn: str, limit: int = 50) -> List[str]:
        return list(self._queries.get(urn, []))[:limit]

    def supports_query_history(self) -> bool:
        return any(self._queries.values())

    def find_similar_tables(
        self,
        table: str,
        limit: int = 10,
        *,
        database: Optional[str] = None,
        db_schema: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """Every dataset in the snapshot, so the near-miss check can compare.

        Sorted, because the caller picks the closest name and an unstable
        order would make the suggestion wobble between runs.
        """
        out = []
        for key in sorted(self._tables):
            record = self._tables[key]
            out.append((key.split(".")[-1], record["urn"]))
        return out[:limit] if limit else out


# --- building one ---------------------------------------------------------


def export(graph, platform: str, env: str, platform_instance: Optional[str]) -> Dict:
    """Read a live DataHub and return a snapshot dict.

    Deliberately captures only what the checks consume: schemas, column tags,
    deprecation, lineage counts and query text. No descriptions, no ownership,
    nothing that would turn a demo fixture into a data export.
    """
    from .catalog import DataHubCatalog

    catalog = DataHubCatalog(
        graph, platform=platform, env=env, platform_instance=platform_instance
    )

    LIST = """query d($start: Int!) {
      searchAcrossEntities(input: {types: [DATASET], query: "*", start: $start, count: 50}) {
        total searchResults { entity { urn ... on Dataset {
          name platform { name } properties { qualifiedName } } } }
      }
    }"""

    urns: List[Tuple[str, str]] = []
    start = 0
    while True:
        page = graph.execute_graphql(LIST, variables={"start": start})[
            "searchAcrossEntities"
        ]
        results = page["searchResults"]
        if not results:
            break
        for r in results:
            ent = r.get("entity") or {}
            if (ent.get("platform") or {}).get("name") != platform:
                continue
            qualified = (ent.get("properties") or {}).get("qualifiedName") or ""
            if qualified:
                urns.append((ent["urn"], qualified))
        start += len(results)
        if start >= page["total"]:
            break

    tables: Dict[str, Dict] = {}
    downstreams: Dict[str, List[Dict]] = {}
    queries: Dict[str, List[str]] = {}

    for urn, qualified in sorted(urns, key=lambda p: p[1].lower()):
        parts = qualified.split(".")
        # The leading segment is the platform instance, which is supplied
        # separately at check time and must not be baked into the key.
        if platform_instance and parts and parts[0] == platform_instance:
            parts = parts[1:]
        if not parts:
            continue
        key = ".".join(parts).lower()

        schema = catalog.resolve_table(
            database=parts[0] if len(parts) > 2 else None,
            db_schema=parts[-2] if len(parts) > 1 else None,
            table=parts[-1],
        )
        if not schema.exists:
            continue

        tables[key] = {
            "urn": schema.urn,
            "deprecated": schema.deprecated,
            "tags": sorted(schema.tags),
            "columns": {
                name: {
                    "name": col.name,
                    "type": col.native_type,
                    "tags": sorted(col.tags),
                    "terms": sorted(col.terms),
                }
                for name, col in sorted(schema.columns.items())
            },
        }

        down = catalog.get_downstreams(schema.urn)
        if down:
            downstreams[schema.urn] = [
                {"urn": d.urn, "name": d.name, "type": d.entity_type} for d in down
            ]
        stmts = catalog.get_queries(schema.urn)
        if stmts:
            queries[schema.urn] = stmts

    import datetime

    return {
        "source": "DataHub showcase-ecommerce datapack",
        "taken_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "platform": platform,
        "env": env,
        "platform_instance": platform_instance,
        "tables": tables,
        "downstreams": downstreams,
        "queries": queries,
    }
