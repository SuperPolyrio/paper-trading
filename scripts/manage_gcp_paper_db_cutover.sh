#!/usr/bin/env bash
set -Eeuo pipefail

root="${POLY_QUANT_GCP_ROOT:-/opt/paper-trading}"
python_bin="${PAPER_DB_PYTHON:-/opt/polyData/.venv/bin/python}"
source_env="${PAPER_DB_SOURCE_ENV:-${HOME}/.config/polydata/polydata.env}"
target_env="${PAPER_DB_TARGET_ENV:-${HOME}/.config/prediction-market-quant/paper-db-target.env}"
active_env="${PAPER_DB_ACTIVE_ENV:-${HOME}/.config/prediction-market-quant/paper-db-active.env}"
rollback_env="${active_env}.rollback"
target_password_file="${PAPER_DB_TARGET_PASSWORD_FILE:-${HOME}/.config/prediction-market-quant/secrets/paper-db-target-password}"
runtime_password_file="${PAPER_DB_RUNTIME_PASSWORD_FILE:-${HOME}/.config/prediction-market-quant/secrets/paper-db-password}"
rollback_password_file="${runtime_password_file}.rollback"
artifact_dir="${PAPER_DB_ARTIFACT_DIR:-/mnt/l2-archive/.health/paper-db-migration}"
status_path="${PAPER_LIVE_STATUS_PATH:-/mnt/l2-archive/.health/paper-live-shadow/status.json}"
mode="${1:-}"

mkdir -p "${artifact_dir}" "$(dirname "${active_env}")"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

db() {
  local target_password
  test -s "${target_password_file}"
  test "$(stat -c %a "${target_password_file}")" = "600"
  IFS= read -r target_password < "${target_password_file}"
  test -n "${target_password}"
  PAPER_TARGET_POSTGRES_PASSWORD="${target_password}" \
    PYTHONPATH="${root}" "${python_bin}" -m quant.paper.db_migration \
    --env-file "${source_env}" --env-file "${target_env}" \
    --source-prefix POLYDATA_POSTGRES --target-prefix PAPER_TARGET_POSTGRES "$@"
}

validate_target_config() {
  test -s "${target_env}"
  if grep -Eq '^[[:space:]]*PAPER_TARGET_POSTGRES_PASSWORD=' "${target_env}"; then
    echo "target DB password must use PAPER_DB_TARGET_PASSWORD_FILE, not ${target_env}" >&2
    return 78
  fi
  test -s "${target_password_file}"
  test "$(stat -c %a "${target_password_file}")" = "600"
}

wait_for_drain() {
  "${python_bin}" - "${status_path}" <<'PY'
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        time.sleep(1)
        continue
    if int(status.get("queued_intents", -1)) == 0 and int(status.get("processing_intents", -1)) == 0:
        raise SystemExit(0)
    time.sleep(1)
raise SystemExit("paper command queue did not drain within 120 seconds")
PY
}

write_target_override() {
  set -a
  # shellcheck disable=SC1090
  source "${target_env}"
  set +a
  umask 077
  local temporary="${active_env}.tmp"
  {
    printf 'POLYDATA_POSTGRES_HOST=%q\n' "${PAPER_TARGET_POSTGRES_HOST:?}"
    printf 'POLYDATA_POSTGRES_PORT=%q\n' "${PAPER_TARGET_POSTGRES_PORT:?}"
    printf 'POLYDATA_POSTGRES_USER=%q\n' "${PAPER_TARGET_POSTGRES_USER:?}"
    printf 'POLYDATA_POSTGRES_DATABASE=%q\n' "${PAPER_TARGET_POSTGRES_DATABASE:?}"
    printf 'POLYDATA_POSTGRES_SEARCH_PATH=%q\n' "${PAPER_TARGET_POSTGRES_SEARCH_PATH:-quant,core,oracle,ops,public}"
    printf 'PAPER_LIVE_POSTGRES_PORT=\n'
    printf 'POLYDATA_QUANT_POSTGRES_CONNECT_TIMEOUT_SECONDS=%q\n' "${PAPER_TARGET_POSTGRES_CONNECT_TIMEOUT_SECONDS:-5}"
    printf 'POLYDATA_QUANT_POSTGRES_STATEMENT_TIMEOUT_MS=%q\n' "${PAPER_TARGET_POSTGRES_STATEMENT_TIMEOUT_MS:-20000}"
    printf 'POLYDATA_QUANT_POSTGRES_LOCK_TIMEOUT_MS=%q\n' "${PAPER_TARGET_POSTGRES_LOCK_TIMEOUT_MS:-5000}"
  } > "${temporary}"
  chmod 0600 "${temporary}"
  mv "${temporary}" "${active_env}"
}

restore_source() {
  if [[ -f "${rollback_env}" && ! -s "${rollback_password_file}" ]]; then
    echo "rollback route exists but rollback DB credential is missing" >&2
    return 78
  fi
  systemctl --user stop poly-quant-gcp-paper-health.service \
    poly-quant-gcp-paper-live-shadow.service 2>/dev/null || true
  if [[ -f "${rollback_env}" ]]; then
    mv "${rollback_env}" "${active_env}"
  else
    rm -f "${active_env}"
  fi
  if [[ -f "${rollback_password_file}" ]]; then
    install -m 0600 "${rollback_password_file}" "${runtime_password_file}"
    rm -f "${rollback_password_file}"
  fi
  systemctl --user daemon-reload
  systemctl --user start poly-quant-gcp-paper-live-shadow.service \
    poly-quant-gcp-paper-health.service
}

case "${mode}" in
  prepare)
    validate_target_config
    db preflight --require-private --allow-missing-migration \
      --output "${artifact_dir}/route-preflight-${stamp}.json"
    db apply-schema --output "${artifact_dir}/schema-${stamp}.json"
    db sync-catalog --output "${artifact_dir}/catalog-${stamp}.json"
    db sync --output "${artifact_dir}/snapshot-${stamp}.json"
    db verify --core-only --output "${artifact_dir}/parity-${stamp}.json"
    db latency --samples 20 --output "${artifact_dir}/latency-${stamp}.json"
    db preflight --require-private --output "${artifact_dir}/preflight-${stamp}.json"
    ;;
  shadow-verify)
    validate_target_config
    db verify --core-only --output "${artifact_dir}/shadow-parity-${stamp}.json"
    ;;
  cutover)
    [[ "${PAPER_DB_CUTOVER_APPROVE:-}" == "YES" ]] || {
      echo "set PAPER_DB_CUTOVER_APPROVE=YES after reviewing prepare artifacts" >&2
      exit 2
    }
    validate_target_config
    db preflight --require-private --output "${artifact_dir}/cutover-preflight-${stamp}.json"
    wait_for_drain
    rm -f "${rollback_env}" "${rollback_password_file}"
    [[ ! -f "${active_env}" ]] || cp -p "${active_env}" "${rollback_env}"
    test -s "${runtime_password_file}"
    cp -p "${runtime_password_file}" "${rollback_password_file}"
    chmod 0600 "${rollback_password_file}"
    trap restore_source ERR
    systemctl --user stop poly-quant-gcp-paper-health.service \
      poly-quant-gcp-paper-live-shadow.service
    db sync-catalog --output "${artifact_dir}/cutover-catalog-${stamp}.json"
    db sync --output "${artifact_dir}/cutover-sync-${stamp}.json"
    # Authority state blocks cutover. Large current-book/NAV read models are
    # copied above but are rebuildable and must not extend the drain window.
    db verify --core-only --output "${artifact_dir}/cutover-parity-${stamp}.json"
    write_target_override
    install -m 0600 "${target_password_file}" "${runtime_password_file}"
    systemctl --user daemon-reload
    systemctl --user start poly-quant-gcp-paper-live-shadow.service \
      poly-quant-gcp-paper-health.service
    sleep 5
    PYTHONPATH="${root}" "${python_bin}" -m quant.paper.production_runtime \
      canary --base-url http://127.0.0.1:18700 --require-ready \
      --output "${artifact_dir}/cutover-canary-${stamp}.json"
    trap - ERR
    ;;
  rollback)
    [[ "${PAPER_DB_ROLLBACK_APPROVE:-}" == "YES" ]] || {
      echo "set PAPER_DB_ROLLBACK_APPROVE=YES to restore the previous DB route" >&2
      exit 2
    }
    test -s "${rollback_password_file}" || {
      echo "rollback DB credential is missing: ${rollback_password_file}" >&2
      exit 78
    }
    restore_source
    ;;
  *)
    echo "usage: $0 {prepare|shadow-verify|cutover|rollback}" >&2
    exit 2
    ;;
esac
