#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

python_bin="${POLY_QUANT_PYTHON_BIN:-${HOME}/.conda/envs/prediction-market-quant/bin/python}"
source_env="${PAPER_CATALOG_SOURCE_ENV:-${PWD}/.env}"
target_env="${PAPER_CATALOG_TARGET_ENV:-${HOME}/.config/prediction-market-quant/paper-db-target-local-tunnel.env}"
password_file="${PAPER_CATALOG_TARGET_PASSWORD_FILE:-${HOME}/.config/prediction-market-quant/secrets/paper-db-target-password}"
output_dir="${PAPER_CATALOG_SYNC_OUTPUT_DIR:-${PWD}/runtime_outputs/production/paper-db-gcp-migration/catalog-sync}"
lock_path="${PAPER_CATALOG_SYNC_LOCK_PATH:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/poly-quant-paper-catalog-sync.lock}"

test -x "${python_bin}"
test -r "${source_env}"
test -r "${target_env}"
test -r "${password_file}"
install -d -m 0700 "${output_dir}" "$(dirname "${lock_path}")"

exec 9>"${lock_path}"
if ! flock -n 9; then
  exit 0
fi

export PAPER_TARGET_POSTGRES_PASSWORD
PAPER_TARGET_POSTGRES_PASSWORD="$(<"${password_file}")"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
source_override_args=()
if [[ -n "${PAPER_CATALOG_SOURCE_HOST_OVERRIDE:-}" ]]; then
  source_override_args+=(
    --source-host-override "${PAPER_CATALOG_SOURCE_HOST_OVERRIDE}"
  )
fi
if [[ -n "${PAPER_CATALOG_SOURCE_PORT_OVERRIDE:-}" ]]; then
  source_override_args+=(
    --source-port-override "${PAPER_CATALOG_SOURCE_PORT_OVERRIDE}"
  )
fi

exec "${python_bin}" -m quant.paper.db_migration \
  --env-file "${source_env}" \
  --env-file "${target_env}" \
  --source-prefix POLYDATA_POSTGRES \
  "${source_override_args[@]}" \
  sync-catalog \
  --output "${output_dir}/catalog-${stamp}.json"
