"""Build a benchmark of SQL that is valid by construction, plus defective twins.

Why this and not a set of LLM-written queries: to measure whether a checker is
correct you need SQL whose correctness you already know. Queries assembled
directly from the catalog's own schemas are valid by construction, so any
ERROR reported on them is a false positive, with no judgment call involved.
Injecting one known defect into a copy of each gives the matching recall set.

This makes the headline numbers reproducible by anyone with the datapack, and
independent of which model happened to write the SQL that day.
"""

from __future__ import annotations

import dataclasses
import random
from typing import Dict, List, Optional, Sequence, Tuple


@dataclasses.dataclass
class Table:
    database: str
    schema: str
    name: str
    columns: List[str]

    @property
    def fqn(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}"


@dataclasses.dataclass
class Case:
    """One benchmark item."""

    case_id: str
    sql: str
    template: str
    # None for the valid set. For the defective set, what we broke and how.
    defect_kind: Optional[str] = None
    defect_token: Optional[str] = None
    expected_check: Optional[str] = None


def _pick(rng: random.Random, seq: Sequence, n: int) -> List:
    n = min(n, len(seq))
    return rng.sample(list(seq), n)


# -- valid query templates ------------------------------------------------
# Each returns SQL that references only real tables and real columns.


def t_simple(rng, t: Table) -> str:
    cols = _pick(rng, t.columns, 3)
    return f"SELECT {', '.join(cols)}\nFROM {t.fqn}"


def t_where_order(rng, t: Table) -> str:
    cols = _pick(rng, t.columns, 3)
    return (
        f"SELECT {', '.join(cols)}\n"
        f"FROM {t.fqn}\n"
        f"WHERE {cols[0]} IS NOT NULL\n"
        f"ORDER BY {cols[-1]} DESC"
    )


def t_aggregate(rng, t: Table) -> str:
    cols = _pick(rng, t.columns, 2)
    return (
        f"SELECT {cols[0]}, COUNT(*) AS row_count\n"
        f"FROM {t.fqn}\n"
        f"GROUP BY {cols[0]}\n"
        f"HAVING COUNT(*) > 1"
    )


def t_case_expr(rng, t: Table) -> str:
    cols = _pick(rng, t.columns, 2)
    return (
        f"SELECT {cols[0]},\n"
        f"       CASE WHEN {cols[1]} IS NULL THEN 'missing' ELSE 'present' END AS flag\n"
        f"FROM {t.fqn}"
    )


def t_star(rng, t: Table) -> str:
    return f"SELECT *\nFROM {t.fqn}"


def t_cte(rng, t: Table) -> str:
    cols = _pick(rng, t.columns, 2)
    return (
        f"WITH base AS (\n"
        f"    SELECT {cols[0]}, {cols[1]}\n"
        f"    FROM {t.fqn}\n"
        f")\n"
        f"SELECT {cols[0]}\nFROM base"
    )


def t_subquery(rng, t: Table) -> str:
    cols = _pick(rng, t.columns, 2)
    return (
        f"SELECT s.{cols[0]}\n"
        f"FROM (\n"
        f"    SELECT {cols[0]}, {cols[1]}\n"
        f"    FROM {t.fqn}\n"
        f") AS s"
    )


def t_window(rng, t: Table) -> str:
    cols = _pick(rng, t.columns, 2)
    return (
        f"SELECT {cols[0]},\n"
        f"       ROW_NUMBER() OVER (PARTITION BY {cols[0]} ORDER BY {cols[1]}) AS rn\n"
        f"FROM {t.fqn}"
    )


def t_ctas(rng, t: Table) -> str:
    cols = _pick(rng, t.columns, 2)
    return (
        f"CREATE TABLE {t.database}.{t.schema}.plumbline_scratch AS\n"
        f"SELECT {cols[0]}, {cols[1]}\n"
        f"FROM {t.fqn}"
    )


SINGLE_TABLE_TEMPLATES = [
    ("simple", t_simple),
    ("where_order", t_where_order),
    ("aggregate", t_aggregate),
    ("case_expr", t_case_expr),
    ("star", t_star),
    ("cte", t_cte),
    ("subquery", t_subquery),
    ("window", t_window),
    ("ctas", t_ctas),
]


def t_join(rng, left: Table, right: Table, key: str) -> str:
    lcols = [c for c in _pick(rng, left.columns, 2) if c != key] or [key]
    rcols = [c for c in _pick(rng, right.columns, 2) if c != key] or [key]
    return (
        f"SELECT l.{lcols[0]}, r.{rcols[0]}\n"
        f"FROM {left.fqn} AS l\n"
        f"JOIN {right.fqn} AS r ON l.{key} = r.{key}"
    )


# -- defect injection -----------------------------------------------------


def typo(rng: random.Random, name: str) -> str:
    """Produce a realistic typo: drop a character or transpose two."""
    if len(name) < 4:
        return name + "x"
    if rng.random() < 0.5:
        i = rng.randrange(1, len(name) - 1)
        return name[:i] + name[i + 1 :]
    i = rng.randrange(1, len(name) - 2)
    return name[:i] + name[i + 1] + name[i] + name[i + 2 :]


def _replace_token(sql: str, old: str, new: str) -> Optional[str]:
    """Replace whole-word `old` with `new`, once, case-insensitively."""
    import re

    pattern = re.compile(rf"\b{re.escape(old)}\b", re.IGNORECASE)
    if not pattern.search(sql):
        return None
    return pattern.sub(new, sql, count=1)


def build(
    tables: Sequence[Table],
    *,
    seed: int = 20260726,
    per_template: int = 6,
) -> Tuple[List[Case], List[Case]]:
    """Return (valid_cases, defective_cases)."""
    rng = random.Random(seed)
    usable = [t for t in tables if len(t.columns) >= 4]
    valid: List[Case] = []

    for tmpl_name, fn in SINGLE_TABLE_TEMPLATES:
        for i in range(per_template):
            t = usable[(i * 7 + hash(tmpl_name)) % len(usable)]
            valid.append(
                Case(
                    case_id=f"{tmpl_name}-{i}",
                    sql=fn(rng, t),
                    template=tmpl_name,
                )
            )

    # Joins between tables that genuinely share a column name.
    pairs = []
    for i, a in enumerate(usable):
        for b in usable[i + 1 :]:
            shared = set(c.lower() for c in a.columns) & set(
                c.lower() for c in b.columns
            )
            # id-ish keys make for realistic joins
            keys = [k for k in shared if k.endswith("_id")]
            if keys:
                pairs.append((a, b, sorted(keys)[0]))
    rng.shuffle(pairs)
    for i, (a, b, key) in enumerate(pairs[: per_template * 2]):
        valid.append(
            Case(case_id=f"join-{i}", sql=t_join(rng, a, b, key), template="join")
        )

    # -- defective twins --------------------------------------------------
    defective: List[Case] = []
    by_name = {t.name.lower(): t for t in usable}

    for case in valid:
        # Which real identifiers appear in this SQL?
        table = None
        for t in usable:
            if t.name.lower() in case.sql.lower():
                table = t
                break
        if table is None:
            continue

        kind = rng.choice(["column_typo", "column_invented", "table_typo"])

        if kind == "table_typo":
            broken_name = typo(rng, table.name)
            if broken_name.lower() in by_name:
                continue
            sql = _replace_token(case.sql, table.name, broken_name)
            if not sql:
                continue
            defective.append(
                Case(
                    case_id=f"{case.case_id}+table_typo",
                    sql=sql,
                    template=case.template,
                    defect_kind="table_typo",
                    defect_token=broken_name,
                    expected_check="phantom_table",
                )
            )
            continue

        # Pick a real column that actually appears in the rendered SQL.
        present = [c for c in table.columns if _replace_token(case.sql, c, c)]
        if not present:
            continue
        col = rng.choice(present)

        if kind == "column_typo":
            broken = typo(rng, col)
        else:
            # A name a model might reasonably invent for this table.
            broken = rng.choice(
                ["total_amount", "created_date", "customer_name", "status_code", "is_active"]
            )
        if broken.lower() in {c.lower() for c in table.columns}:
            continue

        sql = _replace_token(case.sql, col, broken)
        if not sql:
            continue
        defective.append(
            Case(
                case_id=f"{case.case_id}+{kind}",
                sql=sql,
                template=case.template,
                defect_kind=kind,
                defect_token=broken,
                expected_check="phantom_column",
            )
        )

    return valid, defective


def tables_from_catalog_dump(records: Sequence[Dict], platform: str = "snowflake") -> List[Table]:
    """Turn the GraphQL dump into Table objects.

    Dataset names look like `instance.DB.SCHEMA.TABLE`; the leading segment is
    the platform instance and is supplied separately at check time, so it is
    dropped here.
    """
    out: List[Table] = []
    for r in records:
        if r.get("platform") != platform or not r.get("columns"):
            continue
        name = r.get("qualifiedName") or r.get("name") or ""
        parts = name.split(".")
        if len(parts) < 4:
            continue
        _instance, database, schema, table = parts[0], parts[1], parts[2], parts[-1]
        cols = []
        for c in r["columns"]:
            leaf = c["path"].split(".")[-1].strip()
            if leaf and leaf not in cols:
                cols.append(leaf)
        if cols:
            out.append(Table(database=database, schema=schema, name=table, columns=cols))
    return out
