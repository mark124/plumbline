# Screenshots

Captured from a live run against a real DataHub, not mocked up. They are the
two shots that need something other than a terminal, and they are also the
gallery images for the submission.

| File | What it shows |
| --- | --- |
| `pr-gate-comment.jpg` | The bot comment on a real pull request |
| `pr-gate-blocked.jpg` | The gate marked Required and failing, merge disabled |
| `datahub-quality-summary.jpg` | Plumbline's verdicts on the dataset's Quality tab |
| `datahub-assertions.jpg` | The assertions list, each check stated as a claim |

## What to look at

**`pr-gate-comment.jpg`** names the bad column, the file and line, the real
column, and the catalog URN that proves it. Every part of that came from
DataHub rather than from a model guessing.

**`pr-gate-blocked.jpg`** is the pairing that matters: the catalog gate red and
**Required**, the unit tests green on three Python versions, and the merge
button dead. `master` requires the `gate` check, so the red X has consequences
rather than being advisory. The visible "bypass rules" checkbox is the admin
override, left on deliberately so the repository owner is never locked out.

**`datahub-quality-summary.jpg`** is the write-back. Plumbline's conclusions sit
in DataHub's own Quality tab, beside the dataset's owners, its domain, its data
product, and its glossary terms. The verdict is catalog metadata, not a line in
a CI log, so the next agent to look at this table inherits it.

**`datahub-assertions.jpg`** shows the filter chips reading
`Failing (1)  Error (1)  Passing (1)`, and each assertion phrased as a claim:

- Every column referenced against this dataset exists in its schema (failing)
- No PII-tagged column from this dataset flows into an untagged output (error)
- No new code reads this dataset while it is marked deprecated (passing)

The passing one is the point. A record that only ever appears on failure says
nothing about what was checked and found fine, and "no news" is not the same as
"checked and clean".

## Reproducing them

```bash
plumbline check examples --publish        # then open the dataset's Quality tab
```

The pull request is public: https://github.com/mark124/plumbline/pull/1
