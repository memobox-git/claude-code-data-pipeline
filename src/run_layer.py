"""Build a layer (silver or gold) for one batch in ONE transaction:
  1. run sql/<layer>.sql         (DDL + loads; reads current_setting('olist.batch_id'))
  2. run every check in sql/<layer>_checks.sql, record in meta.dq_results
  3. record source -> target row counts in meta.lineage
  4. a failed 'block' check rolls everything back; 'warn' is recorded only
"""
from __future__ import annotations

import json
import re

from common import ROOT, aws_session, db_connect, settings

LAYERS = {
    "silver": {
        "sql": "20_silver.sql", "checks": "25_silver_checks.sql",
        "lineage": {  # target: source (counts are rows for this batch)
            "silver.orders": "bronze.orders", "silver.order_items": "bronze.order_items",
            "silver.order_payments": "bronze.order_payments", "silver.order_reviews": "bronze.order_reviews",
            "silver.customers": "bronze.customers", "silver.sellers": "bronze.sellers",
            "silver.products": "bronze.products",
        },
    },
    "gold": {
        "sql": "30_gold.sql", "checks": "35_gold_checks.sql",
        "lineage": {
            "gold.dim_customer": "silver.customers", "gold.dim_seller": "silver.sellers",
            "gold.dim_product": "silver.products", "gold.fact_order": "silver.orders",
            "gold.fact_order_item": "silver.order_items",
        },
    },
}
CHECK_HDR = re.compile(r"^--\s*check:\s*(?P<name>[\w-]+)\s*\|\s*(?P<sev>block|warn)\s*\|\s*(?P<target>[\w.]+)\s*$", re.M)


def parse_checks(text: str) -> list[dict]:
    parts = CHECK_HDR.split(text)   # [prelude, name, sev, target, body, name, sev, target, body, ...]
    return [{"name": parts[i], "severity": parts[i + 1], "target": parts[i + 2],
             "sql": parts[i + 3].strip().rstrip(";")} for i in range(1, len(parts), 4)]


def count(cur, table: str, batch_id: str) -> int:
    schema, name = table.split(".")
    cur.execute(f'SELECT count(*) FROM "{schema}"."{name}" WHERE batch_id = %s', (batch_id,))
    return cur.fetchone()[0]


def run(layer: str, batch_id: str) -> dict:
    spec = LAYERS[layer]
    cfg = settings()
    session = aws_session(cfg)
    checks_out, blocks = [], []
    with db_connect(cfg, session) as conn:
        cur = conn.cursor()
        cur.execute("SELECT set_config('olist.batch_id', %s, true)", (batch_id,))
        cur.execute((ROOT / "sql" / spec["sql"]).read_text())

        for chk in parse_checks((ROOT / "sql" / spec["checks"]).read_text()):
            cur.execute(f"SELECT count(*) FROM ({chk['sql']}) f")
            failing = cur.fetchone()[0]
            sample = None
            if failing:
                cur.execute(f"SELECT to_jsonb(f) FROM ({chk['sql']}) f LIMIT 5")
                sample = json.dumps([r[0] for r in cur.fetchall()], default=str)
            cur.execute(
                "INSERT INTO meta.dq_results (batch_id, layer, check_name, target, severity, failing_rows, sample) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (batch_id, layer, chk["name"], chk["target"], chk["severity"], failing, sample),
            )
            status = "ok" if failing == 0 else chk["severity"].upper()
            checks_out.append({"check": chk["name"], "severity": chk["severity"], "failing_rows": failing, "status": status})
            print(f"check {chk['name']:40} {chk['severity']:5} failing={failing:<7} {status}", flush=True)
            if failing and chk["severity"] == "block":
                blocks.append(f"{chk['name']}: {failing} failing rows, sample {sample}")

        lineage = {}
        for target, source in spec["lineage"].items():
            rows_in, rows_out = count(cur, source, batch_id), count(cur, target, batch_id)
            quarantined = 0
            if layer == "silver":
                cur.execute("SELECT count(*) FROM silver.quarantine WHERE batch_id = %s AND source_table = %s", (batch_id, source))
                quarantined = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO meta.lineage (batch_id, layer, source, target, rows_in, rows_out, rows_quarantined) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)", (batch_id, layer, source, target, rows_in, rows_out, quarantined))
            lineage[target] = {"source": source, "rows_in": rows_in, "rows_out": rows_out, "quarantined": quarantined}
            print(f"{source:24} -> {target:24} in={rows_in:<8} out={rows_out:<8} quarantined={quarantined}", flush=True)

        if blocks:
            conn.rollback()
            raise RuntimeError(f"{layer} rolled back, block checks failed:\n  " + "\n  ".join(blocks))
        conn.commit()
    print(f"\n{layer} batch {batch_id} committed", flush=True)
    return {"checks": checks_out, "lineage": lineage}
