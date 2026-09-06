#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${POLY_QUANT_PYTHON_BIN:-/home/jiahuaiyu/.conda/envs/prediction-market-quant/bin/python}"

cd "${PROJECT_ROOT}"
exec "${PYTHON_BIN}" -m quant.simulator.account_truth.cli "$@"
