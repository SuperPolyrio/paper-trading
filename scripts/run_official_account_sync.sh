#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
python_bin="${PREDICTION_MARKET_QUANT_PYTHON_BIN:-$HOME/.conda/envs/prediction-market-quant/bin/python}"
command_name="${1:-daemon}"
if [[ $# -gt 0 ]]; then
  shift
fi
exec "$python_bin" -m quant.simulator.rewards.official_sync_cli "$command_name" "$@"
