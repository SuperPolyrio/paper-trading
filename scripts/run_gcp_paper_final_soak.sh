#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python_bin="${PREDICTION_MARKET_QUANT_PYTHON_BIN:-/opt/polyData/.venv/bin/python}"
worker_status="${PAPER_LIVE_STATUS_PATH:-/mnt/l2-archive/.health/paper-live-shadow/status.json}"
operations_status="${PAPER_OPERATIONS_STATUS_PATH:-/mnt/l2-archive/.health/paper-live-shadow/operations-status.json}"
timeout_seconds="${PAPER_FINAL_SOAK_PREFLIGHT_SECONDS:-1800}"
interval_seconds="${PAPER_FINAL_SOAK_PREFLIGHT_INTERVAL_SECONDS:-10}"
deadline=$(( $(date +%s) + timeout_seconds ))

while (( $(date +%s) < deadline )); do
  if "$python_bin" - "$worker_status" "$operations_status" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def load(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"status is not an object: {path}")
    return payload


def age_seconds(value: object) -> float:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


reasons: list[str] = []
try:
    worker = load(sys.argv[1])
    operations = load(sys.argv[2])
except (OSError, ValueError, TypeError) as exc:
    print(json.dumps({"ready": False, "reasons": [f"status_unavailable:{exc}"]}))
    raise SystemExit(1)

if age_seconds(worker.get("updated_at")) > 15:
    reasons.append("worker_status_stale")
if worker.get("transport_state") != "REDUNDANT":
    reasons.append("worker_not_redundant")
watched = int(worker.get("watched_assets") or 0)
ready = int(worker.get("ready_books") or 0)
if watched <= 0 or ready / watched < 0.98:
    reasons.append("ready_book_ratio_below_0_98")
if int(worker.get("queued_intents") or 0) != 0:
    reasons.append("queued_intents_present")
if int(worker.get("processing_intents") or 0) != 0:
    reasons.append("processing_intents_present")
if worker.get("last_error"):
    reasons.append("worker_error_present")
if age_seconds(operations.get("generated_at")) > 90:
    reasons.append("operations_status_stale")
if str(operations.get("operational_level") or "RED") not in {"GREEN", "YELLOW"}:
    reasons.append("operations_not_ready")
if str(operations.get("admission_mode") or "FAIL_CLOSED") not in {
    "ACCEPT",
    "CALIBRATED_TAKER_SMALL_ONLY",
}:
    reasons.append("operations_admission_not_ready")

print(
    json.dumps(
        {
            "ready": not reasons,
            "reasons": reasons,
            "worker_transport": worker.get("transport_state"),
            "ready_books": ready,
            "watched_assets": watched,
            "operations_level": operations.get("operational_level"),
            "operations_mode": operations.get("admission_mode"),
        },
        sort_keys=True,
    )
)
raise SystemExit(0 if not reasons else 1)
PY
  then
    exec /bin/bash scripts/run_paper_live_shadow_acceptance_soak.sh
  fi
  sleep "$interval_seconds"
done

echo "paper final soak preflight did not become ready within ${timeout_seconds}s" >&2
exit 42
