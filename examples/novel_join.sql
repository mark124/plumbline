-- The agent joined orders to customers on a key pair nobody has ever used.
--
-- Every column here exists, so nothing is provably wrong. But no query the
-- catalog has observed joins these two tables this way, and a join invented
-- by a model is worth one human glance before it changes a row count.
--
-- A warning, never an error. Novel is not the same as wrong.
SELECT
    o.order_id,
    c.customer_class
FROM ORDER_ENTRY_DB.ORDER_ENTRY.ORDERS o
JOIN ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS c
  ON o.order_status = c.customer_class
