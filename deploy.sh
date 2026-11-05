#!/usr/bin/env bash
# Package src/ + sql/ + deps and push to the Lambda. Usage: ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
PKG=${PKG_DIR:-build/lambda_pkg}
rm -rf "$PKG" && mkdir -p "$PKG"
.venv/bin/pip install -q --target "$PKG" --platform manylinux2014_x86_64 --python-version 3.13 \
  --only-binary=:all: --implementation cp "psycopg[binary]>=3.2" python-dotenv
cp src/*.py "$PKG"/ && cp -r sql "$PKG"/sql
find "$PKG" -name '__pycache__' -prune -exec rm -rf {} +
( cd "$PKG" && rm -f ../lambda.zip && zip -qr ../lambda.zip . )
AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1} .venv/bin/python - <<'EOF'
import boto3, pathlib
lam = boto3.client("lambda")
lam.update_function_code(FunctionName="olist-lightning-pipeline", ZipFile=pathlib.Path("build/lambda.zip").read_bytes())
lam.get_waiter("function_updated_v2").wait(FunctionName="olist-lightning-pipeline")
print("deployed olist-lightning-pipeline")
EOF
