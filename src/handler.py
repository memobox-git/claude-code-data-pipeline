"""AWS Lambda entry point. Runs one pipeline step inside the VPC.

Event: {"step": "bronze", "batch_id": "20261105T224756Z"}
Settings arrive as Lambda environment variables (same names as config.env).
The database password is fetched from Secrets Manager at runtime; nothing
secret is ever in the event, the environment or the logs.
"""
from __future__ import annotations

import os
import time

import load_bronze
import report
import reset
import run_layer

STEPS = {
    "bronze": lambda ev: load_bronze.run(ev["batch_id"]),
    "silver": lambda ev: run_layer.run("silver", ev["batch_id"]),
    "gold": lambda ev: run_layer.run("gold", ev["batch_id"]),
    "report": lambda ev: report.run(ev["batch_id"]),
    "reset": lambda ev: reset.run(ev.get("batch_id")),
}


OVERRIDABLE = ("S3_BUCKET", "DB_HOST", "DB_SECRET_NAME", "DB_NAME", "DB_USER")


def lambda_handler(event, context):
    for k in OVERRIDABLE:          # run the same code against another database or bucket
        if event.get(k):
            os.environ[k] = event[k]
    step = event.get("step")
    if step not in STEPS:
        return {"ok": False, "error": f"unknown step {step!r}; known: {sorted(STEPS)}"}
    started = time.time()
    try:
        result = STEPS[step](event)
    except Exception as exc:  # report, don't hide
        return {"ok": False, "step": step, "error": str(exc), "seconds": round(time.time() - started, 1)}
    return {"ok": True, "step": step, "result": result, "seconds": round(time.time() - started, 1)}
