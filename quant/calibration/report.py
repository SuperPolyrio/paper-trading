"""Build a fresh no-submit or live taker calibration report from Postgres."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .calibration_report import build_calibration_report
from .store import CalibrationStore
from .taker_calibration_fit import fit_taker_calibration


def build_run_report(store: CalibrationStore, run_id: str) -> dict[str, Any]:
    run = store.load_run(run_id)
    if run is None:
        raise ValueError(f"unknown calibration run: {run_id}")
    if str(run.get("mode")) == "no-submit":
        payload = dict(run.get("report") or {})
        payload.setdefault("status", str(run.get("run_status") or "PENDING"))
        payload["run_id"] = run_id
        payload["promotion_allowed"] = False
        payload["pnl_grade"] = "SHADOW_UNCALIBRATED"
        return payload
    probes = store.load_probes(run_id)
    fit = fit_taker_calibration(probes)
    pnl = store.pnl_summary()
    payload = {
        **build_calibration_report(fit),
        "run_id": run_id,
        "model_version": str(run["model_version"]),
        "mode": "live",
        "portfolio_pnl": pnl,
    }
    store.finish_run(run_id, status=str(payload["status"]), report=payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = build_run_report(CalibrationStore(), args.run_id)
    except Exception as exc:
        payload = {"status": "FAIL", "run_id": args.run_id, "reason": f"{exc.__class__.__name__}:{exc}"}
    rendered = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered, encoding="utf-8")
    print(rendered, end="", flush=True)
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
