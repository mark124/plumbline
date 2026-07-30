# Sample outputs

What Plumbline actually produces, so you can see the shape of a report without
running anything.

All four were generated from the same command against the four files in
`examples/`, using the bundled catalog snapshot rather than a live DataHub, so
you can reproduce them exactly:

```bash
plumbline check examples --demo --format html --out report.html
```

| File | Format | What it is for |
| --- | --- | --- |
| `report.txt` | text | What you see in a terminal |
| `report.md` | markdown | Posted as a pull request comment by the Action |
| `report.json` | json | For a machine: stable check ids, severities, URNs |
| `report.html` | html | Self-contained, for a CI artifact or a network share |

The run finds 2 errors, 4 warnings, 1 unknown and 1 info, and exits 1.

Two things in there are worth looking at specifically, because they are the
argument rather than the feature:

**The unknown.** `examples/uningested_table.sql` references a table the catalog
has never heard of. That is reported as **unknown**, not as an error, and it
does **not** block the merge. The catalog cannot tell the difference between a
hallucinated table and one nobody has ingested, so neither will Plumbline.

**The blast radius.** Rewriting `ORDER_DETAILS` is reported as touching 34
downstream consumers, named. That is not a defect, it is the thing you want to
know before you approve the change.

## Not included

A `--fix` sample, showing an agent-proposed repair that passed re-verification.
Producing one honestly requires a live catalog and a real model call, and
hand-writing it would make it a mock-up of evidence rather than evidence. Run
it yourself against your own catalog:

```bash
plumbline check models/ --server http://localhost:8080 --fix
```
