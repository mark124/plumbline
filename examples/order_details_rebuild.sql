-- Written by a coding agent from the prompt:
--   "Rebuild the order details table from the replica, keeping the status."
--
-- Nothing here is a syntax error. The catalog knows three things the agent
-- did not: the source is deprecated, one column name is a typo, and the
-- target is load-bearing for a lot of downstream work.
CREATE TABLE ORDER_ENTRY_DB.ANALYTICS.ORDER_DETAILS AS
SELECT
    order_id,
    order_total,
    order_statu
FROM ORDER_ENTRY_DB.ANALYTICS.ORDER_DETAILS_REPLICA
