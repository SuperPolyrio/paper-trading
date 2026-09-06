#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -eq 0 ]]; then
  printf 'paper Python entrypoint is required\n' >&2
  exit 64
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/paper_runtime_credentials.sh
source "${repo_root}/scripts/paper_runtime_credentials.sh"
paper_load_runtime_credentials

python_bin="${PAPER_RUNTIME_PYTHON_BIN:-python}"
exec "${python_bin}" "$@"
