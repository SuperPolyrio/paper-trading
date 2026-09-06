#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

python_bin="${PAPER_PAIRED_PYTHON_BIN:-${HOME}/.conda/envs/prediction-market-quant/bin/python}"
output_dir="${PAPER_PAIRED_OUTPUT_DIR:-runtime_outputs/paper_paired_probe}"
lock_path="${PAPER_PAIRED_LOCK_PATH:-${XDG_RUNTIME_DIR:-/tmp}/poly-quant-paper-paired-reconciler.lock}"
mkdir -p "${output_dir}" "$(dirname "${lock_path}")"

exec 9>"${lock_path}"
flock -n 9 || exit 0

"${python_bin}" scripts/run_paper_paired_probe.py sync-calibration \
  --limit "${PAPER_PAIRED_SYNC_LIMIT:-1000}" \
  --json-out "${output_dir}/reconciler-sync-latest.json" >/dev/null
"${python_bin}" scripts/run_paper_paired_probe.py reconcile-pending \
  --limit "${PAPER_PAIRED_RECONCILE_LIMIT:-20}" \
  --min-age-seconds "${PAPER_PAIRED_MIN_AGE_SECONDS:-30}" \
  --window-seconds "${PAPER_PAIRED_WINDOW_SECONDS:-300}" \
  --max-runtime-seconds "${PAPER_PAIRED_MAX_RUNTIME_SECONDS:-90}" \
  --write-retries "${PAPER_PAIRED_WRITE_RETRIES:-3}" \
  --json-out "${output_dir}/reconciler-orderfilled-latest.json" >/dev/null
"${python_bin}" scripts/run_paper_paired_probe.py report \
  --mode record-only \
  --limit "${PAPER_PAIRED_REPORT_LIMIT:-1000}" \
  --json-out "${output_dir}/reconciler-report-latest.json" >/dev/null
