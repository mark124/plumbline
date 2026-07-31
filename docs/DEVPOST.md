# Devpost submission copy

Paste-ready. One section per field on the submission form. Nothing here
claims anything that is not in the repository or reproducible from it.

---

## Project name

**Plumbline**

## Tagline

Check AI-written data code against your catalog before it merges. The checker
does not contain a model.

## Track

Metadata-Aware Code Generation & Development

---

## Inspiration

A coding agent writes a dbt model. It compiles, it reads well, it passes
review, and it references `order_ttl` on a table where the column is
`order_total`. It joins on a key pair nobody in the company has ever joined
on. It pulls a PII-tagged column into an output table that carries no such
tag. It rewrites a table that thirty-four dashboards depend on.

None of that is visible in the diff. All of it is already written down in your
catalog.

The gap is not that the model is careless. It is that the model is writing
against its guess about your warehouse rather than against your warehouse. So
the fix is not a better prompt. It is ground truth, applied before the merge
button.

A plumb line is a carpenter's instrument: it finds true vertical by reference
to gravity, which is external and not a matter of opinion. That is the whole
design. The catalog is the gravity.

## What it does

Plumbline resolves every table and column in a SQL file against DataHub and
reports what the catalog can disprove. It runs as a CLI, a GitHub Action, and
a pull request gate, and it exits nonzero so CI can stop a merge.

Six checks:

| Check | What it catches | Severity |
| --- | --- | --- |
| Phantom column | Column referenced on a table whose schema says otherwise | Error |
| Phantom table | Table the catalog has never heard of | Unknown, or Error on a near miss |
| Deprecated source | Reading from an asset marked deprecated | Warning |
| PII propagation | PII-tagged column flowing into an untagged output | Warning |
| Unvetted join | Join key pair seen in no observed production query | Warning |
| Blast radius | Who consumes the asset this statement rewrites | Info |

**The rule that makes it usable: severity reflects the strength of the
evidence, not how alarming the problem would be if it were real.**

A missing column on a table whose schema we have is an **error**, because the
catalog can prove it, and it blocks the merge. A table the catalog has never
heard of is an **unknown**. That is not evidence of a defect; it may simply
not be ingested. It never blocks. Promoting the second to the first is how
catalog tools get switched off in week one.

When a reference is provably wrong, an agent can propose the repair. It
reaches DataHub only through the official MCP server, with mutation tools
forced off, so it can read the catalog and cannot change it. Every proposal is
re-parsed and re-checked by the deterministic layer before a human sees it.
Unverified repairs are discarded and reported as "no verified fix".

And it writes back. With `--publish`, each dataset the run reached a
conclusion about gets a DataHub assertion carrying that conclusion, with run
history, on the dataset's Validation tab. The next agent inherits what this
run proved instead of rediscovering it.

## How we built it

**Two layers, and the split is the entire idea.**

Layer 1 is deterministic: parse with sqlglot, resolve every reference through
DataHub's schema resolver and GraphQL, compare, report. There is no model in
this layer and there must never be one. A checker that can hallucinate is not
a checker. We do not fight hallucination with more hallucination.

Layer 2 is agentic, because Layer 1 can prove a reference is wrong but cannot
know what the author meant. That needs judgment and open-ended investigation
of the catalog, which is exactly what a model with tools is for. It runs on
Claude via the DataHub MCP server over stdio.

The rule binding them: **the agent proposes, the deterministic core disposes.**

The same asymmetry governs writing to the catalog. Only the deterministic
layer may publish; the agent is never handed a publishing path. **The one
component allowed to change the graph is the one that cannot hallucinate.** A
model that can invent a column must not be able to record a judgment about one.

**How it uses DataHub.** The context graph for ground truth: schemas,
column-level tags, glossary terms, deprecation aspects, lineage, and query
history. The MCP server for the agent layer, because choosing what to look up
is that layer's whole job. Assertions for the return leg. Layer 1
deliberately does not use MCP: it asks a fixed set of questions and gets facts
back, and there is no model in it to do the choosing.

## Challenges we ran into

**Our own verification gate accepted nine hostile rewrites out of nine.**

Everything had been tested on whether the checker finds bad SQL. Nobody had
asked what a *rewrite* could get past it. When we finally asked, the gate
accepted `SELECT 1`, accepted `DROP TABLE orders`, and accepted a correct fix
with `; DROP TABLE orders` appended, because the parser reads the first
statement and silently discards the rest.

The gate was not broken. It was answering a weaker question than the word
"verified" implied: re-running the catalog checks proves a rewrite is
*grounded*, not that it is the same query. `SELECT 1` resolves perfectly
against any catalog. The gate now compares shape as well as references: one
statement only, same statement kind, same source tables, same output column
count, no dropped filter, with every rule stepping aside for the identifier
under repair.

**The benchmark was not reproducible, twice.** It selected tables using
`hash(template_name)`, and Python salts string hashing per process, so the
fixed seed was doing nothing and two runs of identical code scored 100% and
98.4%. After fixing that we ran it three times, compared the headline numbers,
and called it reproducible. It was not: column order still came from the
catalog into a seeded sampler, so when a rebuild reordered the search index
the defect mix moved while every total stood still. Two runs agreeing is not
evidence about an axis you did not vary. The property is now asserted rather
than inferred, by shuffling the inputs and demanding a byte-identical case set.

**A DataHub outage made every real table look invented.** The SDK swallows
transport errors and reports the table as unresolved, which is
indistinguishable from "this table does not exist". Left alone, an outage
would emit a phantom-table finding for every real table in the file and the
run would look like a successful check. Plumbline now confirms the catalog is
answering before it believes any negative, and if it is not, produces no
report at all.

**Publishing put a red mark on an innocent dataset.** A phantom table has no
URN of its own, so the finding cites the *suggested* table as evidence.
Writing that back marked a healthy `ORDERS` as failing because `ORDRS` was
mistyped somewhere else entirely. The invariant now enforced: publish a
passing assertion only for a check whose failures can be attributed to that
same dataset.

## Accomplishments that we're proud of

**It does not cry wolf.** Zero false positives on 66 queries built from the
catalog's own schemas and valid by construction, and zero blocking errors on 6
real view definitions taken out of the catalog that this project did not
write. That is the number that decides whether a team keeps a checker
installed past week one.

**It says "I don't know" out loud.** An uningested table is reported as
unknown, does not block, and says its columns were not checked. A check that
cannot run reports that it did not run rather than passing silently. When the
catalog is unreachable, there is no report at all rather than a confident
wrong one.

**Thirty-two defects found in our own work, across nine rounds of
deliberately different exercise, plus two more in the benchmark that measures
it.** Not one came from repeating a round that already passed, which is the
only honest reason to believe the count is not finished.

**It runs against a real DataHub inside a GitHub runner.** The pull request
gate stands the platform up from scratch in about five minutes, loads the
catalog, checks the diff, and comments with the bad column, the real column,
and the URN that proves it. No hosted instance, nothing mocked.

## What we learned

**Repeating a passing test suite finds nothing.** Every defect we found came
from a new *kind* of exercise: installing from a clean clone, pointing it at
production SQL, attacking the gate, varying file encodings, hostile
identifiers, malformed catalog responses. Not one came from running an
existing suite again. "All tests pass" is a statement about the angles you
already tried.

**Test the thing that judges, not only the thing being judged.** The costliest
bug was in the component whose entire job was to catch bugs.

**Grounded and correct are different claims.** Only one of them deserves the
word verified, and the gap between them is wide enough to drive a
`DROP TABLE` through.

**A number you cannot reproduce is not a measurement**, whichever direction it
errs in, and comparing headline figures does not prove reproducibility.

## What's next for Plumbline

Rendering dbt Jinja so uncompiled models can be checked in place. Following
PII through multi-hop lineage rather than one statement at a time. Catching a
bad column introduced in a second CTE hop, which is currently a deliberate
miss because convicting an unqualified name with a computed source in scope is
how false positives start. Demonstrating the unvetted-join check against a
catalog with real query history rather than seeded queries.

---

## Try it out

**Thirty seconds, no catalog required:**

```bash
git clone https://github.com/mark124/plumbline
cd plumbline
pip install -e .
plumbline check examples --demo
```

`--demo` reads a snapshot of the public `showcase-ecommerce` catalog that
ships with the repository. Same checker, same six checks, same report; only
the source of the facts changes. Nothing is simulated, and every run states
that the catalog is frozen.

**See it block a real pull request:**
https://github.com/mark124/plumbline/pull/1

Unit tests green, catalog gate red, with a bot comment naming the bad column,
the real column, and the catalog URN.

**Sample outputs** in all four formats: `docs/samples/`

## Repository

https://github.com/mark124/plumbline (Apache-2.0)

## Open-source contributions to DataHub

Three pull requests, all open and under review. None merged, none reviewed
yet, stated plainly rather than optimistically.

- **datahub#18634**: `datahub datapack load` is unusable on Windows.
  `get_path_schema` parses the path with `urlparse`, so `C:\...` yields the
  scheme `"c"` and the filesystem registry lookup raises `KeyError`. Fix plus
  tests.
- **datahub#18635**: `datahub datapack --help` raises `FileNotFoundError`
  because the package ships without
  `cli/datapack/resources/DATAPACK_AGENT_CONTEXT.md`. Verified by removing and
  restoring the file.
- **datahub-skills#57**: a new `datahub-sql-review` Skill. It teaches a coding
  agent to check its own SQL against the catalog before proposing it: resolve
  every table and column, read column-level tags and deprecation, and say
  plainly when the catalog cannot answer instead of guessing. It is
  Plumbline's central idea packaged to work inside any agent using DataHub
  Skills, with no need to install Plumbline at all.

## Built With

`python` `datahub` `datahub-mcp-server` `datahub-skills` `claude` `anthropic`
`model-context-protocol` `sqlglot` `graphql` `github-actions` `snowflake`
`sql` `dbt` `pytest`

---

## Notes for whoever fills the form

- The description field should open with the tagline and the severity rule.
  If a judge reads two sentences, those are the two.
- Do not lead with "100% recall". It is a self-graded exam: defects we
  injected, into queries we generated, of exactly the kinds we target. It is
  in the README with that caveat attached and it belongs nowhere near the
  headline.
- The video is under 3 minutes and ends on the red pull request gate.
- Sample outputs are recommended by the rules and are in `docs/samples/`.
