# Plumbline

**A catalog-grounded verifier for AI-written data code.**

Working name: Plumbline. A plumb line is the tool a carpenter uses to check
whether something is true, referenced against gravity rather than against
opinion. This checks AI-written SQL against the catalog rather than against
the model's confidence.

Target track: **Metadata-Aware Code Generation & Development**.

## The problem

In 2026 a large and growing share of production data code is written by
coding agents. The agent writes a dbt model, a transformation, a backfill
script. The agent has no reliable view of the warehouse, so it does what
language models do when they lack ground truth: it produces plausible
column names, plausible join keys, and plausible table names.

The failure mode is not a crash. A hallucinated column usually fails loudly
at compile time, which is the *good* case. The dangerous cases are quiet:

- The agent joins on a key pair nobody has ever joined on, and the row count
  silently changes.
- The agent reads from a table that exists but is deprecated, or is the
  staging copy rather than the certified one.
- The agent pulls a PII-tagged column into an output that is not marked PII.
- The agent changes a column that eleven downstream dashboards depend on.

None of these are visible in the diff. All of them are visible in the
catalog. That is the whole idea.

## What it does

Plumbline sits between "the agent wrote some SQL" and "the SQL merges."

Given a SQL file or a dbt model, it:

1. Parses the SQL (via DataHub's own sqlglot-based parser) to extract table
   references, column references, joins, and output schema.
2. Resolves every reference against the live DataHub catalog.
3. Emits findings, each anchored to a file and line.
4. Exits nonzero so CI can gate on it.

### Check families

| Check | What it catches | Source of truth |
| --- | --- | --- |
| Phantom table | Table referenced that does not exist | catalog search / get_entities |
| Phantom column | Column referenced that is not in the table's schema | list_schema_fields |
| Deprecated source | Table exists but is marked deprecated | entity status + tags |
| Unvetted join | Join key pair appearing in zero observed production queries | get_dataset_queries |
| PII propagation | PII-tagged column flowing into an untagged output | column tags + column-level lineage |
| Blast radius | Existing asset's schema changed, with live downstream consumers | get_lineage |

### Architecture: the checker must not hallucinate

Two layers, and the split is the point.

**Layer 1, deterministic.** Parse, resolve, compare. No model in the loop.
A column either appears in the catalog's schema for that table or it does
not. This layer produces the findings and the numbers. We do not fight
hallucination with more hallucination.

**Layer 2, agentic.** For findings that need judgment (which table did the
agent *mean*, what is the safe rewrite, is this join defensible), an agent
with the DataHub MCP tools investigates and proposes a patch. The patch is
then handed back to Layer 1 and re-verified. The agent proposes; the
deterministic core disposes. A proposed fix that does not pass Layer 1 is
never shown as a fix.

## Honest limits (state these up front, in the README and the video)

- **Precision is high, recall is bounded by catalog completeness.** If an
  asset is genuinely missing from the catalog, Plumbline will flag correct
  code. This is why findings are tiered: "in catalog, column absent" is an
  error; "table not in catalog at all" is an unknown, reported separately
  and never counted as a hallucination.
- **Unvetted join is a heuristic, not a proof.** Zero observed queries means
  novel, not wrong. It is a yellow, never a red.
- **Query history is only as good as ingestion.** On a catalog with no
  query history the join check degrades to nothing, and it says so instead
  of silently passing.

## The measured claim

The deliverable is a tool plus evidence.

Build a task set of realistic analytics-engineering requests grounded in the
showcase-ecommerce datapack (1,049 entities). Generate SQL for each under
two conditions:

- **(a) schema-blind:** the request and table names only.
- **(b) catalog-grounded:** an agent with DataHub MCP tools.

Measure with Layer 1: phantom table rate, phantom column rate, deprecated
source use, PII exposure. Report the delta.

**On circularity:** Layer 1 is the measuring instrument for an experiment
about grounding, not about Layer 1. Its correctness is independently
checkable (a column is in the catalog or it is not) and is covered by unit
tests plus a hand-audited sample. Say this explicitly rather than hoping
nobody asks.

### Validating the instrument: precision against real production queries

The number that actually matters for adoption is the false positive rate,
because a checker that flags correct code gets switched off in a week.

There is a clean way to measure it. DataHub's query history holds SQL that
demonstrably ran against these tables. Run Plumbline over those observed
queries: every ERROR it reports on a query that really executed is, by
construction, a false positive. This gives a precision figure that does not
depend on my judgment of what is correct.

Report it whichever way it comes out. If the false positive rate is not
near zero, that is the finding, and the honest move is to say so and fix the
check rather than quietly drop the experiment.

Expected sources of genuine false positives, to look for specifically:

- Dialect mismatch (a query written for one engine, parsed as another).
- Tables ingested without column schemas, which should be UNKNOWN not ERROR.
- Case sensitivity between the SQL and the catalog's stored field paths.

## Why this scores

| Criterion | How |
| --- | --- |
| Use of DataHub | MCP tools for resolution, plus DataHub's own sqlglot lineage parser and schema resolver. Not a wrapper around search. |
| Technical execution | Deterministic core with tests, real CI gate, end to end on a live instance. |
| Originality | The out-of-box direction is catalog to code. This is code back to catalog, as a gate. Nothing ships this. |
| Real-world usefulness | This is the live problem for every team letting agents touch dbt. |
| Submission quality | Video, README, honest limits section. |
| OSS contribution | Ship a Plumbline skill to datahub-skills; upstream any parser fix found along the way. |

## Deliverables

- `plumbline` CLI: `plumbline check path/to/model.sql`
- GitHub Action wrapper for the CI gate
- Markdown PR-comment report + JSON findings
- Benchmark harness and results table
- Apache 2.0 licensed public repo (required by the rules)
- Demo video under 3 minutes
