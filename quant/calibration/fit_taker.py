"""Fit the current run and persist a reproducible fit artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .store import CalibrationStore
from .taker_calibration_fit import fit_taker_calibration


def fit_run(run_id: str, *, store: CalibrationStore | None = None) -> dict[str, object]:
    source = store or CalibrationStore()
    rows = source.load_probes(run_id)
    fit = fit_taker_calibration(rows)
    return {"run_id": run_id, "status": "PASS" if rows else "BLOCKED", "fit": fit}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = fit_run(args.run_id)
    output = args.output or Path("reports/calibration") / args.run_id / "fit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
