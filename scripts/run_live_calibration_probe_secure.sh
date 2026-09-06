#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
: "${LIVE_CALIBRATION_PLAN:?LIVE_CALIBRATION_PLAN is required}"
mode="${LIVE_CALIBRATION_MODE:-live}"
case "${mode}" in
  live|prepare-live|no-submit) ;;
  *)
    printf 'unsupported LIVE_CALIBRATION_MODE: %s\n' "${mode}" >&2
    exit 64
    ;;
esac
if [[ "${mode}" == "live" ]]; then
  : "${LIVE_CALIBRATION_RUN_ID:?LIVE_CALIBRATION_RUN_ID is required for live mode}"
fi
paper_control_env="${PAPER_DB_CONTROL_ENV:-${HOME}/.config/prediction-market-quant/paper-db-control.env}"
source scripts/live_calibration_credentials.sh
live_load_paper_control_env "${paper_control_env}"
if [[ -n "${CREDENTIALS_DIRECTORY:-}" ]]; then
  live_load_db_credential
  live_load_trading_credentials
else
  live_load_calibration_env "${LIVE_CALIBRATION_ENV_FILE:-${HOME}/.config/prediction-market-quant/calibration.env}"
fi

args=(
  --config "$LIVE_CALIBRATION_PLAN"
  "--${mode}"
  --json-out "${LIVE_CALIBRATION_OUTPUT:-runtime_outputs/taker_calibration/live-service-latest.json}"
)
if [[ "${mode}" == "live" ]]; then
  args+=(--run-id "$LIVE_CALIBRATION_RUN_ID")
fi
if [[ -n "${LIVE_CALIBRATION_MARKET_ID:-}" ]]; then
  args+=(--market-id "$LIVE_CALIBRATION_MARKET_ID")
fi
if [[ -n "${LIVE_CALIBRATION_ASSET_ID:-}" ]]; then
  args+=(--asset-id "$LIVE_CALIBRATION_ASSET_ID")
fi
if [[ -n "${LIVE_CALIBRATION_CLEAN_COHORT_ID:-}" ]]; then
  args+=(--clean-cohort-id "$LIVE_CALIBRATION_CLEAN_COHORT_ID")
fi
if [[ -n "${LIVE_CALIBRATION_FROZEN_MANIFEST:-}" ]]; then
  args+=(--frozen-manifest "$LIVE_CALIBRATION_FROZEN_MANIFEST")
fi
if [[ -n "${LIVE_CALIBRATION_SIDE:-}" ]]; then
  args+=(--side "$LIVE_CALIBRATION_SIDE")
fi
if [[ -n "${LIVE_CALIBRATION_ORDER_TYPE:-}" ]]; then
  args+=(--order-type "$LIVE_CALIBRATION_ORDER_TYPE")
fi
if [[ -n "${LIVE_CALIBRATION_AMOUNT:-}" ]]; then
  args+=(--amount "$LIVE_CALIBRATION_AMOUNT")
fi
if [[ -n "${LIVE_CALIBRATION_AMOUNT_UNIT:-}" ]]; then
  args+=(--amount-unit "$LIVE_CALIBRATION_AMOUNT_UNIT")
fi

python_bin="${LIVE_CALIBRATION_PYTHON_BIN:-python}"
exec "$python_bin" -m quant.calibration.run_plan "${args[@]}"
