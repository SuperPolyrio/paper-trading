#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${PROJECT_ROOT}/scripts/run_official_account_truth.sh"

if [[ -n "${POLY_QUANT_ACCOUNT_TRUTH_SCOPE_ID:-}" ]]; then
  command="delta-reconcile"
elif [[ -n "${POLY_QUANT_ACCOUNT_TRUTH_STRATEGY_IDS:-}" ]]; then
  command="reconcile"
else
  command="capture"
fi

exec "${RUNNER}" "${command}"
