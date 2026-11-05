-- Gold: star schema the dashboard reads. Built from silver in one transaction
-- for the batch in current_setting('olist.batch_id'). Every table states its grain.
--
-- GMV rule: gmv = price + freight; 0 for canceled or unavailable orders.
-- Items and payments are each rolled up to ONE ROW PER ORDER before they meet.

CREATE SCHEMA IF NOT EXISTS gold;

-- Grain: one row = one customer account (customer_id). customer_unique_id links repeat buyers.
CREATE TABLE IF NOT EXISTS gold.dim_customer (
    customer_id text PRIMARY KEY, customer_unique_id text NOT NULL,
    city text, state text, batch_id text NOT NULL
);
-- Grain: one row = one seller.
CREATE TABLE IF NOT EXISTS gold.dim_seller (
    seller_id text PRIMARY KEY, city text, state text, batch_id text NOT NULL
);
-- Grain: one row = one product.
CREATE TABLE IF NOT EXISTS gold.dim_product (
    product_id text PRIMARY KEY, category text, category_pt text,
    weight_g int, photos_qty int, batch_id text NOT NULL
);
-- Grain: one row = one order.
CREATE TABLE IF NOT EXISTS gold.fact_order (
    order_id text PRIMARY KEY, customer_id text NOT NULL, order_status text NOT NULL,
    purchased_at timestamp NOT NULL, purchase_date date NOT NULL,
    delivered_at timestamp, estimated_delivery_at timestamp,
    is_delivered boolean NOT NULL, is_late boolean, days_late numeric(6,1),
    is_counted boolean NOT NULL,                -- false for canceled / unavailable
    item_count int NOT NULL, item_price numeric(12,2) NOT NULL, item_freight numeric(12,2) NOT NULL,
    item_total numeric(12,2) NOT NULL,          -- price + freight, all orders (Marketplace view)
    gmv numeric(12,2) NOT NULL,                 -- item_total if counted else 0 (agreed number)
    payment_count int NOT NULL, payment_total numeric(12,2) NOT NULL,   -- Finance view
    review_score int, has_review boolean NOT NULL,
    batch_id text NOT NULL
);
-- Grain: one row = one order line (order_id, order_item_id).
CREATE TABLE IF NOT EXISTS gold.fact_order_item (
    order_id text NOT NULL, order_item_id int NOT NULL, product_id text NOT NULL, seller_id text NOT NULL,
    customer_id text NOT NULL, order_status text NOT NULL, purchase_date date NOT NULL,
    is_counted boolean NOT NULL, price numeric(12,2) NOT NULL, freight_value numeric(12,2) NOT NULL,
    gmv numeric(12,2) NOT NULL, is_late boolean, review_score int,
    batch_id text NOT NULL, PRIMARY KEY (order_id, order_item_id)
);
-- Grain: one row = one step in the bridge from a team's number to the agreed GMV.
CREATE TABLE IF NOT EXISTS gold.revenue_bridge (
    team text NOT NULL, step_no int NOT NULL, step text NOT NULL,
    amount numeric(14,2) NOT NULL, running_total numeric(14,2) NOT NULL,
    batch_id text NOT NULL, PRIMARY KEY (team, step_no)
);

DELETE FROM gold.revenue_bridge  WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM gold.fact_order_item WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM gold.fact_order      WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM gold.dim_product     WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM gold.dim_seller      WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM gold.dim_customer    WHERE batch_id = current_setting('olist.batch_id');

INSERT INTO gold.dim_customer
SELECT customer_id, customer_unique_id, city, state, batch_id FROM silver.customers
WHERE batch_id = current_setting('olist.batch_id');

INSERT INTO gold.dim_seller
SELECT seller_id, city, state, batch_id FROM silver.sellers WHERE batch_id = current_setting('olist.batch_id');

INSERT INTO gold.dim_product
SELECT product_id, coalesce(category_name_english, 'unknown'), category_name, weight_g, photos_qty, batch_id
FROM silver.products WHERE batch_id = current_setting('olist.batch_id');

-- Roll-ups: one row per order from items, one row per order from payments, one from reviews.
WITH items AS (
    SELECT order_id, count(*) AS item_count, sum(price) AS item_price, sum(freight_value) AS item_freight
    FROM silver.order_items WHERE batch_id = current_setting('olist.batch_id') GROUP BY order_id
), pays AS (
    SELECT order_id, count(*) AS payment_count, sum(payment_value) AS payment_total
    FROM silver.order_payments WHERE batch_id = current_setting('olist.batch_id') GROUP BY order_id
), revs AS (
    SELECT DISTINCT ON (order_id) order_id, review_score
    FROM silver.order_reviews WHERE batch_id = current_setting('olist.batch_id')
    ORDER BY order_id, review_answered_at DESC NULLS LAST, review_created_at DESC NULLS LAST
)
INSERT INTO gold.fact_order
SELECT o.order_id, o.customer_id, o.order_status, o.purchased_at, o.purchased_at::date,
       o.delivered_customer_at, o.estimated_delivery_at,
       o.order_status = 'delivered',
       CASE WHEN o.delivered_customer_at IS NOT NULL AND o.estimated_delivery_at IS NOT NULL
            THEN o.delivered_customer_at > o.estimated_delivery_at END,
       CASE WHEN o.delivered_customer_at IS NOT NULL AND o.estimated_delivery_at IS NOT NULL
            THEN round(extract(epoch FROM (o.delivered_customer_at - o.estimated_delivery_at)) / 86400.0, 1) END,
       o.order_status NOT IN ('canceled', 'unavailable'),
       coalesce(i.item_count, 0), coalesce(i.item_price, 0), coalesce(i.item_freight, 0),
       coalesce(i.item_price, 0) + coalesce(i.item_freight, 0),
       CASE WHEN o.order_status IN ('canceled', 'unavailable') THEN 0
            ELSE coalesce(i.item_price, 0) + coalesce(i.item_freight, 0) END,
       coalesce(p.payment_count, 0), coalesce(p.payment_total, 0),
       r.review_score, r.review_score IS NOT NULL,
       o.batch_id
FROM silver.orders o
LEFT JOIN items i USING (order_id)
LEFT JOIN pays  p USING (order_id)
LEFT JOIN revs  r USING (order_id)
WHERE o.batch_id = current_setting('olist.batch_id');

INSERT INTO gold.fact_order_item
SELECT i.order_id, i.order_item_id, i.product_id, i.seller_id, f.customer_id, f.order_status, f.purchase_date,
       f.is_counted, i.price, i.freight_value,
       CASE WHEN f.is_counted THEN i.price + i.freight_value ELSE 0 END,
       f.is_late, f.review_score, i.batch_id
FROM silver.order_items i JOIN gold.fact_order f USING (order_id)
WHERE i.batch_id = current_setting('olist.batch_id');

-- The bridge: each team's export total, the adjustments, and the agreed GMV.
WITH f AS (SELECT * FROM gold.fact_order WHERE batch_id = current_setting('olist.batch_id')),
steps AS (
    SELECT 'finance' AS team, 1 AS step_no, 'Payments export total (all payment rows)' AS step, sum(payment_total) AS amount FROM f
    UNION ALL SELECT 'finance', 2, 'minus payments on orders with no items', -sum(payment_total) FROM f WHERE item_count = 0
    UNION ALL SELECT 'finance', 3, 'minus payments on canceled or unavailable orders that have items', -sum(payment_total) FROM f WHERE NOT is_counted AND item_count > 0
    UNION ALL SELECT 'finance', 4, 'minus payment minus item difference on counted orders (installment interest, rounding, vouchers)', -sum(payment_total - item_total) FROM f WHERE is_counted AND item_count > 0
    UNION ALL SELECT 'marketplace', 1, 'Items export total (price + freight, all orders)', sum(item_total) FROM f
    UNION ALL SELECT 'marketplace', 2, 'minus items on canceled or unavailable orders', -sum(item_total) FROM f WHERE NOT is_counted
)
INSERT INTO gold.revenue_bridge
SELECT team, step_no, step, coalesce(amount, 0),
       sum(coalesce(amount, 0)) OVER (PARTITION BY team ORDER BY step_no), current_setting('olist.batch_id')
FROM steps;

INSERT INTO gold.revenue_bridge
SELECT DISTINCT ON (team) team, step_no + 1, 'Agreed GMV (price + freight, counted orders only)', 0, running_total, current_setting('olist.batch_id')
FROM gold.revenue_bridge WHERE batch_id = current_setting('olist.batch_id')
ORDER BY team, step_no DESC;
