#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/live_calibration_credentials.sh
live_load_db_credential
python_bin="${LIVE_CALIBRATION_PYTHON_BIN:-$HOME/.conda/envs/prediction-market-quant/bin/python}"
exec "$python_bin" -m quant.calibration.drift_monitor "$@"
