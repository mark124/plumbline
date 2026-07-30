## Plumbline
**2 references in this change cannot be resolved against the catalog.**

| 2 error | 4 warning | 1 unknown | 1 info |
| --- | --- | --- | --- |

### Error

- **Column `credit_limt` does not exist** `examples/customer_revenue.sql:10`
  The catalog has a schema for `ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS` and it contains no column named `credit_limt`. The closest real column is `credit_limit`.
  Suggested replacement: `credit_limit`
  Catalog evidence: `urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)`

- **Column `order_statu` does not exist** `examples/order_details_rebuild.sql:11`
  The catalog has a schema for `ORDER_ENTRY_DB.ANALYTICS.ORDER_DETAILS_REPLICA` and it contains no column named `order_statu`. The closest real column is `order_status`.
  Suggested replacement: `order_status`
  Catalog evidence: `urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details_replica,PROD)`

### Warning

- **PII column `customer_id` flows into `ORDER_ENTRY_DB.ANALYTICS.CUSTOMER_REVENUE`** `examples/customer_revenue.sql:8`
  `ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS` and `ORDER_ENTRY_DB.ORDER_ENTRY.ORDERS`.`customer_id` is tagged PII in the catalog, and this statement writes it into `ORDER_ENTRY_DB.ANALYTICS.CUSTOMER_REVENUE`, which carries no such tag. Either tag the output, mask the column, or drop it from the select list.
  Catalog evidence: `urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)`

- **PII column `cust_email` flows into `ORDER_ENTRY_DB.ANALYTICS.CUSTOMER_REVENUE`** `examples/customer_revenue.sql:9`
  `ORDER_ENTRY_DB.ORDER_ENTRY.CUSTOMERS`.`cust_email` is tagged Email Address, PII in the catalog, and this statement writes it into `ORDER_ENTRY_DB.ANALYTICS.CUSTOMER_REVENUE`, which carries no such tag. Either tag the output, mask the column, or drop it from the select list.
  Catalog evidence: `urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)`

- **Join on `order_status` = `customer_class` is not seen in query history** `examples/novel_join.sql`
  No query recorded against these tables joins `order_status` to `customer_class`. That does not make it wrong, but it is a join pattern nobody in the organisation has used, which is worth confirming when the code was machine-written. Check the cardinality before merging.

- **`ORDER_ENTRY_DB.ANALYTICS.ORDER_DETAILS_REPLICA` is marked deprecated** `examples/order_details_rebuild.sql:12`
  The catalog marks `ORDER_ENTRY_DB.ANALYTICS.ORDER_DETAILS_REPLICA` as deprecated. New code should not take a dependency on it. Check the asset's documentation for the intended replacement.
  Catalog evidence: `urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details_replica,PROD)`

### Unknown

- **Table `ORDER_ENTRY_DB.ORDER_ENTRY.SHIPMENTS` is not in the catalog** `examples/uningested_table.sql:3`
  No dataset matching `ORDER_ENTRY_DB.ORDER_ENTRY.SHIPMENTS` was found, and nothing in the catalog has a similar name. This is reported as unknown, not as an error: the table may simply not be ingested. Its columns were not checked.

### Info

- **`ORDER_ENTRY_DB.ANALYTICS.ORDER_DETAILS` has 34 downstream consumers** `examples/order_details_rebuild.sql:7`
  `ORDER_ENTRY_DB.ANALYTICS.ORDER_DETAILS` has 34 downstream consumers (12 charts, 3 dashboards, 19 datasets). Including: `Order Details`, `Order Entry Dashboard`, `order_history`, `order_details`, `datahub_order_entries`. Changing its schema affects all of them.
  Catalog evidence: `urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD)`

---
Errors are references the catalog can disprove. Unknowns are references the catalog cannot speak to, usually because the asset is not ingested; they never block.