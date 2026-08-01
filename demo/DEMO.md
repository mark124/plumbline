# Demo script

A shot list for the submission video. Target is under 3 minutes, which is the
hackathon limit. Every command below has been run and produces the output
shown, so nothing needs to be faked or retaken for correctness.

## Before you record

**Run this first. It checks every shot below and tells you what is missing.**

```bash
python demo/preflight.py
```

It runs each shot's real command against the live catalog and reports
`ready` or `gap` per shot, with the remedy. Exits 0 when the whole list can
be recorded. Discovering mid-take that the catalog is down, or that the shot
needing an API key cannot run, costs a session.

If it reports gaps, the setup is:

```bash
# 1. DataHub up with the showcase catalog
datahub docker quickstart
datahub datapack load showcase-ecommerce

# 2. The two conditions the datapack lacks (deprecation + query history)
python demo/seed_demo_catalog.py --server $DATAHUB_GMS_URL

# 3. Point Plumbline at it. On this machine GMS is on 8081, not the
#    default 8080, because another service holds 8080.
export DATAHUB_GMS_URL=http://localhost:8081

# 4. Shot 4 only. Without this the agent shot cannot be recorded.
export ANTHROPIC_API_KEY=...
```

Two things that will waste your time if you do not know them:

**`datahub docker quickstart` reports failure on Windows even when it
succeeds.** It brings the whole stack up and then dies printing its own
success banner, because a tick character cannot be encoded when stdout is
redirected. Check container health, never the exit code.

**Every command in this script assumes `DATAHUB_GMS_URL` is exported.**
Without it you get a connection error rather than a report, on camera.

Terminal: widen it to about 110 columns so the findings do not wrap, and turn
the font up. The output is the product; it has to be readable at 1080p.

`--fix` takes 24 to 34 seconds. Let it run and cut the dead time in the edit
rather than trying to fill it.

---

## Shot 1: the problem (about 20 seconds)

Show `examples/order_details_rebuild.sql` in an editor.

> "A coding agent wrote this model. It compiles, it passes review, and it is
> wrong in three ways. None of them are visible in the diff."

Do not explain the three ways. Let the next shot do it.

## Shot 2: the check (about 35 seconds)

```bash
plumbline check examples/order_details_rebuild.sql \
  --platform snowflake --platform-instance b2fd91
```

Three findings appear. Point at them in this order:

1. `order_statu` does not exist, and the catalog knows the real column is
   `order_status`.
2. The source table is **deprecated**. The query works; the organisation has
   said not to build on it.
3. The target has **34 downstream consumers**, including three dashboards by
   name.

> "Every one of those came from the catalog, not from a model guessing."

## Shot 3: the honesty rule (about 25 seconds)

This is the shot that separates it from a linter that cries wolf.

```bash
plumbline check examples/uningested_table.sql \
  --platform snowflake --platform-instance b2fd91
echo "exit code: $?"
```

One **unknown**, and **exit code 0**.

> "SHIPMENTS is not in the catalog. That might mean the agent invented it, or
> that nobody has ingested it yet. It cannot tell, so it says so and does not
> fail your build. Severity tracks the evidence, not the alarm."


## Shot 4: the agent repairs it (about 30 seconds)

```bash
plumbline check examples/customer_revenue.sql \
  --platform snowflake --platform-instance b2fd91 --fix
```

Takes 24 to 34 seconds. Cut the wait in the edit rather than filling it.

The agent reaches DataHub through the official MCP server, confirms the real
column with a tool call, and returns a rewrite. Show the accepted fix.

> "It did not guess `credit_limit`. It asked the catalog, through DataHub's own
> MCP server, with the write tools switched off."

## Shot 5: what the gate throws away (about 20 seconds)

Do not run anything. Show this on screen as text:

```
rejected: the rewrite is not the same query, it contains 2 statements
          and a repair must be exactly one
rejected: the rewrite is not the same query, it is a DROP statement
          where the original was a SELECT
```

> "Nine hostile rewrites were put to this gate while it was being built. It
> accepted all nine. Re-running the catalog checks proved a rewrite was
> grounded, and grounded is a weaker claim than verified. It now compares the
> shape too."

This is the most credible thing in the video. Say it plainly and move on.

## Shot 6: it writes the verdict back (about 25 seconds)

```bash
plumbline check models/ --platform-instance b2fd91 --publish
```

Then switch to the DataHub UI, open the dataset, and go to the **Validation**
tab. The assertions are there with their run history.

> "It reads the catalog to find out what is true, and writes back what it
> concluded. The next agent inherits it. Only the deterministic layer can
> write: the one component allowed to change the catalog is the one that
> cannot hallucinate."

## Shot 7: it blocks a real pull request (about 30 seconds)

End here. This is the shot that makes a data lead think "I would merge this".

Open https://github.com/mark124/plumbline/pull/1 in a browser.

- Unit tests green: 3.10, 3.11, 3.12.
- The catalog gate **red**.
- The bot comment naming the bad column, the file and line, the real column,
  and the catalog URN that proves it.
- Scroll to the bottom: **"Merging is blocked"**, with the merge button
  disabled.

> "That gate stood a real DataHub up inside the GitHub runner, loaded the
> catalog, and checked the diff against it. No hosted instance, nothing
> mocked."

Let the red X and the disabled merge button sit on screen together for a beat
before cutting. That pairing is the whole argument.

`master` has branch protection requiring the `gate` check, which is what makes
the button disabled rather than merely warned about. Before it was added the
page showed a red X next to a live merge button, which would have contradicted
the narration in the same frame. Admin bypass is on, so you can still merge
your own work when you need to.

---

## Timing

| Shot | Length |
| --- | --- |
| 1. The problem | 0:20 |
| 2. The check | 0:35 |
| 3. The honesty rule | 0:25 |
| 4. The agent repairs it | 0:30 |
| 5. What the gate throws away | 0:20 |
| 6. It writes the verdict back | 0:25 |
| 7. It blocks a real pull request | 0:30 |
| **Total** | **2:45** |

Fifteen seconds of headroom against the three-minute limit. If a shot runs
long, cut shot 5 to a single sentence over shot 4's output. Do not cut shot 3.
It is the only one that argues the tool will not waste your time, and every
other checker in this category will claim precision without demonstrating
restraint.

## Deliberately not in the video

**The benchmark numbers.** They belong in the written description, where a
reader can weigh the caveats. Reciting "100% recall" to camera invites exactly
the scepticism it deserves, since we injected those defects ourselves.

**"Beat the gate" as an interactive segment.** It films badly, because it is
just someone typing. It belongs in the hosted demo where the viewer does the
typing.

**The HTML report.** Nice to look at, but it competes with shot 7 for the same
job and shot 7 is stronger. Put a screenshot in the write-up instead.

## Recording without a live catalog

The demo path needs no catalog at all, so if the local DataHub is unavailable
every shot except 6 and 7 can be recorded with `--demo` instead of `--server`:

```bash
plumbline check examples --demo
```

Shot 6 needs a live catalog, because it writes to it.
