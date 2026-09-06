#!/usr/bin/env bash
set -Eeuo pipefail

: "${PAPER_DB_SSH_TARGET:?PAPER_DB_SSH_TARGET is required}"
: "${PAPER_DB_REMOTE_HOST:?PAPER_DB_REMOTE_HOST is required}"

local_host="${PAPER_DB_LOCAL_HOST:-127.0.0.1}"
local_port="${PAPER_DB_LOCAL_PORT:-45435}"
remote_port="${PAPER_DB_REMOTE_PORT:-5432}"

exec /usr/bin/ssh \
  -N \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=20 \
  -o ServerAliveCountMax=6 \
  -L "${local_host}:${local_port}:${PAPER_DB_REMOTE_HOST}:${remote_port}" \
  "${PAPER_DB_SSH_TARGET}"
