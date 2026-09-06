#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

python_bin="${LIVE_CALIBRATION_PYTHON_BIN:-${HOME}/.conda/envs/prediction-market-quant/bin/python}"
export LIVE_CALIBRATION_PYTHON_BIN="${python_bin}"
paper_control_env="${PAPER_DB_CONTROL_ENV:-${HOME}/.config/prediction-market-quant/paper-db-control.env}"
calibration_env="${LIVE_CALIBRATION_ENV_FILE:-${HOME}/.config/prediction-market-quant/calibration.env}"

test -x "${python_bin}"
source scripts/live_calibration_credentials.sh
live_load_paper_control_env "${paper_control_env}"
live_load_calibration_env "${calibration_env}"

exec "${python_bin}" scripts/run_maker_live_probe.py "$@"
