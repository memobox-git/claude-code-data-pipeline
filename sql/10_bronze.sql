-- Bronze: files exactly as landed. Every source column is text.
-- Each table adds batch_id, source_file, loaded_at. Idempotent.
-- Grain of every bronze table: one row = one line of the source CSV.

CREATE SCHEMA IF NOT EXISTS bronze;

CREATE TABLE IF NOT EXISTS bronze.orders (
    order_id text, customer_id text, order_status text,
    order_purchase_timestamp text, order_approved_at text,
    order_delivered_carrier_date text, order_delivered_customer_date text,
    order_estimated_delivery_date text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bronze.order_items (
    order_id text, order_item_id text, product_id text, seller_id text,
    shipping_limit_date text, price text, freight_value text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bronze.order_payments (
    order_id text, payment_sequential text, payment_type text,
    payment_installments text, payment_value text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bronze.order_reviews (
    review_id text, order_id text, review_score text,
    review_comment_title text, review_comment_message text,
    review_creation_date text, review_answer_timestamp text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bronze.customers (
    customer_id text, customer_unique_id text, customer_zip_code_prefix text,
    customer_city text, customer_state text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bronze.sellers (
    seller_id text, seller_zip_code_prefix text, seller_city text, seller_state text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bronze.products (
    product_id text, product_category_name text, product_name_lenght text,
    product_description_lenght text, product_photos_qty text, product_weight_g text,
    product_length_cm text, product_height_cm text, product_width_cm text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bronze.product_category_translation (
    product_category_name text, product_category_name_english text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bronze.geolocation (
    geolocation_zip_code_prefix text, geolocation_lat text, geolocation_lng text,
    geolocation_city text, geolocation_state text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);
