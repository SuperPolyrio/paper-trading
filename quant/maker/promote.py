"""Promote a maker model only from a passing immutable holdout report."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from quant.calibration.store import DEFAULT_VENUE_REGIME_ID, CalibrationStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/calibration/maker_core_v1.yaml")
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--require-gates", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.require_gates:
            raise RuntimeError("--require-gates is mandatory")
        evaluation = json.loads(args.evaluation.read_text(encoding="utf-8"))
        config_raw = args.config.read_bytes()
        config = yaml.safe_load(config_raw)
        if evaluation.get("status") != "PASS" or not evaluation.get(
            "promotion_allowed"
        ):
            raise RuntimeError("maker holdout gates are not satisfied")
        model_version = str(config["model_version"])
        if evaluation.get("model_version") != model_version:
            raise RuntimeError("evaluation and config model versions differ")
        store = CalibrationStore()
        store.ensure_schema()
        evidence_hash = hashlib.sha256(args.evaluation.read_bytes()).hexdigest()
        code_commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "UNKNOWN"
        )
        store.register_execution_model_version(
            model_version=model_version,
            model_family="MAKER_QUEUE",
            trained_run_id=args.run_id,
            venue_regime_id=DEFAULT_VENUE_REGIME_ID,
            validated_domain=config,
            training_manifest_hash=evidence_hash,
            holdout_manifest_hash=evidence_hash,
            metrics=evaluation,
            code_commit=code_commit,
            config_hash=hashlib.sha256(config_raw).hexdigest(),
        )
        row = store.promote_execution_model_version(
            model_version=model_version,
            model_family="MAKER_QUEUE",
            venue_regime_id=DEFAULT_VENUE_REGIME_ID,
            evaluation=evaluation,
        )
        payload = {"status": "PASS", "model": row}
    except Exception as exc:
        payload = {"status": "BLOCKED", "reason": f"{exc.__class__.__name__}:{exc}"}
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
