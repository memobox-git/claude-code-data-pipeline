-- Silver data-quality checks. Each query returns the FAILING rows (zero rows = pass).
-- Header format:  -- check: <name> | <severity block|warn> | <target table>

-- check: orders_unique | block | silver.orders
SELECT order_id, count(*) FROM silver.orders WHERE batch_id = current_setting('olist.batch_id')
GROUP BY order_id HAVING count(*) > 1;

-- check: items_have_order | block | silver.order_items
SELECT i.* FROM silver.order_items i LEFT JOIN silver.orders o USING (order_id)
WHERE i.batch_id = current_setting('olist.batch_id') AND o.order_id IS NULL;

-- check: payments_have_order | block | silver.order_payments
SELECT p.* FROM silver.order_payments p LEFT JOIN silver.orders o USING (order_id)
WHERE p.batch_id = current_setting('olist.batch_id') AND o.order_id IS NULL;

-- check: orders_have_customer | block | silver.orders
SELECT o.order_id, o.customer_id FROM silver.orders o LEFT JOIN silver.customers c USING (customer_id)
WHERE o.batch_id = current_setting('olist.batch_id') AND c.customer_id IS NULL;

-- check: items_have_product | warn | silver.order_items
SELECT i.order_id, i.product_id FROM silver.order_items i LEFT JOIN silver.products p USING (product_id)
WHERE i.batch_id = current_setting('olist.batch_id') AND p.product_id IS NULL;

-- check: items_have_seller | warn | silver.order_items
SELECT i.order_id, i.seller_id FROM silver.order_items i LEFT JOIN silver.sellers s USING (seller_id)
WHERE i.batch_id = current_setting('olist.batch_id') AND s.seller_id IS NULL;

-- check: delivered_orders_have_delivery_date | warn | silver.orders
SELECT order_id, order_status FROM silver.orders
WHERE batch_id = current_setting('olist.batch_id') AND order_status = 'delivered' AND delivered_customer_at IS NULL;

-- check: delivered_orders_have_items | warn | silver.orders
SELECT o.order_id FROM silver.orders o LEFT JOIN silver.order_items i USING (order_id)
WHERE o.batch_id = current_setting('olist.batch_id') AND o.order_status = 'delivered' AND i.order_id IS NULL;

-- check: quarantine_rate_under_1pct | block | silver.quarantine
SELECT source_table, count(*) AS quarantined FROM silver.quarantine
WHERE batch_id = current_setting('olist.batch_id')
GROUP BY source_table HAVING count(*) > 0.01 * (SELECT count(*) FROM bronze.orders WHERE batch_id = current_setting('olist.batch_id'));
