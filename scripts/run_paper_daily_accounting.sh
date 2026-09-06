#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/paper_runtime_credentials.sh
paper_load_runtime_credentials

python_bin="${PREDICTION_MARKET_QUANT_PYTHON_BIN:-$HOME/.conda/envs/prediction-market-quant/bin/python}"
"$python_bin" -m quant.paper.security runtime-enforce \
  --audit-log "${PAPER_SECURITY_AUDIT_LOG:-runtime_outputs/security/paper-security-audit.jsonl}" \
  >/dev/null
exec "$python_bin" scripts/reconcile_paper_daily_accounting.py
