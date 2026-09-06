#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

latest="runtime_outputs/paper_live_shadow/gcp_soak_6h_spool_v1/latest.json"
if [[ ! -f "$latest" ]]; then
  exit 0
fi

PYTHON_BIN="${PREDICTION_MARKET_QUANT_PYTHON_BIN:-$HOME/.conda/envs/prediction-market-quant/bin/python}"
if ! "$PYTHON_BIN" - "$latest" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
passed = payload.get("status") == "PASS" and payload.get("soak_complete") is True
raise SystemExit(0 if passed else 1)
PY
then
  exit 0
fi

systemctl --user --no-block start poly-quant-gcp-paper-soak-24h.service
