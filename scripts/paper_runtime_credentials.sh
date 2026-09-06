#!/usr/bin/env bash

# Source this file before starting a paper-only process.
paper_load_runtime_credentials() {
  export POLY_QUANT_DISABLE_DOTENV=1

  local credential_path="${PAPER_DB_PASSWORD_CREDENTIAL_PATH:-}"
  if [[ -z "$credential_path" && -n "${CREDENTIALS_DIRECTORY:-}" ]]; then
    credential_path="${CREDENTIALS_DIRECTORY}/paper-db-password"
  fi

  if [[ -n "$credential_path" && -r "$credential_path" ]]; then
    IFS= read -r POLYDATA_POSTGRES_PASSWORD < "$credential_path"
    export POLYDATA_POSTGRES_PASSWORD
  elif [[ "${PAPER_SECURITY_REQUIRE_DB_CREDENTIAL_FILE:-0}" == "1" ]]; then
    printf 'paper DB credential file is required but unavailable\n' >&2
    return 78
  fi
}
