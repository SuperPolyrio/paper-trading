#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

local_env="${BOOK_L2_GCP_LOCAL_ENV_FILE:-${HOME}/.config/prediction-market-quant/gcp-l2-batch-local.env}"
if [[ -f "${local_env}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${local_env}"
  set +a
fi

remote_target="${POLY_QUANT_GCP_SSH_TARGET:-jhuaiyu3@34.143.254.155}"
remote_root="${POLY_QUANT_GCP_ROOT:-/opt/paper-trading}"
remote_user="${remote_target%%@*}"
remote_home="${POLY_QUANT_GCP_HOME:-/home/${remote_user}}"
ssh_args=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
if [[ -n "${BOOK_L2_GCP_STANDARD_RELAY_TARGET:-}" ]]; then
  remote_target="${BOOK_L2_GCP_COLLECTOR_INTERNAL_TARGET:-jhuaiyu3@10.148.0.2}"
  ssh_args+=(
    -o "ProxyJump=${BOOK_L2_GCP_STANDARD_RELAY_TARGET}"
    -o "HostKeyAlias=${BOOK_L2_GCP_COLLECTOR_HOST_KEY_ALIAS:-polymonitor-web-singapore-01-internal}"
    -o ExitOnForwardFailure=yes
  )
fi

python_bin="/opt/polyData/.venv/bin/python"
health_dir="/mnt/l2-archive/.health/paper-live-shadow"
manifest_path="${remote_root}/runtime_outputs/production/paper-worker-build.json"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="${remote_root}/.paper-worker-rollbacks"
backup_path="${backup_dir}/${stamp}.tar.gz"
backup_meta="${backup_dir}/${stamp}.meta"
before_path="${health_dir}/deploy-state-before-${stamp}.json"
verify_path="${health_dir}/deploy-state-verify-${stamp}.json"
preflight_path="${health_dir}/preflight-${stamp}.json"
canary_path="${health_dir}/canary-${stamp}.json"
source_sha="$(git rev-parse HEAD 2>/dev/null || true)"
source_dirty=false
if [[ -n "$(git status --porcelain 2>/dev/null || true)" ]]; then
  source_dirty=true
fi

mapfile -t manifest_files < <(
  python -c \
    'from quant.paper.production_runtime import MANIFEST_FILES; print("\n".join(MANIFEST_FILES))'
)
sync_candidates=(
  quant
  scripts/install_gcp_paper_worker.sh
  scripts/check_gcp_paper_live_shadow.sh
  deploy/systemd/poly-quant-gcp-paper-live-shadow-watchdog.service
  deploy/systemd/poly-quant-gcp-paper-live-shadow-watchdog.timer
  deploy/systemd/paper-live-shadow.env.example
  "${manifest_files[@]}"
)
declare -A seen_sync_files=()
sync_files=()
for relative in "${sync_candidates[@]}"; do
  if [[ -z "${seen_sync_files[${relative}]:-}" ]]; then
    [[ -e "${relative}" ]] || {
      printf 'required paper worker deploy file is missing: %s\n' "${relative}" >&2
      exit 1
    }
    seen_sync_files["${relative}"]=1
    sync_files+=("${relative}")
  fi
done

remote() {
  ssh "${ssh_args[@]}" "${remote_target}" "$@"
}

rollback() {
  status=$?
  trap - ERR
  printf 'paper worker deployment failed; restoring %s\n' "${backup_path}" >&2
  remote bash -s -- \
    "${remote_root}" "${backup_path}" "${backup_meta}" \
    "${sync_files[@]}" <<'REMOTE'
set -euo pipefail
root="$1"
backup="$2"
meta="$3"
shift 3
health_preexisting=0
catalog_sync_preexisting=0
operations_preexisting=0
if [[ -f "${meta}" ]]; then
  # shellcheck disable=SC1090
  source "${meta}"
fi
# Never turn a failed connection or an incomplete bootstrap backup into a
# destructive remote delete. A rollback may remove deployed files only after
# the archive has been proven readable.
if [[ ! -s "${backup}" ]] || ! tar -tzf "${backup}" >/dev/null 2>&1; then
  printf 'paper worker rollback skipped: backup is missing or invalid: %s\n' \
    "${backup}" >&2
  exit 1
fi
systemctl --user stop poly-quant-execution-catalog-sync.timer 2>/dev/null || true
systemctl --user stop poly-quant-execution-catalog-sync.service 2>/dev/null || true
systemctl --user stop poly-quant-gcp-paper-live-shadow-watchdog.timer 2>/dev/null || true
systemctl --user stop poly-quant-gcp-paper-live-shadow-watchdog.service 2>/dev/null || true
systemctl --user stop poly-quant-gcp-paper-operations-snapshot.timer 2>/dev/null || true
systemctl --user stop poly-quant-gcp-paper-operations-snapshot.service 2>/dev/null || true
systemctl --user stop poly-quant-gcp-paper-health.service 2>/dev/null || true
systemctl --user stop poly-quant-gcp-paper-live-shadow.service 2>/dev/null || true
for relative in "$@"; do
  rm -rf "${root:?}/${relative}"
done
rm -f "${root}/runtime_outputs/production/paper-worker-build.json"
tar -xzf "${backup}" -C "${root}"
install -m 0644 "${root}/deploy/systemd/poly-quant-gcp-paper-live-shadow.service" \
  ~/.config/systemd/user/poly-quant-gcp-paper-live-shadow.service
for unit in \
  poly-quant-gcp-paper-soak-6h.service \
  poly-quant-gcp-paper-soak-24h.service \
  poly-quant-gcp-paper-soak-7d.service; do
  if [[ -f "${root}/deploy/systemd/${unit}" ]]; then
    install -m 0644 "${root}/deploy/systemd/${unit}" \
      "${HOME}/.config/systemd/user/${unit}"
  else
    rm -f "${HOME}/.config/systemd/user/${unit}"
  fi
done
if [[ "${health_preexisting}" == "1" && -f "${root}/deploy/systemd/poly-quant-gcp-paper-health.service" ]]; then
  install -m 0644 "${root}/deploy/systemd/poly-quant-gcp-paper-health.service" \
    ~/.config/systemd/user/poly-quant-gcp-paper-health.service
else
  systemctl --user disable poly-quant-gcp-paper-health.service 2>/dev/null || true
  rm -f ~/.config/systemd/user/poly-quant-gcp-paper-health.service
fi
if [[ "${catalog_sync_preexisting}" == "1" ]] && \
   [[ -f "${root}/deploy/systemd/poly-quant-execution-catalog-sync.service" ]] && \
   [[ -f "${root}/deploy/systemd/poly-quant-execution-catalog-sync.timer" ]]; then
  install -m 0644 \
    "${root}/deploy/systemd/poly-quant-execution-catalog-sync.service" \
    ~/.config/systemd/user/poly-quant-execution-catalog-sync.service
  install -m 0644 \
    "${root}/deploy/systemd/poly-quant-execution-catalog-sync.timer" \
    ~/.config/systemd/user/poly-quant-execution-catalog-sync.timer
else
  systemctl --user disable poly-quant-execution-catalog-sync.timer \
    2>/dev/null || true
  rm -f \
    ~/.config/systemd/user/poly-quant-execution-catalog-sync.service \
    ~/.config/systemd/user/poly-quant-execution-catalog-sync.timer
fi
if [[ "${operations_preexisting}" == "1" ]] && \
   [[ -f "${root}/deploy/systemd/poly-quant-gcp-paper-operations-snapshot.service" ]] && \
   [[ -f "${root}/deploy/systemd/poly-quant-gcp-paper-operations-snapshot.timer" ]]; then
  install -m 0644 \
    "${root}/deploy/systemd/poly-quant-gcp-paper-operations-snapshot.service" \
    ~/.config/systemd/user/poly-quant-gcp-paper-operations-snapshot.service
  install -m 0644 \
    "${root}/deploy/systemd/poly-quant-gcp-paper-operations-snapshot.timer" \
    ~/.config/systemd/user/poly-quant-gcp-paper-operations-snapshot.timer
else
  systemctl --user disable poly-quant-gcp-paper-operations-snapshot.timer \
    2>/dev/null || true
  rm -f \
    ~/.config/systemd/user/poly-quant-gcp-paper-operations-snapshot.service \
    ~/.config/systemd/user/poly-quant-gcp-paper-operations-snapshot.timer
fi
systemctl --user daemon-reload
systemctl --user enable --now poly-quant-gcp-paper-live-shadow.service
systemctl --user enable --now poly-quant-gcp-paper-live-shadow-watchdog.timer
if [[ "${health_preexisting}" == "1" ]]; then
  systemctl --user enable --now poly-quant-gcp-paper-health.service
fi
if [[ "${catalog_sync_preexisting}" == "1" ]]; then
  systemctl --user enable --now poly-quant-execution-catalog-sync.timer
fi
if [[ "${operations_preexisting}" == "1" ]]; then
  systemctl --user enable --now poly-quant-gcp-paper-operations-snapshot.timer
fi
REMOTE
  exit "${status}"
}
trap rollback ERR

remote 'mountpoint -q /mnt/l2-archive && test -x /opt/polyData/.venv/bin/python'
remote "sudo install -d -m 0755 -o \"\$(id -un)\" -g \"\$(id -gn)\" '${remote_root}' && install -d -m 0755 '${backup_dir}' '${health_dir}' '${remote_root}/runtime_outputs/production'"

remote bash -s -- \
  "${remote_root}" "${backup_path}" "${backup_meta}" \
  "${sync_files[@]}" <<'REMOTE'
set -euo pipefail
root="$1"
backup="$2"
meta="$3"
shift 3
health_preexisting=0
[[ -f "${root}/deploy/systemd/poly-quant-gcp-paper-health.service" ]] && health_preexisting=1
catalog_sync_preexisting=0
[[ -f "${root}/deploy/systemd/poly-quant-execution-catalog-sync.timer" ]] && \
  catalog_sync_preexisting=1
operations_preexisting=0
[[ -f "${root}/deploy/systemd/poly-quant-gcp-paper-operations-snapshot.timer" ]] && \
  operations_preexisting=1
{
  printf 'health_preexisting=%s\n' "${health_preexisting}"
  printf 'catalog_sync_preexisting=%s\n' "${catalog_sync_preexisting}"
  printf 'operations_preexisting=%s\n' "${operations_preexisting}"
} > "${meta}"
tar --ignore-failed-read -czf "${backup}" -C "${root}" \
  "$@" runtime_outputs/production/paper-worker-build.json
REMOTE

rsync -a --relative \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  -e "ssh ${ssh_args[*]}" \
  "${sync_files[@]}" \
  "${remote_target}:${remote_root}/"

remote bash -s -- \
  "${remote_root}" "${python_bin}" "${manifest_path}" "${source_sha}" "${source_dirty}" \
  "${preflight_path}" "${before_path}" <<'REMOTE'
set -euo pipefail
root="$1"
python_bin="$2"
manifest="$3"
source_sha="$4"
source_dirty="$5"
preflight="$6"
before="$7"
export PYTHONPATH="${root}"
chmod +x \
  "${root}/scripts/install_gcp_paper_worker.sh" \
  "${root}/scripts/manage_gcp_paper_db_cutover.sh" \
  "${root}/scripts/paper_runtime_credentials.sh" \
  "${root}/scripts/run_gcp_paper_live_shadow.sh" \
  "${root}/scripts/run_gcp_paper_final_soak.sh" \
  "${root}/scripts/run_paper_execution_catalog_sync.sh" \
  "${root}/scripts/run_paper_operations_snapshot.sh" \
  "${root}/scripts/run_paper_health_secure.sh" \
  "${root}/scripts/check_gcp_paper_live_shadow.sh"
install -d -m 0700 \
  ~/.config/prediction-market-quant \
  ~/.config/prediction-market-quant/secrets \
  ~/.config/systemd/user
if [[ ! -f ~/.config/prediction-market-quant/paper-runtime.env ]]; then
  install -m 0600 "${root}/deploy/systemd/paper-runtime.env.example" \
    ~/.config/prediction-market-quant/paper-runtime.env
fi
if [[ ! -f ~/.config/prediction-market-quant/paper-live-shadow.env ]]; then
  install -m 0600 "${root}/deploy/systemd/paper-live-shadow.env.example" \
    ~/.config/prediction-market-quant/paper-live-shadow.env
fi
test -s ~/.config/prediction-market-quant/secrets/paper-db-password
test "$(stat -c %a ~/.config/prediction-market-quant/secrets/paper-db-password)" = "600"
install -m 0644 "${root}/deploy/systemd/poly-quant-gcp-paper-live-shadow.service" \
  ~/.config/systemd/user/poly-quant-gcp-paper-live-shadow.service
install -m 0644 "${root}/deploy/systemd/poly-quant-gcp-paper-soak-6h.service" \
  ~/.config/systemd/user/poly-quant-gcp-paper-soak-6h.service
install -m 0644 "${root}/deploy/systemd/poly-quant-gcp-paper-soak-24h.service" \
  ~/.config/systemd/user/poly-quant-gcp-paper-soak-24h.service
install -m 0644 "${root}/deploy/systemd/poly-quant-gcp-paper-soak-7d.service" \
  ~/.config/systemd/user/poly-quant-gcp-paper-soak-7d.service
install -m 0644 "${root}/deploy/systemd/poly-quant-gcp-paper-health.service" \
  ~/.config/systemd/user/poly-quant-gcp-paper-health.service
install -m 0644 \
  "${root}/deploy/systemd/poly-quant-gcp-paper-operations-snapshot.service" \
  ~/.config/systemd/user/poly-quant-gcp-paper-operations-snapshot.service
install -m 0644 \
  "${root}/deploy/systemd/poly-quant-gcp-paper-operations-snapshot.timer" \
  ~/.config/systemd/user/poly-quant-gcp-paper-operations-snapshot.timer
install -m 0644 "${root}/deploy/systemd/poly-quant-gcp-paper-live-shadow-watchdog.service" \
  ~/.config/systemd/user/poly-quant-gcp-paper-live-shadow-watchdog.service
install -m 0644 "${root}/deploy/systemd/poly-quant-gcp-paper-live-shadow-watchdog.timer" \
  ~/.config/systemd/user/poly-quant-gcp-paper-live-shadow-watchdog.timer
install -m 0644 \
  "${root}/deploy/systemd/poly-quant-execution-catalog-sync.service" \
  ~/.config/systemd/user/poly-quant-execution-catalog-sync.service
install -m 0644 \
  "${root}/deploy/systemd/poly-quant-execution-catalog-sync.timer" \
  ~/.config/systemd/user/poly-quant-execution-catalog-sync.timer
systemctl --user daemon-reload
set -a
# shellcheck disable=SC1090
source ~/.config/prediction-market-quant/paper-runtime.env
# shellcheck disable=SC1090
source ~/.config/prediction-market-quant/paper-live-shadow.env
# shellcheck disable=SC1091
[[ ! -f ~/.config/prediction-market-quant/paper-db-active.env ]] || \
  source ~/.config/prediction-market-quant/paper-db-active.env
set +a
export PAPER_DB_PASSWORD_CREDENTIAL_PATH=~/.config/prediction-market-quant/secrets/paper-db-password
export PAPER_SECURITY_REQUIRE_DB_CREDENTIAL_FILE=1
# shellcheck disable=SC1090
source "${root}/scripts/paper_runtime_credentials.sh"
paper_load_runtime_credentials
env_file_args=(
  --env-file ~/.config/prediction-market-quant/paper-runtime.env
  --env-file ~/.config/prediction-market-quant/paper-live-shadow.env
)
if [[ -f ~/.config/prediction-market-quant/paper-db-active.env ]]; then
  env_file_args+=(--env-file ~/.config/prediction-market-quant/paper-db-active.env)
fi
"${python_bin}" -m quant.paper.production_runtime manifest \
  --root "${root}" \
  --output "${manifest}" \
  --source-git-sha "${source_sha}" \
  --source-dirty "${source_dirty}"
preflight_ok=false
for attempt in $(seq 1 5); do
  if "${python_bin}" -m quant.paper.production_runtime \
    "${env_file_args[@]}" \
    preflight --skip-db --skip-event-socket \
    --root "${root}" --manifest "${manifest}" \
    --output "${preflight}"; then
    preflight_ok=true
    break
  fi
  [[ "${attempt}" == "5" ]] || sleep 3
done
if [[ "${preflight_ok}" != "true" ]]; then
  cat "${preflight}" >&2 2>/dev/null || true
  exit 1
fi
for attempt in $(seq 1 5); do
  if "${python_bin}" -m quant.paper.production_runtime \
    "${env_file_args[@]}" \
    state-snapshot --output "${before}"; then
    break
  fi
  if [[ "${attempt}" == "5" ]]; then
    exit 1
  fi
  sleep 3
done
REMOTE

if [[ "${PAPER_WORKER_DEPLOY_FAILPOINT:-}" == "after_preflight" ]]; then
  printf 'triggering deployment acceptance failpoint: after_preflight\n' >&2
  false
fi

# Prefer a clean drain before SIGTERM. If a paper-only command is wedged by the
# version being replaced, startup recovery will inspect durable audit evidence
# and either terminalize it or safely requeue it. The new build must then prove
# that the recovered queue drains before deployment is accepted.
if ! remote "${python_bin}" - "${health_dir}/status.json" \
  "${PAPER_WORKER_PRE_DEPLOY_DRAIN_SECONDS:-120}" <<'PY'
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
timeout = max(1.0, float(sys.argv[2]))
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    try:
        status = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        time.sleep(1)
        continue
    if "queued_intents" not in status or "processing_intents" not in status:
        raise SystemExit("status does not expose drain counters")
    if int(status["queued_intents"]) == 0 and int(status["processing_intents"]) == 0:
        raise SystemExit(0)
    time.sleep(1)
raise SystemExit(f"paper command queue did not drain within {timeout:g} seconds")
PY
then
  printf 'paper queue did not drain cleanly; continuing with durable startup recovery\n' >&2
fi

remote 'systemctl --user stop poly-quant-execution-catalog-sync.timer poly-quant-execution-catalog-sync.service 2>/dev/null || true; systemctl --user stop poly-quant-gcp-paper-live-shadow-watchdog.timer poly-quant-gcp-paper-live-shadow-watchdog.service 2>/dev/null || true; systemctl --user stop poly-quant-gcp-paper-live-shadow.service; ! systemctl --user is-active --quiet poly-quant-gcp-paper-live-shadow.service'
remote bash -s -- \
  "${remote_root}" "${python_bin}" "${remote_home}" "${manifest_path}" \
  "${preflight_path}" <<'REMOTE'
set -euo pipefail
root="$1"
python_bin="$2"
home_dir="$3"
manifest="$4"
preflight="$5"
set -a
# shellcheck disable=SC1090
source "${home_dir}/.config/prediction-market-quant/paper-runtime.env"
# shellcheck disable=SC1090
source "${home_dir}/.config/prediction-market-quant/paper-live-shadow.env"
# shellcheck disable=SC1091
[[ ! -f "${home_dir}/.config/prediction-market-quant/paper-db-active.env" ]] || \
  source "${home_dir}/.config/prediction-market-quant/paper-db-active.env"
set +a
export PYTHONPATH="${root}"
export PAPER_DB_PASSWORD_CREDENTIAL_PATH="${home_dir}/.config/prediction-market-quant/secrets/paper-db-password"
export PAPER_SECURITY_REQUIRE_DB_CREDENTIAL_FILE=1
# shellcheck disable=SC1090
source "${root}/scripts/paper_runtime_credentials.sh"
paper_load_runtime_credentials
"${python_bin}" -m quant.paper.live_shadow_service init-schema
env_file_args=(
  --env-file "${home_dir}/.config/prediction-market-quant/paper-runtime.env"
  --env-file "${home_dir}/.config/prediction-market-quant/paper-live-shadow.env"
)
if [[ -f "${home_dir}/.config/prediction-market-quant/paper-db-active.env" ]]; then
  env_file_args+=(--env-file "${home_dir}/.config/prediction-market-quant/paper-db-active.env")
fi
"${python_bin}" -m quant.paper.production_runtime \
  "${env_file_args[@]}" \
  preflight --skip-event-socket \
  --root "${root}" --manifest "${manifest}" --output "${preflight}"
REMOTE
remote rm -f "${health_dir}/status.json"
remote 'systemctl --user enable poly-quant-gcp-paper-live-shadow.service poly-quant-gcp-paper-health.service poly-quant-gcp-paper-operations-snapshot.timer && systemctl --user start poly-quant-gcp-paper-live-shadow.service && systemctl --user restart poly-quant-gcp-paper-health.service && systemctl --user start poly-quant-gcp-paper-operations-snapshot.service && systemctl --user start poly-quant-gcp-paper-operations-snapshot.timer'

remote bash -s -- "${health_dir}/status.json" <<'REMOTE'
set -euo pipefail
status_path="$1"
for _ in $(seq 1 90); do
  if systemctl --user is-active --quiet poly-quant-gcp-paper-live-shadow.service && \
     systemctl --user is-active --quiet poly-quant-gcp-paper-health.service && \
     test -s "${status_path}"; then
    exit 0
  fi
  sleep 1
done
systemctl --user --no-pager status poly-quant-gcp-paper-live-shadow.service || true
systemctl --user --no-pager status poly-quant-gcp-paper-health.service || true
exit 1
REMOTE

remote "${python_bin}" - "${health_dir}/status.json" \
  "${PAPER_WORKER_POST_DEPLOY_DRAIN_SECONDS:-300}" <<'PY'
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
timeout = max(1.0, float(sys.argv[2]))
deadline = time.monotonic() + timeout
last = None
while time.monotonic() < deadline:
    try:
        status = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        time.sleep(1)
        continue
    if "queued_intents" not in status or "processing_intents" not in status:
        raise SystemExit("status does not expose post-deploy drain counters")
    last = {
        "queued_intents": int(status["queued_intents"]),
        "processing_intents": int(status["processing_intents"]),
        "updated_at": status.get("updated_at"),
    }
    if last["queued_intents"] == 0 and last["processing_intents"] == 0:
        raise SystemExit(0)
    time.sleep(1)
raise SystemExit(
    "recovered paper command queue did not drain after deployment: "
    + json.dumps(last, sort_keys=True)
)
PY

build_id="$(remote "${python_bin}" - "${manifest_path}" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1], encoding="utf-8"))["build_id"])
PY
)"
remote bash -s -- \
  "${remote_root}" "${python_bin}" "${remote_home}" "${before_path}" "${verify_path}" <<'REMOTE'
set -euo pipefail
root="$1"
python_bin="$2"
home_dir="$3"
before="$4"
verify="$5"
set -a
# shellcheck disable=SC1090
source "${home_dir}/.config/prediction-market-quant/paper-runtime.env"
# shellcheck disable=SC1090
source "${home_dir}/.config/prediction-market-quant/paper-live-shadow.env"
# shellcheck disable=SC1091
[[ ! -f "${home_dir}/.config/prediction-market-quant/paper-db-active.env" ]] || \
  source "${home_dir}/.config/prediction-market-quant/paper-db-active.env"
set +a
export PYTHONPATH="${root}"
export PAPER_DB_PASSWORD_CREDENTIAL_PATH="${home_dir}/.config/prediction-market-quant/secrets/paper-db-password"
export PAPER_SECURITY_REQUIRE_DB_CREDENTIAL_FILE=1
# shellcheck disable=SC1090
source "${root}/scripts/paper_runtime_credentials.sh"
paper_load_runtime_credentials
for attempt in $(seq 1 5); do
  if "${python_bin}" -m quant.paper.production_runtime \
    verify-state --before "${before}" --output "${verify}"; then
    break
  fi
  if [[ "${attempt}" == "5" ]]; then
    cat "${verify}" >&2 2>/dev/null || true
    exit 1
  fi
  sleep 3
done
REMOTE
canary_ok=false
for attempt in $(seq 1 5); do
  if remote env PYTHONPATH="${remote_root}" "${python_bin}" \
    -m quant.paper.production_runtime canary \
    --expected-build-id "${build_id}" --output "${canary_path}"; then
    canary_ok=true
    break
  fi
  [[ "${attempt}" == "5" ]] || sleep 3
done
if [[ "${canary_ok}" != "true" ]]; then
  remote cat "${canary_path}" >&2 2>/dev/null || true
  exit 1
fi

remote 'systemctl --user start poly-quant-execution-catalog-sync.service && ! systemctl --user is-failed --quiet poly-quant-execution-catalog-sync.service'
remote 'systemctl --user enable --now poly-quant-gcp-paper-live-shadow-watchdog.timer poly-quant-execution-catalog-sync.timer poly-quant-gcp-paper-operations-snapshot.timer && systemctl --user is-enabled --quiet poly-quant-gcp-paper-live-shadow.service && systemctl --user is-active --quiet poly-quant-gcp-paper-live-shadow.service && systemctl --user is-enabled --quiet poly-quant-gcp-paper-health.service && systemctl --user is-active --quiet poly-quant-gcp-paper-health.service && systemctl --user is-enabled --quiet poly-quant-gcp-paper-live-shadow-watchdog.timer && systemctl --user is-active --quiet poly-quant-gcp-paper-live-shadow-watchdog.timer && systemctl --user is-enabled --quiet poly-quant-execution-catalog-sync.timer && systemctl --user is-active --quiet poly-quant-execution-catalog-sync.timer && systemctl --user is-enabled --quiet poly-quant-gcp-paper-operations-snapshot.timer && systemctl --user is-active --quiet poly-quant-gcp-paper-operations-snapshot.timer'
trap - ERR

printf 'paper worker deploy complete\n'
printf 'build_id=%s\n' "${build_id}"
printf 'preflight=%s\n' "${preflight_path}"
printf 'state_verification=%s\n' "${verify_path}"
printf 'canary=%s\n' "${canary_path}"
