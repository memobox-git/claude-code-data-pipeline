-- Gold data-quality checks. Each query returns the FAILING rows (zero rows = pass).

-- check: fact_order_one_row_per_order | block | gold.fact_order
SELECT order_id, count(*) FROM gold.fact_order WHERE batch_id = current_setting('olist.batch_id')
GROUP BY order_id HAVING count(*) > 1;

-- check: fact_order_covers_all_silver_orders | block | gold.fact_order
SELECT o.order_id FROM silver.orders o LEFT JOIN gold.fact_order f USING (order_id)
WHERE o.batch_id = current_setting('olist.batch_id') AND f.order_id IS NULL;

-- check: fact_order_item_covers_all_silver_items | block | gold.fact_order_item
SELECT i.order_id, i.order_item_id FROM silver.order_items i
LEFT JOIN gold.fact_order_item g ON g.order_id = i.order_id AND g.order_item_id = i.order_item_id
WHERE i.batch_id = current_setting('olist.batch_id') AND g.order_id IS NULL;

-- check: gmv_zero_for_canceled_unavailable | block | gold.fact_order
SELECT order_id, order_status, gmv FROM gold.fact_order
WHERE batch_id = current_setting('olist.batch_id') AND order_status IN ('canceled', 'unavailable') AND gmv <> 0;

-- check: gmv_equals_sum_of_item_gmv | block | gold.fact_order
SELECT f.order_id, f.gmv, sum(i.gmv) AS item_gmv FROM gold.fact_order f
JOIN gold.fact_order_item i USING (order_id)
WHERE f.batch_id = current_setting('olist.batch_id') GROUP BY f.order_id, f.gmv HAVING f.gmv <> sum(i.gmv);

-- check: bridge_lands_on_agreed_gmv | block | gold.revenue_bridge
SELECT b.team, b.running_total, g.gmv_total FROM gold.revenue_bridge b
CROSS JOIN (SELECT sum(gmv) AS gmv_total FROM gold.fact_order WHERE batch_id = current_setting('olist.batch_id')) g
WHERE b.batch_id = current_setting('olist.batch_id') AND b.step LIKE 'Agreed GMV%' AND b.running_total <> g.gmv_total;

-- check: fact_rows_have_dims | block | gold.fact_order_item
SELECT i.order_id, i.order_item_id FROM gold.fact_order_item i
LEFT JOIN gold.dim_product p USING (product_id) LEFT JOIN gold.dim_seller s USING (seller_id)
LEFT JOIN gold.dim_customer c USING (customer_id)
WHERE i.batch_id = current_setting('olist.batch_id') AND (p.product_id IS NULL OR s.seller_id IS NULL OR c.customer_id IS NULL);

-- check: counted_orders_without_payment | warn | gold.fact_order
SELECT order_id, order_status, gmv FROM gold.fact_order
WHERE batch_id = current_setting('olist.batch_id') AND is_counted AND payment_count = 0;

-- check: orders_with_payment_but_no_items | warn | gold.fact_order
SELECT order_id, order_status, payment_total FROM gold.fact_order
WHERE batch_id = current_setting('olist.batch_id') AND item_count = 0 AND payment_total > 0;
