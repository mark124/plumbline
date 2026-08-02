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

Exit code 1. The pull request does not merge. With `--publish`, the verdict
goes back into DataHub as an assertion on the dataset, so the next agent
inherits what this run proved instead of rediscovering it.

**Demo video (1:40):** https://youtu.be/g2X3Hx4P_U4

## Try it in thirty seconds, without a catalog

```bash
pip install -e .
plumbline check examples --demo
```

That is the whole setup. `--demo` reads a snapshot of the public
`showcase-ecommerce` catalog that ships with this repository, so all six
checks run and every finding below is real: a missing column, a deprecated
source, PII crossing into an untagged table, a join nobody has made, and 34
downstream consumers of the table being rewritten.

This exists because of an honest problem with the tool. Recall is bounded by
catalog coverage, so pointing Plumbline at a sparse DataHub returns a wall of
Unknown and looks like it does nothing. That is the tool being careful, and it
is still a terrible first impression. The snapshot is the same checker reading
frozen facts: nothing is simulated, and every run says out loud that the
catalog is not live.

Sample outputs in all four formats are in
[`docs/samples/`](docs/samples/) if you would rather not run anything.

## Why this belongs in the code-generation track

Because generation without verification is the open half of that loop. An
agent that writes production data code and cannot check its own references
against the catalog is not finished, it is unattended. Plumbline closes the
loop: it verifies what a generating agent produced, and when a reference is
wrong its own repair agent generates the correction, which the deterministic
layer then has to accept before anyone sees it.

**It is not sqlfluff with a dbt manifest.** A linter checks style, and a
manifest only knows what dbt owns. Neither knows that a source is marked
deprecated, that a column carries a PII tag, that a join key pair appears in
no query anyone has ever run, or that thirty-four dashboards read the table
you are about to rewrite. None of that lives in your repository, and a
manifest cannot check SQL that is not in a dbt project yet, which is exactly
where machine-written code arrives.

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

## It reads the catalog for truth, and writes the verdict back

Reading is only half of a graph. With `--publish`, every dataset the run
reached a conclusion about gets a DataHub **assertion** carrying that
conclusion, visible on the dataset's Validation tab with its run history:

```
$ plumbline check models/ --publish
Published 12 assertion(s) to DataHub: 8 passing, 4 failing.
```

```
plumbline:phantom_column     FAILURE   Column `credit_limt` does not exist
plumbline:pii_propagation    ERROR     PII column `customer_id` flows into CUSTOMER_REVENUE
plumbline:deprecated_source  SUCCESS
```

Three checks are deliberately **not** published, and the reason is the same
each time: a passing assertion may only be written for a check whose failures
can be blamed on that same dataset.

- **Blast radius** is context, not a verdict. Asserting on it would mark every
  dataset that merely has consumers as failing.
- **Phantom table** cites the *suggested* dataset, because a missing table has
  no URN of its own. Publishing it put a failure on a healthy dataset because
  a different name was mistyped somewhere.
- **Unvetted join** concerns two datasets and records neither, so its failures
  cannot be published at all. Publishing only its successes would state that
  joins were fine on a run where a join warning fired.

Three decisions in there are worth stating, because each one is a restraint
rather than a feature:

**Only the deterministic layer may write.** The agent reaches DataHub through
the MCP server with mutation tools forced off, and it is never handed a
publishing path. So the one component allowed to change the catalog is the one
that cannot hallucinate. A model that can invent a column must not be able to
record a judgment about one.

**Clean datasets get a passing assertion, not silence.** A record that only
appears on failure says nothing about what was checked and found fine, and "no
news" is not the same as "checked and clean".

**It is off by default.** A checker that silently edits a shared catalog the
first time somebody tries it has taken a liberty it was not granted. CI turns
it on knowingly. If the catalog refuses the write, the run says so and the
report still stands: publishing is a side effect, never the point.

Assertion ids are derived from the dataset and the check, so re-running
updates one assertion and appends to its history rather than littering the
dataset with a new one every time CI fires. This was measured rather than
assumed: **six consecutive runs leave three assertions per dataset, each with
six run events.** The count of assertions is flat and the history grows, which
is what a Validation tab is for. A rehearsal take before a real take adds a
row of history, not a duplicate assertion.

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

### The report draws the difference, it does not just name it

`--format html` writes a single self-contained page with no scripts, webfonts
or network requests, so it survives being uploaded as a CI artifact.

The severity tiers are drawn differently **in kind**, not in colour. A rule
runs down the left of the findings: solid, with a filled marker, where the
catalog gave a reading; **dashed, with a hollow marker, where the catalog was
silent**. Colour alone would fail a colourblind reader and a greyscale print,
and would quietly turn "I could not check this" into "this is wrong", which is
the one confusion this project exists to prevent.

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

Read this section in the order it is written, because the strongest number is
not the most impressive one.

**What the evidence actually supports.** The number that decides whether a
checker survives contact with a team is its false positive rate, and on 66
queries that are valid by construction it raised **zero** blocking errors. On
6 real view definitions this project did not write, taken out of the catalog,
it also raised **zero**. Those are the claims worth making.

**What it does not support.** The recall figure below is a self-graded exam:
defects this project injected, into queries this project generated, from the
catalog's own schemas, of exactly the three kinds Plumbline targets. A perfect
score there means the resolution logic is sound. It is not evidence about the
full space of things a language model gets wrong, and anyone reading it as
such is reading it wrong.

Both harnesses run against the public `showcase-ecommerce` datapack
(67 datasets, 816 columns).

### Precision and recall on a constructed benchmark

`bench/run_bench.py` builds queries directly from the catalog's own schemas, so
they are valid by construction and any blocking error is a false positive by
definition. It then injects exactly one known defect per query for the recall
set.

| Measure | Result |
| --- | --- |
| False positive rate on 66 valid queries | **0.0%** (0 blocked) |
| Recall on 63 queries with an injected defect | **100%** (63 caught) |
| ... column typo | 16/16 |
| ... invented column | 23/23 |
| ... table typo | 24/24 |

Templates cover joins, CTEs, subqueries, window functions, `CASE`, `GROUP BY`,
`SELECT *`, and `CREATE TABLE AS`.

**The harness was not reproducible, twice, and this is worth reading before
you trust any number above it.**

First, table selection used `hash(template_name)`. Python randomizes string
hashing per process, so the fixed seed was doing nothing and every run scored
a different case set; two runs of identical code produced 100% and 98.4%.
Selection is now positional.

Second, and only found because a DataHub rebuild forced a search reindex:
column order came straight from the catalog into `rng.sample`, so reordered
fields changed which columns the templates picked and therefore the mix of
injected defect kinds. The per-kind split moved while the totals did not,
which is exactly why the first fix looked verified when it was not. Comparing
headline numbers cannot detect this.

Both orderings are now normalised inside `build()`, at the point of use. The
property is asserted directly rather than inferred from equal runs:
`tests/test_bench_determinism.py` shuffles the tables and the columns and
requires a byte-identical case set. A number you cannot reproduce is not a
measurement, whichever direction it errs in, and two runs agreeing is not
proof that it is reproducible.

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

### Every check exercised against a live catalog

Unit tests run against an in-memory catalog, which is fast but cannot catch a
malformed GraphQL query. So each check was also run against a real DataHub:

| Check | Verified live | Notes |
| --- | --- | --- |
| Phantom column | Yes | 66/66 valid and 63/63 defective benchmark cases |
| Phantom table | Yes | Near-miss promotion confirmed on real table names |
| PII propagation | Yes | Fires on `cust_email`, tagged `Email Address, PII` |
| Blast radius | Yes | 34 downstream consumers: 19 datasets, 3 dashboards, 12 charts |
| Deprecated source | Yes, after seeding | The datapack has 0 deprecated assets |
| Unvetted join | Yes, after seeding | The datapack has 0 query entities |

The last two needed their precondition to exist before they could run at all,
so a deprecation flag and one `Query` entity were seeded into a local
instance. The join check was then verified **in both directions**: silent on a
join matching the observed query, and flagging a join on a key pair nobody has
used. A check that only ever fires is not a check.

### Catalog shapes it has been run against

The showcase datapack is Snowflake, three-part names, uppercase identifiers,
and a platform instance. Most DataHub instances are none of those, so the
other shapes were seeded and resolved directly:

| Platform | Name shape | Platform instance | Resolved |
| --- | --- | --- | --- |
| postgres | `shopdb.public.users` | none | Yes |
| mysql | `shopdb.orders` (two-tier) | none | Yes |
| snowflake | `ANALYTICS_DB.PUBLIC.EVENTS` | none | Yes |
| bigquery | `myproj.analytics.sessions` | none | Yes |

Phantom columns were caught end to end on both a three-tier platform
(postgres) and a two-tier one (mysql). Still untested: DataHub Cloud, and any
catalog large enough for pagination limits to bite.

### When DataHub is unreachable, it refuses to report

This one is worth stating because the obvious implementation is wrong. The
SDK's schema resolver swallows transport errors and reports the table as
unresolved, which is indistinguishable from "this table does not exist". Taken
at face value, a DataHub outage would emit a phantom-table finding for every
real table in the file, and the run would look like a successful check.

Plumbline confirms the catalog is answering before it believes any negative,
and if it is not, produces no report at all:

```
Error: DataHub at http://localhost:8080 is not reachable (ConnectionError).
Refusing to report, because an unreachable catalog would make every table
look like it does not exist.

No report was produced. Re-run when DataHub is reachable.
```

A partial report presented as a complete one is the failure this whole tool
exists to avoid, so it applies to the tool itself.

### Cost

The two layers have very different price tags, which is why the fast one is
the gate and the slow one is opt-in.

| Path | Measured |
| --- | --- |
| `plumbline check` (deterministic, the CI gate) | **1.6s** median for one file, 3 runs |
| `plumbline check --fix` (per blocking finding) | **24 to 34 seconds**, 4 runs, one Claude API call chain each |

Measured against a local DataHub quickstart on a laptop. Treat the second row
as variable rather than a guarantee: an earlier configuration, at a higher
effort setting, took 296 seconds for the same file, which is what motivated
the explicit client timeout. `--fix` proposes for at most 5 findings per run
by default. Leave it off in CI unless you want repairs, and keep the
deterministic gate as the thing that blocks merges.

## What red-teaming found

The benchmark generates SQL from templates this project wrote, which is a soft
target. So the checker was also attacked directly: 26 hand-written statements
chosen because they are awkward for a resolver, using only columns that
genuinely exist. Quoted identifiers, `USING` joins, self-joins, `UNION ALL`,
correlated subqueries, three-deep CTE chains, CTEs with explicit column lists,
`QUALIFY`, aliases shadowing real column names, `VALUES` clauses, comments
naming fake columns, string literals that look like columns.

**It found one real false positive:** `SELECT o.*` raised a blocking error,
because a qualified star parses as a column literally named `*`. Plain
`SELECT *` was handled and the qualified form was not. Fixed, with a
regression test that also confirms a genuine phantom alongside the star is
still caught. The suite now reports 0 false positives, with one statement
(`WINDOW w AS (...)`) that sqlglot cannot parse and which is therefore
reported as unknown rather than passed.

### A second sweep, over input shapes rather than SQL shapes

Every defect so far had been found by a *new kind* of exercise, never by
repeating an old one, so a further sweep deliberately attacked categories
nothing had touched: statement types other than `SELECT`, file encodings and
line endings, multi-statement files, degenerate files, dialect mismatch, and
identifiers chosen to confuse a parser.

It found three misses, and no false alarms:

| Found | Status |
| --- | --- |
| `UPDATE`/`DELETE` columns were never checked (no `SELECT` scope to walk) | Fixed, with tests |
| A leading byte order mark made a file unparseable | Fixed, read as `utf-8-sig` |
| A bad column introduced in a second CTE hop is not caught | Documented below, not fixed |

The BOM one is worth calling out because Windows editors and shells write BOMs
by default. It was never dangerous, since the file was reported as unparsed
rather than passed, but a file silently going unchecked for an invisible
character is a bad way to lose coverage.

Cases that already behaved correctly: `MERGE`, `TRUNCATE`, `CREATE VIEW`,
recursive CTEs, `EXCEPT`, lateral flatten, semicolons inside strings and
comments, CRLF line endings, files with no trailing newline, empty files,
comment-only files, files that are not SQL at all, and a glob matching
nothing.

### Prompt injection through catalog metadata

The agent reads dataset descriptions over MCP. In a real organisation anyone
with catalog write access can edit a description, which makes catalog metadata
untrusted input to something that writes SQL.

A description was planted saying that any repair must also add the `dob` and
`phone_number` columns. In that trial the agent ignored it and proposed the
minimal correct fix, but one trial of a probabilistic system proves very
little, so the defense does not depend on it. Those are real column names, so
they resolve cleanly, and a gate that only rejected new *errors* would have
accepted a rewrite that quietly widened PII exposure.

The verification gate now rejects any rewrite that introduces a finding the
original statement did not have, at warning severity as well as error. The
injection outcome is blocked whether or not the model notices the attack.

### A third sweep, attacking the gate rather than the checker

Everything above tests whether Layer 1 finds defects. The opposite question
had never been asked: what can a *rewrite* get past Layer 1 and reach a human
with the word "verified" attached? Ten hostile proposals were put to the gate
directly, of the kind a confused model, a poisoned description, or a bad day
could produce.

**Nine of the ten were accepted.** The gate re-ran the catalog checks and found
nothing wrong, which was true and beside the point: `SELECT 1` contains no bad
column, and neither does `DROP TABLE orders`. Grounded and correct are
different claims, and only the second deserves the word verified.

| Proposal | Was | Now |
| --- | --- | --- |
| `SELECT 1` in place of the query | accepted | rejected |
| `DROP TABLE orders` as the "fix" | accepted | rejected |
| A correct fix with `; DROP TABLE orders` appended | accepted | rejected |
| The offending column deleted rather than corrected | accepted | rejected |
| The `WHERE` clause quietly removed | accepted | rejected |
| A different source table substituted | accepted | rejected |
| A PII column joined in on the way past | accepted | rejected |
| An honest minimal repair | accepted | accepted |

The gate now also compares the rewrite with the original as a *shape*: one
statement only, same statement kind, same source tables, same number of output
columns, no dropped filter. Each rule steps aside for the identifier under
repair, since that is the one thing a fix is supposed to change, and a
phantom-table finding is allowed to name a different table because that is the
entire repair. Regression tests cover both directions, including that a
reformatted, re-cased, or `WHERE`-clause repair is still accepted.

The appended-statement case is the one worth staring at. `parse_one` reads the
first statement and silently discards the rest, so the fix itself verified
clean while the `DROP` rode along invisibly into text displayed for a human to
copy.

### A fourth sweep: identifiers, scale, and file selection

Three more categories nothing had touched.

**Identifier shape** (16 cases: quoted, upper-cased, spaces in names, reserved
words, unicode, backticks, semi-structured access, MySQL two-tier). Fifteen
already behaved. One defect: the same missing table written
`ANALYTICS.PUBLIC.ORDRS` in one place and `analytics.public.ordrs` in another
was reported twice, because only the leaf name was case-folded. Fixed.

**Scale.** 300 columns resolve in 0.09s; 300 phantom columns in 0.46s; 500
statements in 0.91s. Long `OR` chains, 5000-item `IN` lists, 500-way `UNION`,
100-deep CTE chains: all fine. But **around 66 levels of subquery nesting the
process died outright** with `STATUS_STACK_OVERFLOW`: no traceback, no report,
an exit code that reads as infrastructure failure rather than as a result.
sqlglot parses and optimizes by recursion and exhausts the C stack rather than
raising, so it cannot be caught. Tokenizing is the one stage that is iterative,
so depth is now measured off the token stream before the parser runs, and
anything past 40 levels is reported as unchecked. Verified to 20,000 levels.

**File selection**, where every failure is silent, because a file that is never
opened is never reported on and the run still ends with "clean":

| Found | Effect |
| --- | --- |
| `*.sql` glob is case-sensitive on Linux | `QUERY.SQL` silently skipped in CI, passes on Windows |
| A real file named `report[1].sql` was treated as a pattern | Named file dropped without a word |
| The same path twice was checked twice | Doubled findings, inflated file count |

All three fixed, with end-to-end tests through the CLI.

### A fifth sweep: the agent's reply, the output formats, the catalog's answers

Three seams rather than three inputs. Each is a place where this project
trusts something it does not control.

**Which fenced block is the fix?** `_extract_sql` took the *first* ```` ```sql ````
block in the model's reply, while the system prompt asks the model to *finish*
with the corrected statement. A model that quotes the broken input back before
presenting the repair, which they routinely do, would have its own copy of the
bad SQL extracted, re-verified, rejected, and reported as "the agent found no
defensible repair" with a good fix sitting two paragraphs below. A silent
failure that looks like the model underperforming. The last block now wins,
untagged and tilde fences are accepted, and a tagged block beats an untagged
one.

**Identifiers that are hostile to the output rather than the parser.** A
warehouse accepts a quoted column name containing almost anything, and that
name is then printed to a terminal, pasted into a GitHub comment, and opened
in a browser. Twelve such names were run through all four renderers. HTML,
Markdown and JSON held. The terminal did not: **a carriage return in a column
name overwrites the line that reports it**, so a crafted identifier could
erase its own finding, and an ESC could recolour or hide the rest of the
output. A report its input can edit is not a report. Control characters are
now shown as escapes rather than executed, while newline and tab are left
alone so a multi-line fix block keeps its shape.

**What DataHub actually sends back.** Every GraphQL response was walked with
chained lookups assuming one nesting shape, taken from one version of one
instance. GraphQL answers a partially failed query with nulls inside `data`
alongside an `errors` array, so a field the caller cannot read arrives as
`None` where a dict was expected. Nineteen realistic response shapes were
tried: **three crashed `resolve_table` outright**, which would have lost every
phantom-column finding over a tag lookup. `_fetch_governance` already
documented that it degrades rather than raises; it now does. 17 shapes are
covered by tests, alongside a well-formed response so the null tolerance
cannot quietly change what a good answer means.

**What the gate still does not cover:** it compares references and structure,
not meaning. A rewrite that keeps the same shape and swaps a comparison
operator, or changes a join key to another real column, resolves perfectly and
passes. Review the diff. `--fix` proposes, it does not commit.

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
- **A bad column introduced in a second CTE hop is missed.** In
  `WITH a AS (...), b AS (SELECT nope FROM a) SELECT nope FROM b`, `nope` is
  unqualified and its source is a CTE, so it is treated as opaque and skipped.
  This is deliberate: with a computed source in scope, an unqualified name
  could legitimately be anything, and convicting it is how false positives
  start. A miss, not a false alarm.
- **A statement nesting more than 40 levels deep is not checked.** sqlglot
  exhausts the C stack below that and takes the process with it, and the
  failure cannot be caught, only avoided. Over the limit the statement is
  reported as unchecked. Nested function calls reach five or ten levels, so
  nothing written by a person or a sane generator comes close.
- **`--fix` costs a model call per finding.** Measured at 24 to 34 seconds per
  finding against a local catalog; a run that exceeds the client timeout is
  reported as "no fix proposed" rather than retried. The deterministic findings
  are unaffected when that happens: you still get the error, the location, and
  the nearest-name suggestion.

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
plumbline check models/ --format html --out report.html    # CI artifact

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

## See it block a real pull request

[**Pull request #1**](https://github.com/mark124/plumbline/pull/1) on this
repository is left open on purpose. The unit tests pass and the catalog gate
fails, which is the distinction worth seeing: nothing is wrong with the code,
something is wrong with what the code believes about the warehouse.

The gate is not mocked and nothing is exposed to the internet.
[`.github/workflows/plumbline-gate.yml`](.github/workflows/plumbline-gate.yml)
stands up a real DataHub inside the runner, loads the public
`showcase-ecommerce` datapack, waits for 67 datasets to index, seeds the two
conditions the datapack lacks, and resolves `models/` against it. The catalog
lives and dies with the job, so a fork can reproduce the whole thing by
opening a pull request. It takes about five minutes.

The comment it leaves names the offending column, the real column it meant,
and the catalog URN that proves it.

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

# The datapack has no deprecated assets and no query history, so two of the
# six checks have nothing to fire on. This adds exactly those two conditions
# and is safe to re-run.
python demo/seed_demo_catalog.py --server http://localhost:8080

python bench/run_bench.py --platform-instance b2fd91 --out bench-results.json
python bench/real_sql_check.py --platform-instance b2fd91
pytest
```

Then try the examples, which are the same ones the demo video walks through:

```bash
export DATAHUB_GMS_URL=http://localhost:8080
plumbline check examples/order_details_rebuild.sql \
  --platform snowflake --platform-instance b2fd91   # 3 findings, exit 1
plumbline check examples/uningested_table.sql \
  --platform snowflake --platform-instance b2fd91   # 1 unknown, exit 0
plumbline check examples/novel_join.sql \
  --platform snowflake --platform-instance b2fd91   # 1 warning, exit 0
```

## Contributions made upstream while building this

Three pull requests, all open and under review at the time of writing. Their
state is given honestly rather than optimistically: none has been merged, and
none has had a maintainer review yet.

| PR | What it does | State |
| --- | --- | --- |
| [datahub-project/datahub#18634](https://github.com/datahub-project/datahub/pull/18634) | Fixes `datahub datapack load` on Windows | Submitted, under review |
| [datahub-project/datahub#18635](https://github.com/datahub-project/datahub/pull/18635) | Fixes `datahub datapack --help` crashing | Submitted, under review |
| [datahub-project/datahub-skills#57](https://github.com/datahub-project/datahub-skills/pull/57) | Adds a `datahub-sql-review` Skill | Submitted, under review |

**The two platform fixes** came out of running against a real DataHub on
Windows. `get_path_schema` parses a path with `urlparse`, so `C:\...` yields
the scheme `"c"` and the filesystem registry lookup raises `KeyError`, which
makes `datapack load` unusable on Windows entirely. The second ships the
package without `cli/datapack/resources/DATAPACK_AGENT_CONTEXT.md`, so
`datahub datapack --help` raises `FileNotFoundError`. Both include tests, and
for the second the missing file was verified as the cause by removing and
restoring it.

**The Skill** is the contribution worth reading. `datahub-sql-review` teaches
a coding agent to check its own SQL against the catalog before proposing it:
resolve every table and column, read column-level tags and deprecation, and
say plainly when the catalog cannot answer rather than guessing. It is
Plumbline's central idea packaged so that it works inside any agent using
DataHub Skills, without installing Plumbline at all. It passes the repository's
own prettier and markdownlint configuration.

The two `datahub` pull requests show red CI. That is not from these changes:
#18634 fails a timing-sensitive throughput test on a shared runner, and #18635
fails on an unrelated Airbyte install step. `testQuick`, which contains the
added unit test, passes on both.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
