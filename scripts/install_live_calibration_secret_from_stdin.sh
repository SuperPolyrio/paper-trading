#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  printf 'usage: %s SECRET_NAME TARGET_PATH\n' "$0" >&2
  exit 64
fi

secret_name="$1"
target_path="$2"
case "$secret_name" in
  live-db-password|live-private-key|live-api-key|live-api-secret|live-api-passphrase) ;;
  *)
    printf 'unsupported live calibration secret name: %s\n' "$secret_name" >&2
    exit 64
    ;;
esac

umask 077
install -d -m 700 "$(dirname "$target_path")"
temporary="${target_path}.tmp.$$"
trap 'rm -f "$temporary"' EXIT
dd status=none of="$temporary"
if [[ ! -s "$temporary" ]]; then
  printf 'refusing to install an empty credential\n' >&2
  exit 65
fi
chmod 600 "$temporary"
mv -f "$temporary" "$target_path"
trap - EXIT
