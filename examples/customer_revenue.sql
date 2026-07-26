-- Written by a coding agent from the prompt:
--   "Build a model giving me revenue per customer with their contact details
--    and credit limit, for the CRM export."
--
-- It compiles. It is wrong in three ways that only the catalog can see.
CREATE TABLE ORDER_ENTRY_DB.ANALYTICS.CUSTOMER_REVENUE AS
SELECT
    c.customer_id,
    c.cust_email,
    c.credit_limt,
    o.order_total
FROM ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS c
JOIN ORDER_ENTRY_DB.ORDER_ENTRY.ORDERS o
  ON c.customer_id = o.customer_id
