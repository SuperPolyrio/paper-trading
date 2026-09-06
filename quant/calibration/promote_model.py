"""Promote a calibrated taker model only after rechecking every holdout gate."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .execution_model_registry import ExecutionModelRegistry
from .report import build_run_report
from .store import CalibrationStore


def promote_run(store: CalibrationStore, run_id: str, *, require_gates: bool) -> dict[str, Any]:
    if not require_gates:
        raise RuntimeError("--require-gates is mandatory for model promotion")
    run = store.load_run(run_id)
    if run is None:
        raise ValueError(f"unknown calibration run: {run_id}")
    report = build_run_report(store, run_id)
    promoted = ExecutionModelRegistry(store).promote(
        run_id=run_id,
        model_version=str(run["model_version"]),
        report=report,
    )
    return {
        "status": "PASS",
        "run_id": run_id,
        "model_version": str(promoted["model_version"]),
        "model_state": str(promoted["model_state"]),
        "promoted_at": promoted.get("promoted_at"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--require-gates", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload: dict[str, Any] = promote_run(
            CalibrationStore(),
            args.run_id,
            require_gates=args.require_gates,
        )
    except Exception as exc:
        payload = {"status": "FAIL", "run_id": args.run_id, "reason": f"{exc.__class__.__name__}:{exc}"}
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
