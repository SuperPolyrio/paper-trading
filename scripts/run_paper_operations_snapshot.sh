#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/paper_runtime_credentials.sh
paper_load_runtime_credentials

python_bin="${PREDICTION_MARKET_QUANT_PYTHON_BIN:-$HOME/.conda/envs/prediction-market-quant/bin/python}"
"$python_bin" -m quant.paper.security runtime-enforce \
  --audit-log "${PAPER_SECURITY_AUDIT_LOG:-runtime_outputs/security/paper-security-audit.jsonl}" \
  >/dev/null
args=(
  --window-minutes "${PAPER_OPERATIONS_WINDOW_MINUTES:-15}"
  --status-path "${PAPER_LIVE_STATUS_PATH:-runtime_outputs/paper_live_shadow/status.json}"
  --manifest-path "${PAPER_WORKER_BUILD_MANIFEST:-runtime_outputs/production/paper-worker-build.json}"
  --output-dir "${PAPER_OPERATIONS_OUTPUT_DIR:-runtime_outputs/paper_operations}"
)

if [[ "${PAPER_OPERATIONS_STRICT_EXIT:-0}" == "1" ]]; then
  args+=(--strict-exit)
fi

"$python_bin" -m quant.paper.operations "${args[@]}"

output_dir="${PAPER_OPERATIONS_OUTPUT_DIR:-runtime_outputs/paper_operations}"
publish_artifact() {
  local source_path="$1"
  local target_path="$2"
  [[ -n "$target_path" ]] || return 0
  [[ "$source_path" != "$target_path" ]] || return 0
  install -d -m 0755 "$(dirname "$target_path")"
  local temporary="${target_path}.tmp.$$"
  install -m 0644 "$source_path" "$temporary"
  mv -f "$temporary" "$target_path"
}

publish_artifact "${output_dir}/status.json" "${PAPER_OPERATIONS_STATUS_PATH:-}"
publish_artifact "${output_dir}/metrics.prom" "${PAPER_OPERATIONS_METRICS_PATH:-}"
