-- A model that resolves cleanly against the catalog.
--
-- The Plumbline gate runs over this directory on every pull request. This
-- file is the "before" state: every table and column below exists, so the
-- check passes and the build is green.
CREATE TABLE ORDER_ENTRY_DB.ANALYTICS.CUSTOMER_ORDERS AS
SELECT
    c.customer_id,
    c.customer_class,
    o.order_id,
    o.order_status,
    o.order_total
FROM ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS c
JOIN ORDER_ENTRY_DB.ORDER_ENTRY.ORDERS o
  ON c.customer_id = o.customer_id
