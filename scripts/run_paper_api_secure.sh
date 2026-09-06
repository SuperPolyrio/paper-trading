#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/paper_runtime_credentials.sh
source "${repo_root}/scripts/paper_runtime_credentials.sh"
paper_load_runtime_credentials

if [[ -z "${PAPER_API_KEY_PEPPER_FILE:-}" && -n "${CREDENTIALS_DIRECTORY:-}" ]]; then
  export PAPER_API_KEY_PEPPER_FILE="${CREDENTIALS_DIRECTORY}/paper-api-key-pepper"
fi
if [[ -z "${PAPER_API_KEY_PEPPER_FILE:-}" ]]; then
  local_config_root="${XDG_CONFIG_HOME:-${HOME}/.config}"
  default_pepper_file="${local_config_root}/prediction-market-quant/secrets/paper-api-key-pepper"
  if [[ -r "${default_pepper_file}" ]]; then
    export PAPER_API_KEY_PEPPER_FILE="${default_pepper_file}"
  fi
fi
if [[ -z "${PAPER_API_KEY_PEPPER_FILE:-}" || ! -r "${PAPER_API_KEY_PEPPER_FILE}" ]]; then
  printf 'paper API pepper credential file is required but unavailable\n' >&2
  exit 78
fi

python_bin="${PAPER_RUNTIME_PYTHON_BIN:-python}"
exec "$python_bin" "${repo_root}/scripts/paper_api_server.py" "$@"
