#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

latest="runtime_outputs/paper_live_shadow/gcp_soak_24h_accuracy_final_v3/latest.json"
python_bin="${PREDICTION_MARKET_QUANT_PYTHON_BIN:-/opt/polyData/.venv/bin/python}"
if [[ ! -f "$latest" ]]; then
  exit 0
fi

if ! "$python_bin" - "$latest" <<'PY'
import json
from pathlib import Path
import sys

from quant.paper.acceptance import soak_promotion_eligible

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if soak_promotion_eligible(payload) else 1)
PY
then
  exit 0
fi

systemctl --user --no-block start poly-quant-gcp-paper-soak-7d.service
