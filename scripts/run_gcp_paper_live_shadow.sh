#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/paper_runtime_credentials.sh
paper_load_runtime_credentials

export PYTHONUNBUFFERED=1
export OPENBLAS_NUM_THREADS="${PAPER_LIVE_OPENBLAS_NUM_THREADS:-1}"
export OMP_NUM_THREADS="${PAPER_LIVE_OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${PAPER_LIVE_MKL_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${PAPER_LIVE_NUMEXPR_NUM_THREADS:-1}"
if [[ -n "${PAPER_LIVE_POSTGRES_PORT:-}" ]]; then
  export POLYDATA_POSTGRES_PORT="${PAPER_LIVE_POSTGRES_PORT}"
fi
operations_admission_flag="${PAPER_OPERATIONS_ADMISSION_FLAG:---operations-admission-enforce}"
if [[ "$operations_admission_flag" != "--operations-admission-shadow" && "$operations_admission_flag" != "--operations-admission-enforce" ]]; then
  echo "invalid PAPER_OPERATIONS_ADMISSION_FLAG: $operations_admission_flag" >&2
  exit 2
fi
unified_admission_flag="${PAPER_UNIFIED_ADMISSION_FLAG:---unified-admission-shadow}"
if [[ "$unified_admission_flag" != "--unified-admission-shadow" && "$unified_admission_flag" != "--unified-admission-enforce" ]]; then
  echo "invalid PAPER_UNIFIED_ADMISSION_FLAG: $unified_admission_flag" >&2
  exit 2
fi

exec python -m quant.paper.live_shadow_service run \
  --paper-security-enforce \
  --paper-security-audit-log "${PAPER_SECURITY_AUDIT_LOG:-/mnt/l2-archive/.health/paper-live-shadow/security-audit.jsonl}" \
  --skip-schema-init \
  --external-event-socket "${PAPER_LIVE_GCP_EVENT_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/poly-quant/paper-events.sock}" \
  --external-subscription-file "${PAPER_LIVE_GCP_SUBSCRIPTION_FILE:-/mnt/l2-archive/control/subscriptions-execution-redundant.json}" \
  --external-sources "${PAPER_LIVE_GCP_EVENT_SOURCES:-primary,secondary}" \
  --external-feed-stale-seconds "${PAPER_LIVE_GCP_FEED_STALE_SECONDS:-30}" \
  --max-watch-assets "${PAPER_LIVE_MAX_WATCH_ASSETS:-512}" \
  --seed-watchlist-limit "${PAPER_LIVE_SEED_WATCHLIST_LIMIT:-120}" \
  --seed-reconcile-seconds "${PAPER_LIVE_SEED_RECONCILE_SECONDS:-30}" \
  --watch-refresh-seconds "${PAPER_LIVE_WATCH_REFRESH_SECONDS:-5}" \
  --intent-poll-seconds "${PAPER_LIVE_INTENT_POLL_SECONDS:-0.05}" \
  --health-seconds "${PAPER_LIVE_HEALTH_SECONDS:-2}" \
  --order-delay-ms "${PAPER_LIVE_ORDER_DELAY_MS:-100}" \
  --max-book-age-ms "${PAPER_LIVE_MAX_BOOK_AGE_MS:-60000}" \
  --fee-bps "${PAPER_LIVE_FEE_BPS:-0}" \
  --initial-cash "${PAPER_LIVE_INITIAL_CASH:-10000}" \
  --settlement-poll-seconds "${PAPER_LIVE_SETTLEMENT_POLL_SECONDS:-30}" \
  --rest-timeout-seconds "${PAPER_LIVE_REST_TIMEOUT_SECONDS:-10}" \
  --rest-retries "${PAPER_LIVE_REST_RETRIES:-2}" \
  --rest-resync-retry-seconds "${PAPER_LIVE_REST_RESYNC_RETRY_SECONDS:-5}" \
  --db-operation-timeout-seconds "${PAPER_LIVE_DB_OPERATION_TIMEOUT_SECONDS:-10}" \
  --persistent-db-connections \
  --authority-enforce \
  --authority-lease-seconds "${PAPER_LIVE_AUTHORITY_LEASE_SECONDS:-60}" \
  --authority-heartbeat-seconds "${PAPER_LIVE_AUTHORITY_HEARTBEAT_SECONDS:-10}" \
  --market-terms-ttl-seconds "${PAPER_LIVE_MARKET_TERMS_TTL_SECONDS:-300}" \
  --nav-snapshot-seconds "${PAPER_LIVE_NAV_SNAPSHOT_SECONDS:-5}" \
  --nav-history-seconds "${PAPER_LIVE_NAV_HISTORY_SECONDS:-60}" \
  --risk-max-order-notional "${PAPER_LIVE_RISK_MAX_ORDER_NOTIONAL:-1000}" \
  --risk-max-strategy-gross "${PAPER_LIVE_RISK_MAX_STRATEGY_GROSS:-10000}" \
  --risk-max-condition "${PAPER_LIVE_RISK_MAX_CONDITION:-2500}" \
  --risk-max-event "${PAPER_LIVE_RISK_MAX_EVENT:-5000}" \
  --risk-max-neg-risk-group "${PAPER_LIVE_RISK_MAX_NEG_RISK_GROUP:-5000}" \
  --risk-max-category "${PAPER_LIVE_RISK_MAX_CATEGORY:-7500}" \
  --risk-max-daily-loss "${PAPER_LIVE_RISK_MAX_DAILY_LOSS:-1000}" \
  --risk-max-open-orders "${PAPER_LIVE_RISK_MAX_OPEN_ORDERS:-100}" \
  --risk-max-order-rate-per-minute "${PAPER_LIVE_RISK_MAX_ORDER_RATE_PER_MINUTE:-120}" \
  --risk-max-visible-depth-ratio "${PAPER_LIVE_RISK_MAX_VISIBLE_DEPTH_RATIO:-1}" \
  "$operations_admission_flag" \
  --operations-status-path "${PAPER_OPERATIONS_STATUS_PATH:-/mnt/l2-archive/.health/paper-live-shadow/operations-status.json}" \
  --operations-status-max-age-seconds "${PAPER_OPERATIONS_STATUS_MAX_AGE_SECONDS:-90}" \
  --operations-yellow-max-notional "${PAPER_OPERATIONS_YELLOW_MAX_NOTIONAL:-20}" \
  --build-manifest "${PAPER_WORKER_BUILD_MANIFEST:-runtime_outputs/production/paper-worker-build.json}" \
  "$unified_admission_flag" \
  --admission-geoblock-proxy-url "${PAPER_GEOBLOCK_PROXY_URL:-}" \
  --admission-geoblock-timeout-seconds "${PAPER_GEOBLOCK_TIMEOUT_SECONDS:-5}" \
  --admission-geoblock-ttl-seconds "${PAPER_GEOBLOCK_TTL_SECONDS:-60}" \
  --history-size "${PAPER_LIVE_HISTORY_SIZE:-256}" \
  --venue-admission-enforce \
  --venue-heartbeat-timeout-seconds "${PAPER_VENUE_HEARTBEAT_TIMEOUT_SECONDS:-10}" \
  --venue-heartbeat-enforce-release \
  --lifecycle-scheduler-shadow \
  --lifecycle-scheduler-enforce-order \
  --durable-liquidity-overlay-enforce \
  --liquidity-overlay-version "${PAPER_LIQUIDITY_OVERLAY_VERSION:-paper-live-overlay-v1}" \
  --own-order-oms-enforce \
  --paper-account-id "${PAPER_OMS_ACCOUNT_ID:-paper-account}" \
  --fill-finality-shadow \
  --fill-finality-auto-reconcile \
  --simulator-run-id "${PAPER_SIMULATOR_RUN_ID:-}" \
  --status-path "${PAPER_LIVE_STATUS_PATH:-/mnt/l2-archive/.health/paper-live-shadow/status.json}" \
  --health-spool-path "${PAPER_LIVE_HEALTH_SPOOL_PATH:-/mnt/l2-archive/.health/paper-live-shadow/health-spool.jsonl}" \
  --shutdown-path "${PAPER_LIVE_SHUTDOWN_PATH:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/poly-quant/paper-live-shadow.stop}"
