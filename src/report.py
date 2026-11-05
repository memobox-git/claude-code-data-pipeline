"""Read-only report from gold: the bridge and the business answers.
Connects as the bi_reader role when BI_SECRET_NAME is set, else as the admin.
Also writes the result as JSON to s3://$S3_BUCKET/gold-snapshots/<batch>.json for the dashboard.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import psycopg

from common import aws_session, db_password, settings

QUERIES = {
    "bridge": """
        SELECT team, step_no, step, amount, running_total FROM gold.revenue_bridge
        WHERE batch_id = %(b)s ORDER BY team, step_no""",
    "totals": """
        SELECT count(*) AS orders, sum(gmv) AS gmv, sum(payment_total) AS payments, sum(item_total) AS items,
               count(*) FILTER (WHERE NOT is_counted) AS canceled_or_unavailable
        FROM gold.fact_order WHERE batch_id = %(b)s""",
    "top_categories": """
        SELECT p.category, sum(i.gmv) AS gmv, count(DISTINCT i.order_id) AS orders
        FROM gold.fact_order_item i JOIN gold.dim_product p USING (product_id)
        WHERE i.batch_id = %(b)s AND i.is_counted GROUP BY 1 ORDER BY 2 DESC LIMIT 10""",
    "top_sellers": """
        SELECT i.seller_id, s.state, sum(i.gmv) AS gmv, count(DISTINCT i.order_id) AS orders,
               round(avg(i.review_score), 2) AS avg_review
        FROM gold.fact_order_item i JOIN gold.dim_seller s USING (seller_id)
        WHERE i.batch_id = %(b)s AND i.is_counted GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10""",
    "late_vs_review": """
        SELECT CASE WHEN is_late THEN 'late' ELSE 'on time' END AS delivery, count(*) AS orders,
               round(avg(review_score), 2) AS avg_review,
               round(100.0 * count(*) FILTER (WHERE review_score <= 2) / count(*), 1) AS pct_1_or_2_stars
        FROM gold.fact_order WHERE batch_id = %(b)s AND is_delivered AND is_late IS NOT NULL AND has_review
        GROUP BY 1 ORDER BY 1""",
    "monthly_gmv": """
        SELECT to_char(date_trunc('month', purchase_date), 'YYYY-MM') AS month, sum(gmv) AS gmv, count(*) AS orders
        FROM gold.fact_order WHERE batch_id = %(b)s AND is_counted GROUP BY 1 ORDER BY 1""",
    "dq_summary": """
        SELECT layer, severity, count(*) AS checks, count(*) FILTER (WHERE NOT passed) AS failed
        FROM meta.dq_results WHERE batch_id = %(b)s GROUP BY 1, 2 ORDER BY 1, 2""",
}


def run(batch_id: str) -> dict:
    cfg = settings()
    session = aws_session(cfg)
    bi_secret = os.environ.get("BI_SECRET_NAME")
    user = cfg["BI_USER"] if bi_secret else cfg["DB_USER"]
    conn = psycopg.connect(
        host=cfg["DB_HOST"], port=int(cfg["DB_PORT"]), dbname=cfg["DB_NAME"], user=user,
        password=db_password(session, bi_secret or cfg["DB_SECRET_NAME"]), sslmode="require", connect_timeout=15,
    )
    out = {"batch_id": batch_id, "read_as": user, "generated_at": datetime.now(timezone.utc).isoformat()}
    with conn, conn.cursor() as cur:
        for name, q in QUERIES.items():
            cur.execute(q, {"b": batch_id})
            cols = [d.name for d in cur.description]
            out[name] = [dict(zip(cols, row)) for row in cur.fetchall()]
    session.client("s3").put_object(
        Bucket=cfg["S3_BUCKET"], Key=f"gold-snapshots/{batch_id}.json", ContentType="application/json",
        Body=json.dumps(out, default=str, indent=1).encode(),
    )
    return json.loads(json.dumps(out, default=str))
