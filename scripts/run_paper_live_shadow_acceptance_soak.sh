#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/paper_runtime_credentials.sh
paper_load_runtime_credentials
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
PYTHON_BIN="${PREDICTION_MARKET_QUANT_PYTHON_BIN:-$HOME/.conda/envs/prediction-market-quant/bin/python}"
"$PYTHON_BIN" -m quant.paper.security runtime-enforce \
  --audit-log "${PAPER_SECURITY_AUDIT_LOG:-runtime_outputs/security/paper-security-audit.jsonl}" \
  >/dev/null

exec "$PYTHON_BIN" -m quant.paper.acceptance soak \
  --duration-seconds "${PAPER_LIVE_ACCEPTANCE_DURATION_SECONDS:-14400}" \
  --interval-seconds "${PAPER_LIVE_ACCEPTANCE_INTERVAL_SECONDS:-60}" \
  --min-intents "${PAPER_LIVE_ACCEPTANCE_MIN_INTENTS:-4}" \
  --health-stale-seconds "${PAPER_LIVE_ACCEPTANCE_HEALTH_STALE_SECONDS:-15}" \
  --canary-wait-seconds "${PAPER_LIVE_ACCEPTANCE_CANARY_WAIT_SECONDS:-180}" \
  --completed-failure-exit-code "${PAPER_LIVE_ACCEPTANCE_COMPLETED_FAILURE_EXIT_CODE:-1}" \
  --run-canary \
  --resume-state "${PAPER_LIVE_ACCEPTANCE_RESUME_STATE:-runtime_outputs/paper_live_shadow/acceptance/soak-state.json}" \
  --output-dir "${PAPER_LIVE_ACCEPTANCE_OUTPUT_DIR:-runtime_outputs/paper_live_shadow/acceptance}"
