"""SQL parsing and reference extraction.

This module answers one question: which catalog objects does this SQL touch,
and which of those references are real?

It leans on sqlglot for parsing and on DataHub's schema resolver for ground
truth. The important subtlety is scoping: a column named `customer_id` might
come from a real warehouse table, from a CTE, or from a subquery. Only the
first can be checked against the catalog, and confusing the three is how a
tool like this ends up crying wolf.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Dict, List, Optional, Tuple

import sqlglot
import sqlglot.tokens
from sqlglot import exp
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.optimizer.scope import build_scope

from .catalog import Catalog, TableSchema

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TableRef:
    """A table as written in the SQL, plus what the catalog says about it."""

    raw: str
    database: Optional[str]
    db_schema: Optional[str]
    table: str
    schema: TableSchema
    line: Optional[int] = None

    @property
    def exists(self) -> bool:
        return self.schema.exists

    @property
    def urn(self) -> str:
        return self.schema.urn


@dataclasses.dataclass
class ColumnUse:
    """A column reference bound to a real catalog table.

    `scope_tables` is set when the column was written without a table
    qualifier and had to be checked against every table in scope. It is what
    the report cites so the reader can see which tables were searched.
    """

    column: str
    table_ref: TableRef
    line: Optional[int] = None
    scope_tables: List[TableRef] = dataclasses.field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.table_ref.raw}.{self.column}"

    @property
    def searched(self) -> List[TableRef]:
        return self.scope_tables or [self.table_ref]


@dataclasses.dataclass
class DerivedColumnUse:
    """A column read off a CTE or subquery that does not project it.

    Not catalog-grounded: the evidence is the query itself, which lists what
    the derived table actually returns. Kept separate from `phantom_columns`
    so the catalog-backed findings stay clean.
    """

    column: str
    source_alias: str
    available: List[str]
    line: Optional[int] = None


@dataclasses.dataclass
class ParsedSql:
    sql: str
    dialect: str
    file: Optional[str] = None
    tables: List[TableRef] = dataclasses.field(default_factory=list)
    out_tables: List[TableRef] = dataclasses.field(default_factory=list)
    column_uses: List[ColumnUse] = dataclasses.field(default_factory=list)
    phantom_columns: List[ColumnUse] = dataclasses.field(default_factory=list)
    derived_phantoms: List[DerivedColumnUse] = dataclasses.field(default_factory=list)
    joins: List = dataclasses.field(default_factory=list)
    parse_error: Optional[str] = None
    # Reasons a check could not be run properly on this file.
    degraded: List[str] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.parse_error is None


def _line_in(sql: str, token: str) -> Optional[int]:
    """Best-effort line number for a token.

    sqlglot does not carry reliable source positions through the optimizer, so
    we locate the token textually. This is an approximation: for a name that
    appears several times we report the first. It is good enough to jump to,
    and it is never used to decide whether a finding is real.
    """
    if not token:
        return None
    pattern = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
    for i, line in enumerate(sql.splitlines(), start=1):
        if pattern.search(line):
            return i
    return None


def _derived_projection(source) -> Optional[set]:
    """The set of column names a CTE or subquery returns.

    Returns None when we cannot know, which is the safe answer. That happens
    when the derived table selects a star, since its output is then whatever
    its own sources hold and naming a column we did not expect is legitimate.
    """
    expression = getattr(source, "expression", None)
    if expression is None:
        return None
    try:
        selects = expression.selects
    except Exception:  # noqa: BLE001
        return None
    if not selects:
        return None
    for projection in selects:
        if isinstance(projection, exp.Star) or projection.find(exp.Star):
            return None
    try:
        names = {n.lower() for n in expression.named_selects if n}
    except Exception:  # noqa: BLE001
        return None
    return names or None


def _display_name(table: exp.Table) -> str:
    """The table's name as a reader would cite it, without its SQL alias.

    `table.sql()` renders `ORDER_ENTRY.CUSTOMERS AS c`, which reads badly in a
    finding: the alias is an artifact of this one query, not part of the
    asset's identity.
    """
    parts = [table.text("catalog"), table.text("db"), table.name]
    return ".".join(p for p in parts if p)


def _original_spelling(sql: str, name: str) -> str:
    """Recover how the author actually wrote an identifier.

    qualify() normalizes identifier case per dialect (Snowflake upper-cases),
    so by the time we see a phantom column it may read ORDER_TTL when the file
    says order_ttl. Reports should quote the file, not the optimizer.
    """
    match = re.search(rf"\b{re.escape(name)}\b", sql, re.IGNORECASE)
    return match.group(0) if match else name


def _table_parts(
    table: exp.Table, default_db: Optional[str], default_schema: Optional[str]
) -> Tuple[Optional[str], Optional[str], str]:
    """Return (database, schema, table), applying defaults for missing parts."""
    name = table.name
    db_schema = table.text("db") or default_schema
    database = table.text("catalog") or default_db
    return database or None, db_schema or None, name


def parse_sql(
    sql: str,
    catalog: Catalog,
    *,
    dialect: str = "snowflake",
    default_db: Optional[str] = None,
    default_schema: Optional[str] = None,
    file: Optional[str] = None,
    line_offset: int = 0,
) -> ParsedSql:
    """Parse one SQL statement and resolve every reference against the catalog.

    `line_offset` is added to every reported line, so a statement lifted out of
    the middle of a file still points at the right place in that file.
    """
    result = ParsedSql(sql=sql, dialect=dialect, file=file)

    def _line_of(text: str, token: str) -> Optional[int]:
        line = _line_in(text, token)
        return None if line is None else line + line_offset

    try:
        expression = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as exc:  # noqa: BLE001
        result.parse_error = f"{type(exc).__name__}: {exc}"
        return result

    if expression is None:
        result.parse_error = "empty statement"
        return result

    # -- 1. resolve every table reference -------------------------------
    # CTE names are not catalog tables. Collect them first so we never look
    # up `WITH recent_orders AS (...)` as a warehouse table and then report
    # it as a phantom.
    cte_names = {
        cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE) if cte.alias
    }

    by_key: Dict[Tuple, TableRef] = {}

    for table in expression.find_all(exp.Table):
        if not table.name:
            continue
        if table.name.lower() in cte_names and not table.text("db"):
            continue

        database, db_schema, name = _table_parts(table, default_db, default_schema)
        key = (database, db_schema, name.lower())
        if key in by_key:
            continue

        table_schema = catalog.resolve_table(
            database=database, db_schema=db_schema, table=name
        )
        ref = TableRef(
            raw=_display_name(table),
            database=database,
            db_schema=db_schema,
            table=name,
            schema=table_schema,
            line=_line_of(sql, name),
        )
        by_key[key] = ref

    result.tables = list(by_key.values())

    # Outputs: what this statement writes to.
    for node in expression.find_all(exp.Create, exp.Insert):
        target = node.find(exp.Table)
        if target is None:
            continue
        database, db_schema, name = _table_parts(target, default_db, default_schema)
        key = (database, db_schema, name.lower())
        ref = by_key.get(key)
        if ref is None:
            ref = TableRef(
                raw=_display_name(target),
                database=database,
                db_schema=db_schema,
                table=name,
                schema=catalog.resolve_table(
                    database=database, db_schema=db_schema, table=name
                ),
                line=_line_of(sql, name),
            )
        result.out_tables.append(ref)

    # -- 2. bind columns to their source tables -------------------------
    # We deliberately do NOT use sqlglot's full qualify() here. Given a schema,
    # qualify() raises OptimizeError on the first unknown column it meets,
    # which would abort the whole file and throw away every other finding in
    # it. The unknown column is the thing we are looking for, so aborting on it
    # is backwards. Qualifying tables only is enough to bind columns to
    # sources, and it cannot fail on a bad column name.
    try:
        qualified = qualify_tables(
            expression.copy(),
            db=default_schema,
            catalog=default_db,
            dialect=dialect,
        )
    except Exception as exc:  # noqa: BLE001
        # We keep the table-level findings, which do not depend on binding,
        # and record that column checking did not run rather than implying the
        # columns were all fine.
        result.degraded.append(
            f"column binding unavailable ({type(exc).__name__}); "
            "phantom-column check did not run on this file"
        )
        return result

    root = build_scope(qualified)
    if root is None:
        result.degraded.append(
            "scope analysis unavailable; phantom-column check did not run on this file"
        )
        return result

    lookup_by_name: Dict[str, TableRef] = {}
    for ref in by_key.values():
        lookup_by_name[ref.table.lower()] = ref

    def _ref_for(source: exp.Table) -> Optional[TableRef]:
        database, db_schema, name = _table_parts(source, default_db, default_schema)
        return by_key.get((database, db_schema, name.lower())) or lookup_by_name.get(
            name.lower()
        )

    for scope in root.traverse():
        # Classify this scope's sources once. A source we can check against is
        # a real table with a known schema. Anything else (a CTE, a subquery,
        # a table missing from the catalog) is opaque: it could legitimately
        # supply any column name, so its presence means we must not convict an
        # unqualified column.
        # Names this scope introduces itself. SQL lets ORDER BY, GROUP BY and
        # HAVING refer to a SELECT-list alias, so `ORDER BY total_revenue`
        # after `SUM(order_total) AS total_revenue` is valid and must not be
        # read as a reference to a column named total_revenue.
        # Only an explicit `AS name` introduces a new name. `named_selects`
        # cannot be used here: for a bare projection like `SELECT order_ttl` it
        # reports `order_ttl` as an output name, which would make every
        # selected column shield itself and silence the check entirely.
        output_aliases = set()
        try:
            for projection in scope.expression.selects:
                if isinstance(projection, exp.Alias) and projection.alias:
                    output_aliases.add(projection.alias.lower())
        except Exception:  # noqa: BLE001
            output_aliases = set()

        checkable: List[TableRef] = []
        has_opaque = False
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                ref = _ref_for(source)
                if ref is not None and ref.exists and ref.schema.columns:
                    checkable.append(ref)
                else:
                    has_opaque = True
            else:
                has_opaque = True

        for column in scope.find_all(exp.Column):
            col_name = column.name
            if not col_name:
                continue
            # A qualified star (`o.*`) is a Column whose name is literally "*".
            # It selects every column the table has, so it can never be a
            # missing one. Without this, ordinary `SELECT o.*` raises a
            # blocking error against a perfectly good query.
            if col_name == "*" or isinstance(column.this, exp.Star):
                continue
            alias = column.table
            spelling = _original_spelling(sql, col_name)

            if alias:
                source = scope.sources.get(alias)
                if source is None:
                    continue
                if not isinstance(source, exp.Table):
                    # A CTE or subquery. Its columns are computed rather than
                    # catalog columns, but the query itself states exactly what
                    # it returns, so an alias reading a name it does not
                    # project is still provably wrong.
                    derived = _derived_projection(source)
                    if derived is not None and col_name.lower() not in derived:
                        result.derived_phantoms.append(
                            DerivedColumnUse(
                                column=spelling,
                                source_alias=alias,
                                available=sorted(derived),
                                line=_line_of(sql, col_name),
                            )
                        )
                    continue
                ref = _ref_for(source)
                if ref is None:
                    continue

                use = ColumnUse(
                    column=spelling, table_ref=ref, line=_line_of(sql, col_name)
                )
                result.column_uses.append(use)

                # Only a table we actually found can convict a column. An
                # absent table means unknown, and is reported separately as a
                # phantom table, never as a pile of phantom columns.
                if (
                    ref.exists
                    and ref.schema.columns
                    and not ref.schema.has_column(col_name)
                ):
                    result.phantom_columns.append(use)
                continue

            # No alias. A column still unqualified here is one we could not
            # place against a source. That is the signal we want, but it is
            # only conclusive when every source in scope is checkable and none
            # of them has the column.
            if col_name.lower() in output_aliases:
                continue
            if has_opaque or not checkable:
                continue
            if any(ref.schema.has_column(col_name) for ref in checkable):
                continue

            use = ColumnUse(
                column=spelling,
                table_ref=checkable[0],
                line=_line_of(sql, col_name),
                scope_tables=list(checkable),
            )
            result.column_uses.append(use)
            result.phantom_columns.append(use)

    # -- 3. joins (via DataHub's own parser) ----------------------------
    result.joins = _extract_joins(sql, dialect)

    return result


def _extract_joins(sql: str, dialect: str) -> List:
    """Extract join conditions as (left_col, right_col) pairs.

    Uses sqlglot directly rather than DataHub's JoinInfo so that this works
    without a graph connection; the join check compares these pairs against
    observed production queries.
    """
    out: List = []
    try:
        expression = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:  # noqa: BLE001
        return out
    if expression is None:
        return out

    for join in expression.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            continue
        for eq in on.find_all(exp.EQ):
            left, right = eq.this, eq.expression
            if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                out.append((left.name.lower(), right.name.lower()))
    return out


def split_statements(
    sql: str, dialect: str = "snowflake"
) -> List[Tuple[str, int]]:
    """Split a file into statements, returning (text, first_line_number).

    The text is sliced out of the original source rather than re-rendered.
    Round-tripping through sqlglot's generator would collapse the statement
    onto a single line, and every finding in the file would then report line
    1, which makes the report useless for navigating to the problem.
    """
    try:
        tokens = sqlglot.tokenize(sql, dialect=dialect)
    except Exception:  # noqa: BLE001
        return [(sql, 1)]

    boundaries = [
        token.end + 1
        for token in tokens
        if token.token_type == sqlglot.tokens.TokenType.SEMICOLON
    ]

    chunks: List[Tuple[str, int]] = []
    start = 0
    for end in boundaries + [len(sql)]:
        if end <= start:
            continue
        piece = sql[start:end]
        if piece.strip():
            # Line of the first non-whitespace character in this chunk.
            leading = len(piece) - len(piece.lstrip())
            line = sql.count("\n", 0, start + leading) + 1
            chunks.append((piece.strip(), line))
        start = end

    return chunks or [(sql, 1)]
