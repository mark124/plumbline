# Plumbline

**Check AI-written data code against your catalog before it merges.**

A plumb line is what a carpenter uses to test whether something is true,
referenced against gravity rather than against opinion. Plumbline checks SQL
written by coding agents against DataHub, rather than against the model's
confidence.

```
$ plumbline check models/customer_revenue.sql

Plumbline: 1 error, 1 warning, 0 unknown, 1 info across 1 file(s).

  [x] Column `credit_limt` does not exist  (models/customer_revenue.sql:5)
      The catalog has a schema for `ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS` and it
      contains no column named `credit_limt`. The closest real column is `credit_limit`.
      Suggested: credit_limit
      Evidence: urn:li:dataset:(urn:li:dataPlatform:snowflake,...customers,PROD)
```

Exit code 1. The pull request does not merge.

## The problem

A growing share of production data code is written by coding agents. The agent
writes a dbt model, a transformation, a backfill. It has no reliable view of
the warehouse, so it does what language models do without ground truth: it
produces plausible column names, plausible join keys, plausible table names.

The dangerous failures are the quiet ones. A hallucinated column that fails at
compile time is the good case. These are the bad ones:

- Reading from the staging copy instead of the certified table.
- Joining on a key pair nobody has ever joined on, silently changing row counts.
- Pulling a PII-tagged column into an output that carries no such tag.
- Changing a column that eleven dashboards depend on.

None of that is visible in the diff. All of it is visible in the catalog.

## What it checks

| Check | What it catches | Severity |
| --- | --- | --- |
| Phantom column | Column referenced on a table whose schema says otherwise | Error |
| Phantom table | Table the catalog has never heard of | Unknown, or Error on a near miss |
| Deprecated source | Reading from an asset marked deprecated | Warning |
| PII propagation | PII-tagged column flowing into an untagged output | Warning |
| Unvetted join | Join key pair seen in no observed production query | Warning |
| Blast radius | Who consumes the asset this statement rewrites | Info |

## The design decision that matters

**The checker does not contain a model.**

Layer 1 parses the SQL, resolves every reference against DataHub, and compares.
No LLM is involved, so the checker cannot itself hallucinate. This is where
every finding and every number in this README comes from. We do not fight
hallucination with more hallucination.

Layer 2 is agentic: for findings that need judgment (which table was meant,
what is the safe rewrite), an agent investigates and proposes a patch. That
patch is handed back to Layer 1 and re-verified. The agent proposes; the
deterministic core disposes.

```
$ plumbline check models/customer_revenue.sql --fix
Fix agent: 1 of 1 proposals passed re-verification.
```

The agent reaches DataHub **only through the official DataHub MCP server** and
gets no other access. It runs with the server's mutation tools disabled, so it
can read the catalog and cannot change it.

A proposal is accepted only if both hold:

1. The original defect is gone from the rewritten statement.
2. The rewrite introduces no new blocking error.

The second condition is the one that matters. Without it, a model that
"resolves" a bad column by pointing at a different nonexistent table would
pass. Rejected proposals are reported with the reason and never shown as
fixes:

```
Fix agent: 0 of 1 proposals passed re-verification.
  - Column `order_ttl` does not exist: rejected: the rewrite introduces a new error
```

### Severity reflects evidence, not alarm

This is the rule that decides whether a tool like this survives in a real repo.

A column missing from a table we hold the schema for is an **error**: the
catalog can prove it. A table absent from the catalog entirely is an
**unknown**, because the catalog cannot tell a hallucinated name from a table
nobody ingested. Unknowns never block the build.

The exception is a near miss. If `ORDRS` is not in the catalog but `ORDERS` is
in the same schema, that is evidence of a typo rather than an ingestion gap,
and it is promoted to an error with the correction attached.

When a check cannot run, it says so rather than passing quietly:

```
Checks that did not run:
  - No query history in this catalog, so the unvetted-join check did not run.
    Joins were not validated.
```

## Measured results

Two harnesses, both reproducible against the public `showcase-ecommerce`
datapack (67 datasets, 816 columns).

### Precision and recall on a constructed benchmark

`bench/run_bench.py` builds queries directly from the catalog's own schemas, so
they are valid by construction and any blocking error is a false positive by
definition. It then injects exactly one known defect per query for the recall
set.

| Measure | Result |
| --- | --- |
| False positive rate on 66 valid queries | **0.0%** (0 blocked) |
| Recall on 59 queries with an injected defect | **100%** (59 caught) |
| ... column typo | 16/16 |
| ... invented column | 21/21 |
| ... table typo | 22/22 |

Templates cover joins, CTEs, subqueries, window functions, `CASE`, `GROUP BY`,
`SELECT *`, and `CREATE TABLE AS`.

**What this does not prove.** These queries were generated by the same project
that checks them, and the injected defects are exactly the three kinds
Plumbline targets. A perfect score here means the resolution logic is sound. It
does not mean the tool catches everything a language model gets wrong, and it
is not a claim about production dbt repositories.

### Behaviour on SQL this project did not write

`bench/real_sql_check.py` runs the checker over every view definition stored in
the catalog: Tableau custom SQL and a Snowflake view.

| Measure | Result |
| --- | --- |
| Real definitions checked | 5 parseable (8 more were LookML / PowerBI M, skipped) |
| Blocking errors | **0** |
| Unparseable | 1 (dbt model containing Jinja) |

This test earned its place. On its first run it reported **four false
positives**: it flagged `total_revenue` in four shipped, working Tableau
queries, because a `SELECT`-list alias referenced in `ORDER BY` looked like a
column reference. That was a real bug in the checker, found by pointing it at
real code. It is fixed, and covered by regression tests
(`test_select_alias_referenced_in_order_by`).

### Cost

The two layers have very different price tags, which is why the fast one is
the gate and the slow one is opt-in.

| Path | Measured |
| --- | --- |
| `plumbline check` (deterministic, the CI gate) | **1.6s** median for one file, 3 runs |
| `plumbline check --fix` (per blocking finding) | **~5 minutes**, and one Claude API call chain each |

Measured against a local DataHub quickstart on a laptop. The agent spends most
of that time investigating the catalog through MCP rather than waiting on any
one call. `--fix` proposes for at most 5 findings per run by default; on a file
with several errors, expect it to take a while. Leave it off in CI unless you
want repairs, and keep the deterministic gate as the thing that blocks merges.

## Known limitations

Stated plainly, because a checker whose blind spots are undocumented is worse
than one with fewer features.

- **dbt Jinja is not rendered.** A model containing `{{ ref(...) }}` or
  `{{ config(...) }}` fails to parse and is reported as unknown, not as passing.
  Compiling the project first and checking `target/compiled` works today.
- **Recall is bounded by catalog coverage.** Plumbline can only disprove
  references to assets DataHub actually holds. A partially ingested warehouse
  yields unknowns, not errors.
- **The unvetted-join check needs query history.** The showcase datapack has
  none, so that check degrades and reports that it did not run. It is exercised
  by unit tests but has not been demonstrated against a catalog with real query
  history.
- **A novel join is not a wrong join.** That check is a heuristic and is always
  a warning.
- **Column-level PII propagation is checked at the statement level**, not
  through multi-hop lineage.
- **`--fix` is slow and can time out.** It is minutes per finding, and a run
  that exceeds the client timeout is reported as "no fix proposed" rather than
  retried. The deterministic findings are unaffected when that happens: you
  still get the error, the location, and the nearest-name suggestion.

## Install

```bash
pip install -e .
```

## Use

```bash
# Point at your DataHub instance
export DATAHUB_GMS_URL=http://localhost:8080
export DATAHUB_GMS_TOKEN=...

plumbline check models/                       # a directory
plumbline check models/orders.sql             # one file
plumbline check "models/**/*.sql"             # a glob

# Reporting
plumbline check models/ --format markdown --out report.md
plumbline check models/ --format json --out findings.json

# Gating
plumbline check models/ --fail-on warn        # stricter
plumbline check models/ --fail-on never       # report only

# Scoping
plumbline check models/ --check phantom_column --check pii_propagation

# Propose verified repairs for blocking findings (needs ANTHROPIC_API_KEY)
plumbline check models/ --fix
```

Tables written unqualified need defaults, and a catalog using platform
instances needs to be told:

```bash
plumbline check models/ \
  --platform snowflake --platform-instance b2fd91 \
  --database ORDER_ENTRY_DB --schema ANALYTICS
```

## In CI

```yaml
- uses: ./  # or rowset/plumbline@v1
  with:
    paths: models
    server: ${{ secrets.DATAHUB_GMS_URL }}
    token: ${{ secrets.DATAHUB_GMS_TOKEN }}
    database: ORDER_ENTRY_DB
    schema: ANALYTICS
```

The report is written to the job summary whether or not the gate passes. A gate
that fails without showing its reasoning just gets disabled.

## Reproducing the results

```bash
datahub docker quickstart
datahub datapack load showcase-ecommerce

python bench/run_bench.py --platform-instance b2fd91 --out bench-results.json
python bench/real_sql_check.py --platform-instance b2fd91
pytest
```

## Contributions made upstream while building this

Building against a real DataHub surfaced two defects in the platform itself,
both reported upstream:

1. `datahub datapack load` fails on Windows. `get_path_schema` parses a path
   with `urlparse`, so `C:\...` yields the scheme `"c"` and the filesystem
   registry lookup raises `KeyError`. One-line fix.
2. `datahub datapack --help` raises `FileNotFoundError`: the package ships
   without `cli/datapack/resources/DATAPACK_AGENT_CONTEXT.md`.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
