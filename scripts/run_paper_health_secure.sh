#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/paper_runtime_credentials.sh
paper_load_runtime_credentials

python_bin="${PAPER_RUNTIME_PYTHON_BIN:-${PREDICTION_MARKET_QUANT_PYTHON_BIN:-python}}"
exec "$python_bin" -m quant.paper.production_runtime serve "$@"
