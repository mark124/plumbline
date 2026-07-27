-- The case that decides whether a tool like this is usable.
--
-- SHIPMENTS is not in the catalog. That could mean the agent invented it, or
-- it could mean nobody has ingested it yet. Plumbline cannot tell the
-- difference, so it says so and does not fail the build.
--
-- Exit code 0. A partially ingested warehouse must not produce a wall of
-- false accusations.
SELECT
    shipment_id,
    carrier,
    delivered_at
FROM ORDER_ENTRY_DB.ORDER_ENTRY.SHIPMENTS
