-- Silver: correct types, deduplicated, bad rows quarantined with a reason.
-- Runs inside one transaction for the batch in current_setting('olist.batch_id').
-- Grain of each silver table: one row = one real-world record (order, item, payment...).

CREATE SCHEMA IF NOT EXISTS silver;

-- One row = one source row that silver refused, and why.
CREATE TABLE IF NOT EXISTS silver.quarantine (
    quarantine_id bigserial PRIMARY KEY,
    batch_id      text NOT NULL,
    source_table  text NOT NULL,
    reason        text NOT NULL,
    row_data      jsonb NOT NULL,
    quarantined_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS silver.orders (
    order_id text PRIMARY KEY, customer_id text NOT NULL, order_status text NOT NULL,
    purchased_at timestamp, approved_at timestamp, delivered_carrier_at timestamp,
    delivered_customer_at timestamp, estimated_delivery_at timestamp,
    batch_id text NOT NULL
);
CREATE TABLE IF NOT EXISTS silver.order_items (
    order_id text NOT NULL, order_item_id int NOT NULL, product_id text NOT NULL, seller_id text NOT NULL,
    shipping_limit_at timestamp, price numeric(12,2) NOT NULL, freight_value numeric(12,2) NOT NULL,
    batch_id text NOT NULL, PRIMARY KEY (order_id, order_item_id)
);
CREATE TABLE IF NOT EXISTS silver.order_payments (
    order_id text NOT NULL, payment_sequential int NOT NULL, payment_type text NOT NULL,
    payment_installments int, payment_value numeric(12,2) NOT NULL,
    batch_id text NOT NULL, PRIMARY KEY (order_id, payment_sequential)
);
CREATE TABLE IF NOT EXISTS silver.order_reviews (
    review_id text NOT NULL, order_id text NOT NULL, review_score int NOT NULL,
    review_comment_title text, review_comment_message text,
    review_created_at timestamp, review_answered_at timestamp,
    batch_id text NOT NULL, PRIMARY KEY (review_id, order_id)
);
CREATE TABLE IF NOT EXISTS silver.customers (
    customer_id text PRIMARY KEY, customer_unique_id text NOT NULL, zip_code_prefix text,
    city text, state text, batch_id text NOT NULL
);
CREATE TABLE IF NOT EXISTS silver.sellers (
    seller_id text PRIMARY KEY, zip_code_prefix text, city text, state text, batch_id text NOT NULL
);
CREATE TABLE IF NOT EXISTS silver.products (
    product_id text PRIMARY KEY, category_name text, category_name_english text,
    name_length int, description_length int, photos_qty int, weight_g int,
    length_cm int, height_cm int, width_cm int, batch_id text NOT NULL
);

-- Rebuild the batch: remove anything this batch wrote before (idempotent).
DELETE FROM silver.quarantine     WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM silver.order_reviews  WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM silver.order_payments WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM silver.order_items    WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM silver.orders         WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM silver.customers      WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM silver.sellers        WHERE batch_id = current_setting('olist.batch_id');
DELETE FROM silver.products       WHERE batch_id = current_setting('olist.batch_id');

-- Helpers: safe casts return NULL instead of raising.
CREATE OR REPLACE FUNCTION silver.to_ts(t text) RETURNS timestamp LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN t ~ '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$' THEN t::timestamp END $$;
CREATE OR REPLACE FUNCTION silver.to_num(t text) RETURNS numeric LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN t ~ '^-?\d+(\.\d+)?$' THEN t::numeric END $$;
CREATE OR REPLACE FUNCTION silver.to_int(t text) RETURNS int LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN t ~ '^-?\d+$' THEN t::int END $$;

---------------------------------------------------------------- customers
WITH src AS (
    SELECT *, row_number() OVER (PARTITION BY customer_id ORDER BY loaded_at) AS rn
    FROM bronze.customers WHERE batch_id = current_setting('olist.batch_id')
), bad AS (
    INSERT INTO silver.quarantine (batch_id, source_table, reason, row_data)
    SELECT batch_id, 'bronze.customers',
           CASE WHEN customer_id IS NULL THEN 'missing customer_id'
                WHEN customer_unique_id IS NULL THEN 'missing customer_unique_id'
                ELSE 'duplicate customer_id' END,
           to_jsonb(src) - 'rn'
    FROM src WHERE customer_id IS NULL OR customer_unique_id IS NULL OR rn > 1
)
INSERT INTO silver.customers
SELECT customer_id, customer_unique_id, customer_zip_code_prefix, customer_city, customer_state, batch_id
FROM src WHERE customer_id IS NOT NULL AND customer_unique_id IS NOT NULL AND rn = 1;

---------------------------------------------------------------- sellers
WITH src AS (
    SELECT *, row_number() OVER (PARTITION BY seller_id ORDER BY loaded_at) AS rn
    FROM bronze.sellers WHERE batch_id = current_setting('olist.batch_id')
), bad AS (
    INSERT INTO silver.quarantine (batch_id, source_table, reason, row_data)
    SELECT batch_id, 'bronze.sellers',
           CASE WHEN seller_id IS NULL THEN 'missing seller_id' ELSE 'duplicate seller_id' END, to_jsonb(src) - 'rn'
    FROM src WHERE seller_id IS NULL OR rn > 1
)
INSERT INTO silver.sellers
SELECT seller_id, seller_zip_code_prefix, seller_city, seller_state, batch_id
FROM src WHERE seller_id IS NOT NULL AND rn = 1;

---------------------------------------------------------------- products
WITH src AS (
    SELECT p.*, t.product_category_name_english,
           row_number() OVER (PARTITION BY p.product_id ORDER BY p.loaded_at) AS rn
    FROM bronze.products p
    LEFT JOIN bronze.product_category_translation t
           ON t.product_category_name = p.product_category_name
          AND t.batch_id = p.batch_id
    WHERE p.batch_id = current_setting('olist.batch_id')
), bad AS (
    INSERT INTO silver.quarantine (batch_id, source_table, reason, row_data)
    SELECT batch_id, 'bronze.products',
           CASE WHEN product_id IS NULL THEN 'missing product_id' ELSE 'duplicate product_id' END, to_jsonb(src) - 'rn'
    FROM src WHERE product_id IS NULL OR rn > 1
)
INSERT INTO silver.products
SELECT product_id, product_category_name,
       coalesce(product_category_name_english, product_category_name),
       silver.to_int(product_name_lenght), silver.to_int(product_description_lenght),
       silver.to_int(product_photos_qty), silver.to_int(product_weight_g),
       silver.to_int(product_length_cm), silver.to_int(product_height_cm), silver.to_int(product_width_cm), batch_id
FROM src WHERE product_id IS NOT NULL AND rn = 1;

---------------------------------------------------------------- orders
WITH src AS (
    SELECT *, row_number() OVER (PARTITION BY order_id ORDER BY loaded_at) AS rn,
           silver.to_ts(order_purchase_timestamp) AS purchased_at
    FROM bronze.orders WHERE batch_id = current_setting('olist.batch_id')
), bad AS (
    INSERT INTO silver.quarantine (batch_id, source_table, reason, row_data)
    SELECT batch_id, 'bronze.orders',
           CASE WHEN order_id IS NULL THEN 'missing order_id'
                WHEN customer_id IS NULL THEN 'missing customer_id'
                WHEN purchased_at IS NULL THEN 'unparsable order_purchase_timestamp'
                WHEN coalesce(order_status,'') NOT IN ('delivered','shipped','canceled','unavailable','invoiced','processing','created','approved')
                     THEN 'unknown order_status: ' || coalesce(order_status, 'NULL')
                ELSE 'duplicate order_id' END,
           to_jsonb(src) - 'rn' - 'purchased_at'
    FROM src WHERE order_id IS NULL OR customer_id IS NULL OR purchased_at IS NULL OR rn > 1
       OR coalesce(order_status,'') NOT IN ('delivered','shipped','canceled','unavailable','invoiced','processing','created','approved')
)
INSERT INTO silver.orders
SELECT order_id, customer_id, order_status, purchased_at,
       silver.to_ts(order_approved_at), silver.to_ts(order_delivered_carrier_date),
       silver.to_ts(order_delivered_customer_date), silver.to_ts(order_estimated_delivery_date), batch_id
FROM src WHERE order_id IS NOT NULL AND customer_id IS NOT NULL AND purchased_at IS NOT NULL AND rn = 1
  AND order_status IN ('delivered','shipped','canceled','unavailable','invoiced','processing','created','approved');

---------------------------------------------------------------- order_items
WITH src AS (
    SELECT i.*, silver.to_int(order_item_id) AS item_no, silver.to_num(price) AS price_n,
           silver.to_num(freight_value) AS freight_n,
           (o.order_id IS NOT NULL) AS has_order,
           row_number() OVER (PARTITION BY i.order_id, i.order_item_id ORDER BY i.loaded_at) AS rn
    FROM bronze.order_items i
    LEFT JOIN silver.orders o ON o.order_id = i.order_id AND o.batch_id = i.batch_id
    WHERE i.batch_id = current_setting('olist.batch_id')
), bad AS (
    INSERT INTO silver.quarantine (batch_id, source_table, reason, row_data)
    SELECT batch_id, 'bronze.order_items',
           CASE WHEN order_id IS NULL OR item_no IS NULL THEN 'missing key'
                WHEN NOT has_order THEN 'order_id not in silver.orders'
                WHEN price_n IS NULL OR price_n < 0 THEN 'bad price'
                WHEN freight_n IS NULL OR freight_n < 0 THEN 'bad freight_value'
                WHEN product_id IS NULL OR seller_id IS NULL THEN 'missing product_id or seller_id'
                ELSE 'duplicate (order_id, order_item_id)' END,
           to_jsonb(src) - 'rn' - 'item_no' - 'price_n' - 'freight_n' - 'has_order'
    FROM src WHERE order_id IS NULL OR item_no IS NULL OR NOT has_order OR price_n IS NULL OR price_n < 0
       OR freight_n IS NULL OR freight_n < 0 OR product_id IS NULL OR seller_id IS NULL OR rn > 1
)
INSERT INTO silver.order_items
SELECT order_id, item_no, product_id, seller_id, silver.to_ts(shipping_limit_date), price_n, freight_n, batch_id
FROM src WHERE order_id IS NOT NULL AND item_no IS NOT NULL AND has_order AND price_n >= 0 AND freight_n >= 0
  AND product_id IS NOT NULL AND seller_id IS NOT NULL AND rn = 1;

---------------------------------------------------------------- order_payments
WITH src AS (
    SELECT p.*, silver.to_int(payment_sequential) AS seq, silver.to_num(payment_value) AS value_n,
           (o.order_id IS NOT NULL) AS has_order,
           row_number() OVER (PARTITION BY p.order_id, p.payment_sequential ORDER BY p.loaded_at) AS rn
    FROM bronze.order_payments p
    LEFT JOIN silver.orders o ON o.order_id = p.order_id AND o.batch_id = p.batch_id
    WHERE p.batch_id = current_setting('olist.batch_id')
), bad AS (
    INSERT INTO silver.quarantine (batch_id, source_table, reason, row_data)
    SELECT batch_id, 'bronze.order_payments',
           CASE WHEN order_id IS NULL OR seq IS NULL THEN 'missing key'
                WHEN NOT has_order THEN 'order_id not in silver.orders'
                WHEN value_n IS NULL OR value_n < 0 THEN 'bad payment_value'
                ELSE 'duplicate (order_id, payment_sequential)' END,
           to_jsonb(src) - 'rn' - 'seq' - 'value_n' - 'has_order'
    FROM src WHERE order_id IS NULL OR seq IS NULL OR NOT has_order OR value_n IS NULL OR value_n < 0 OR rn > 1
)
INSERT INTO silver.order_payments
SELECT order_id, seq, coalesce(payment_type, 'unknown'), silver.to_int(payment_installments), value_n, batch_id
FROM src WHERE order_id IS NOT NULL AND seq IS NOT NULL AND has_order AND value_n >= 0 AND rn = 1;

---------------------------------------------------------------- order_reviews
WITH src AS (
    SELECT r.*, silver.to_int(review_score) AS score, (o.order_id IS NOT NULL) AS has_order,
           row_number() OVER (PARTITION BY r.review_id, r.order_id ORDER BY silver.to_ts(r.review_answer_timestamp) DESC NULLS LAST, r.loaded_at) AS rn
    FROM bronze.order_reviews r
    LEFT JOIN silver.orders o ON o.order_id = r.order_id AND o.batch_id = r.batch_id
    WHERE r.batch_id = current_setting('olist.batch_id')
), bad AS (
    INSERT INTO silver.quarantine (batch_id, source_table, reason, row_data)
    SELECT batch_id, 'bronze.order_reviews',
           CASE WHEN review_id IS NULL OR order_id IS NULL THEN 'missing key'
                WHEN NOT has_order THEN 'order_id not in silver.orders'
                WHEN score IS NULL OR score NOT BETWEEN 1 AND 5 THEN 'review_score not 1..5'
                ELSE 'duplicate (review_id, order_id)' END,
           to_jsonb(src) - 'rn' - 'score' - 'has_order'
    FROM src WHERE review_id IS NULL OR order_id IS NULL OR NOT has_order OR score IS NULL OR score NOT BETWEEN 1 AND 5 OR rn > 1
)
INSERT INTO silver.order_reviews
SELECT review_id, order_id, score, review_comment_title, review_comment_message,
       silver.to_ts(review_creation_date), silver.to_ts(review_answer_timestamp), batch_id
FROM src WHERE review_id IS NOT NULL AND order_id IS NOT NULL AND has_order AND score BETWEEN 1 AND 5 AND rn = 1;
