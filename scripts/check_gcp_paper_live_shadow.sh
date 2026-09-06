#!/usr/bin/env bash
set -euo pipefail

unit="${PAPER_LIVE_WATCHDOG_UNIT:-poly-quant-gcp-paper-live-shadow.service}"
status_path="${PAPER_LIVE_WATCHDOG_STATUS_PATH:-/mnt/l2-archive/.health/paper-live-shadow/status.json}"
db_host="${PAPER_LIVE_WATCHDOG_DB_HOST:-${POLYDATA_POSTGRES_HOST:-127.0.0.1}}"
db_port="${PAPER_LIVE_WATCHDOG_DB_PORT:-${PAPER_DB_LOCAL_PORT:-${POLYDATA_POSTGRES_PORT:-45435}}}"
# A live calibration intent can legitimately occupy the single paper worker for
# several minutes while DB-backed risk, lifecycle, and accounting checks run.
# Systemd already restarts a dead process immediately, so this watchdog only
# needs to catch an active-but-stalled worker without killing valid in-flight
# evidence collection.
max_age="${PAPER_LIVE_WATCHDOG_MAX_AGE_SECONDS:-420}"
recovery_wait="${PAPER_LIVE_WATCHDOG_RECOVERY_WAIT_SECONDS:-180}"
max_transport_idle="${PAPER_LIVE_WATCHDOG_MAX_TRANSPORT_IDLE_SECONDS:-180}"
health_script="${PAPER_LIVE_WATCHDOG_HEALTH_SCRIPT:-quant/paper/watchdog_health.py}"

emit() {
  printf '{"status":"%s","action":"%s","status_age_seconds":%s,"health_reason":"%s","observed_at":"%s"}\n' \
    "$1" "$2" "$3" "$4" "$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)"
}

mtime=0
if [[ -e "${status_path}" ]]; then
  mtime="$(stat -c %Y "${status_path}")"
fi
age=$(( $(date +%s) - mtime ))
health_reason="STATUS_FILE_MISSING"
health_rc=2
if [[ -e "${status_path}" ]]; then
  set +e
  health_reason="$(
    python3 "${health_script}" \
      --status-path "${status_path}" \
      --max-status-age-seconds "${max_age}" \
      --max-transport-idle-seconds "${max_transport_idle}" \
      --reason-only
  )"
  health_rc=$?
  set -e
fi

if systemctl --user is-active --quiet "${unit}" \
    && (( age <= max_age )) \
    && (( health_rc == 0 )); then
  emit PASS none "${age}" "${health_reason}"
  exit 0
fi

if ! timeout 3 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "${db_host}" "${db_port}"; then
  emit DEGRADED wait_for_db "${age}" "${health_reason}"
  exit 0
fi

systemctl --user restart "${unit}"
deadline=$(( $(date +%s) + recovery_wait ))
while (( $(date +%s) < deadline )); do
  sleep 2
  current_mtime=0
  if [[ -e "${status_path}" ]]; then
    current_mtime="$(stat -c %Y "${status_path}")"
  fi
  current_age=$(( $(date +%s) - current_mtime ))
  if systemctl --user is-active --quiet "${unit}" \
      && (( current_mtime > mtime )) \
      && (( current_age <= max_age )); then
    set +e
    current_reason="$(
      python3 "${health_script}" \
        --status-path "${status_path}" \
        --max-status-age-seconds "${max_age}" \
        --max-transport-idle-seconds "${max_transport_idle}" \
        --reason-only
    )"
    current_health_rc=$?
    set -e
    if (( current_health_rc == 0 )); then
      emit RECOVERED restart "${current_age}" "${current_reason}"
      exit 0
    fi
  fi
done

emit FAIL restart_timeout "$(( $(date +%s) - mtime ))" "${health_reason}" >&2
exit 1
