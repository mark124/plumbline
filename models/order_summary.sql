-- Daily order summary for the revenue dashboard.
--
-- Written by a coding agent. It parses, the column names look right, and the
-- table it reads from exists. Reviewing this diff on its own, there is
-- nothing to object to.
CREATE TABLE ORDER_ENTRY_DB.ANALYTICS.ORDER_SUMMARY AS
SELECT
    order_id,
    order_statu,
    SUM(order_total) AS total_revenue
FROM ORDER_ENTRY_DB.ANALYTICS.ORDER_DETAILS_REPLICA
GROUP BY order_id, order_statu
ORDER BY total_revenue DESC
