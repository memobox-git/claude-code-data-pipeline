"""Run one pipeline step in the Lambda and print its log and result.

Usage: python run.py bronze --batch-id 20261105T224756Z
       python run.py silver --batch-id ...
"""
import argparse, base64, json, sys
import boto3, botocore

ap = argparse.ArgumentParser()
ap.add_argument("step")
ap.add_argument("--batch-id", required=True)
a = ap.parse_args()

cfg = botocore.config.Config(read_timeout=900, connect_timeout=30, retries={"max_attempts": 0})
lam = boto3.client("lambda", region_name="us-east-1", config=cfg)
r = lam.invoke(FunctionName="olist-lightning-pipeline", LogType="Tail",
               Payload=json.dumps({"step": a.step, "batch_id": a.batch_id}).encode())
log = base64.b64decode(r["LogResult"]).decode()
print("\n".join(l for l in log.splitlines() if not l.startswith(("START", "END", "REPORT"))))
out = json.loads(r["Payload"].read())
print(json.dumps(out, indent=1))
sys.exit(0 if out.get("ok") else 1)
