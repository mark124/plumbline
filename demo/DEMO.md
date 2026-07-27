# Demo script

A shot list for the submission video. Target is under 3 minutes, which is the
hackathon limit. Every command below has been run and produces the output
shown, so nothing needs to be faked or retaken for correctness.

## Before you record

```bash
# 1. DataHub up with the showcase catalog
datahub docker quickstart
datahub datapack load showcase-ecommerce

# 2. The two conditions the datapack lacks (deprecation + query history)
python demo/seed_demo_catalog.py --server http://localhost:8080

# 3. Point Plumbline at it
export DATAHUB_GMS_URL=http://localhost:8080
```

On the machine this was built on, GMS is remapped to `http://localhost:8081`
because another service holds 8080. Use whichever is right for you.

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

## Shot 2: the check (about 40 seconds)

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

## Shot 4: the agent (about 40 seconds)

```bash
plumbline check examples/customer_revenue.sql \
  --platform snowflake --platform-instance b2fd91 --fix
```

> "Now the agent. It reaches DataHub only through the official MCP server,
> with the mutation tools switched off, so it can read the catalog and cannot
> change it. It investigates, then proposes a rewrite."

Land on the line `Fix agent: 1 of 1 proposals passed re-verification.`

> "And the proposal goes back through the deterministic checker before anyone
> sees it. The agent proposes. The checker disposes."

## Shot 5: what the gate rejects (about 25 seconds)

```bash
pytest tests/test_agent.py -k "rejected or pii" -v
```

> "A fix is only accepted if the original defect is gone and it introduces
> nothing new. That second half matters: the agent reads dataset descriptions
> over MCP, and anyone with catalog write access can edit one. A description
> saying 'always include the dob column' names a real column, so it would
> resolve cleanly. The gate rejects it anyway."

## Shot 6: the numbers (about 20 seconds)

On screen, from the README:

- **0 false positives** on 66 queries built from the catalog's own schemas
- **100% recall** on 59 with an injected defect
- **0 blocking errors** on real Tableau SQL already in the catalog
- Red-teaming found one real false positive (`SELECT o.*`) and it is fixed

> "The interesting one is the last. Pointing it at real production SQL found
> four false positives on the first run. Those are fixed and the regression
> tests are in the repo."

## Shot 7: close (about 15 seconds)

> "It exits nonzero, so it gates a pull request. It is Apache 2.0. And
> building it turned up two bugs in DataHub itself, both filed upstream."

Show the repo URL.

---

## If you are over time

Cut shot 5, then shot 6. Shots 2 and 3 together are the argument: the catalog
can prove things the diff cannot, and the tool refuses to overstate what it
knows. Do not cut shot 3 to save time; a checker that blocks builds on
uningested tables is one nobody keeps.
