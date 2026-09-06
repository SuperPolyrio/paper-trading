#!/usr/bin/env bash

live_load_credential() {
  local credential_id="$1"
  local environment_name="$2"
  : "${CREDENTIALS_DIRECTORY:?systemd credentials are required}"
  local path="${CREDENTIALS_DIRECTORY}/${credential_id}"
  if [[ ! -r "$path" ]]; then
    printf 'required calibration credential is unavailable: %s\n' "$credential_id" >&2
    return 78
  fi
  local value
  IFS= read -r value < "$path"
  if [[ -z "$value" ]]; then
    printf 'required calibration credential is empty: %s\n' "$credential_id" >&2
    return 78
  fi
  printf -v "$environment_name" '%s' "$value"
  export "$environment_name"
}

live_load_db_credential() {
  export POLY_QUANT_DISABLE_DOTENV=1
  live_load_credential live-db-password POLYDATA_POSTGRES_PASSWORD
}

live_load_trading_credentials() {
  live_load_credential live-private-key POLY_QUANT_PROBE_PRIVATE_KEY
  live_load_api_credentials
}

live_load_api_credentials() {
  live_load_credential live-api-key POLY_QUANT_PROBE_API_KEY
  live_load_credential live-api-secret POLY_QUANT_PROBE_API_SECRET
  live_load_credential live-api-passphrase POLY_QUANT_PROBE_API_PASSPHRASE
}

live_load_calibration_env() {
  local path="${1:-${HOME}/.config/prediction-market-quant/calibration.env}"
  if [[ ! -r "${path}" ]]; then
    printf 'required calibration env is unavailable: %s\n' "${path}" >&2
    return 78
  fi
  local mode
  mode="$(stat -c '%a' "${path}")"
  if [[ "${mode}" != "600" ]]; then
    printf 'calibration env must have mode 600: %s has %s\n' "${path}" "${mode}" >&2
    return 78
  fi
  local python_bin="${LIVE_CALIBRATION_PYTHON_BIN:-python}"
  local key value
  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    printf -v "${key}" '%s' "${value}"
    export "${key}"
  done < <(
    "${python_bin}" - "${path}" <<'PY'
import os
import sys
from dotenv import dotenv_values

required = (
    "POLY_QUANT_PROBE_PRIVATE_KEY",
    "POLY_QUANT_PROBE_API_KEY",
    "POLY_QUANT_PROBE_API_SECRET",
    "POLY_QUANT_PROBE_API_PASSPHRASE",
)
values = dotenv_values(sys.argv[1])
missing = [key for key in required if not values.get(key)]
if missing:
    raise SystemExit("missing required calibration keys: " + ",".join(missing))
for key in required:
    os.write(1, key.encode("utf-8") + b"\0" + str(values[key]).encode("utf-8") + b"\0")
PY
  )
  for key in \
    POLY_QUANT_PROBE_PRIVATE_KEY \
    POLY_QUANT_PROBE_API_KEY \
    POLY_QUANT_PROBE_API_SECRET \
    POLY_QUANT_PROBE_API_PASSPHRASE
  do
    if [[ -z "${!key:-}" ]]; then
      printf 'required calibration key did not load: %s\n' "${key}" >&2
      return 78
    fi
  done
}

live_load_paper_control_env() {
  local path="${1:-${HOME}/.config/prediction-market-quant/paper-db-control.env}"
  if [[ ! -r "${path}" ]]; then
    return 0
  fi
  local mode
  mode="$(stat -c '%a' "${path}")"
  if [[ "${mode}" != "600" ]]; then
    printf 'paper DB control env must have mode 600: %s has %s\n' "${path}" "${mode}" >&2
    return 78
  fi
  local python_bin="${LIVE_CALIBRATION_PYTHON_BIN:-python}"
  local key value
  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    printf -v "${key}" '%s' "${value}"
    export "${key}"
  done < <(
    "${python_bin}" - "${path}" <<'PY'
import os
import sys
from dotenv import dotenv_values

allowed = (
    "POLY_QUANT_PAPER_POSTGRES_HOST",
    "POLY_QUANT_PAPER_POSTGRES_PORT",
    "POLY_QUANT_PAPER_POSTGRES_USER",
    "POLY_QUANT_PAPER_POSTGRES_DATABASE",
    "POLY_QUANT_PAPER_POSTGRES_SEARCH_PATH",
    "POLY_QUANT_PAPER_POSTGRES_PASSWORD_FILE",
    "POLY_QUANT_PAPER_POSTGRES_CONNECT_TIMEOUT_SECONDS",
    "POLY_QUANT_PAPER_POSTGRES_STATEMENT_TIMEOUT_MS",
    "POLY_QUANT_PAPER_POSTGRES_LOCK_TIMEOUT_MS",
    "POLY_QUANT_PAPER_POSTGRES_TCP_USER_TIMEOUT_MS",
)
values = dotenv_values(sys.argv[1])
for key in allowed:
    value = values.get(key)
    if value is not None:
        os.write(1, key.encode("utf-8") + b"\0" + str(value).encode("utf-8") + b"\0")
PY
  )
}
