#!/usr/bin/env bash
# Build dist/lambda.zip for the AWS Lambda python3.12 runtime on arm64 (Graviton).
#
#   ./scripts/build_lambda.sh
#
# Dependencies are downloaded as manylinux wheels for the Lambda platform, so the
# build works from any OS without Docker. pokemaker-infra deploys the zip.
set -euo pipefail

cd "$(dirname "$0")/.."
BUILD=dist/lambda
rm -rf "$BUILD" dist/lambda.zip
mkdir -p "$BUILD"

python3 - <<'PY' > dist/requirements.txt
import tomllib
print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))
PY

python3 -m pip install --quiet \
  --target "$BUILD" \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --requirement dist/requirements.txt

cp -r src/pokemaker "$BUILD/pokemaker"
find "$BUILD" -type d -name "__pycache__" -prune -exec rm -rf {} +

(cd "$BUILD" && zip -qr9 ../lambda.zip .)
echo "Built dist/lambda.zip ($(du -h dist/lambda.zip | cut -f1)); handler: pokemaker.main.handler"
