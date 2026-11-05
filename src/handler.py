"""AWS Lambda entry point. Runs one pipeline step inside the VPC.

Event: {"step": "bronze", "batch_id": "20261105T224756Z"}
Settings arrive as Lambda environment variables (same names as config.env).
The database password is fetched from Secrets Manager at runtime; nothing
secret is ever in the event, the environment or the logs.
"""
from __future__ import annotations

import time

import load_bronze
import run_layer

STEPS = {
    "bronze": lambda ev: load_bronze.run(ev["batch_id"]),
    "silver": lambda ev: run_layer.run("silver", ev["batch_id"]),
    "gold": lambda ev: run_layer.run("gold", ev["batch_id"]),
}


def lambda_handler(event, context):
    step = event.get("step")
    if step not in STEPS:
        return {"ok": False, "error": f"unknown step {step!r}; known: {sorted(STEPS)}"}
    started = time.time()
    try:
        result = STEPS[step](event)
    except Exception as exc:  # report, don't hide
        return {"ok": False, "step": step, "error": str(exc), "seconds": round(time.time() - started, 1)}
    return {"ok": True, "step": step, "result": result, "seconds": round(time.time() - started, 1)}
