#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/live_calibration_credentials.sh
live_load_db_credential
live_load_credential live-private-key POLY_QUANT_PROBE_PRIVATE_KEY
python_bin="${LIVE_CALIBRATION_PYTHON_BIN:-$HOME/.conda/envs/prediction-market-quant/bin/python}"
exec "$python_bin" scripts/watch_calibration_settlements.py --env-file /dev/null "$@"
