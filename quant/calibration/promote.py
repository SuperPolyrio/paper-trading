"""Promote only a model with a passing grouped holdout evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evaluate_holdout import evaluate_run
from .execution_model_registry import ExecutionModelRegistry
from .store import CalibrationStore
from .taker_campaign import verify_campaign_manifest


def promote_campaign(
    store: CalibrationStore,
    manifest_path: Path,
    *,
    require_gates: bool,
) -> dict[str, object]:
    if not require_gates:
        raise RuntimeError("--require-gates is mandatory")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("campaign manifest must be a JSON object")
    report, _ = verify_campaign_manifest(store, manifest)
    if report["status"] != "PASS":
        raise RuntimeError("grouped campaign holdout promotion gates are not satisfied")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    anchor_run_id = str(source.get("anchor_run_id") or "")
    if not anchor_run_id:
        raise RuntimeError("campaign manifest has no anchor run")
    result = ExecutionModelRegistry(store).promote(
        run_id=anchor_run_id,
        model_version=str(report["model_version"]),
        report=report,
    )
    return {"status": "PASS", "model": result, "evaluation_id": report["evaluation_id"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id")
    source.add_argument("--campaign-manifest", type=Path)
    parser.add_argument("--require-gates", action="store_true")
    args = parser.parse_args(argv)
    store = CalibrationStore()
    try:
        if not args.require_gates:
            raise RuntimeError("--require-gates is mandatory")
        if args.campaign_manifest:
            payload = promote_campaign(
                store,
                args.campaign_manifest,
                require_gates=args.require_gates,
            )
        else:
            report, _ = evaluate_run(str(args.run_id), store=store)
            if report["status"] != "PASS":
                raise RuntimeError("grouped holdout promotion gates are not satisfied")
            run = store.load_run(str(args.run_id))
            if not run:
                raise ValueError(f"unknown run {args.run_id}")
            result = ExecutionModelRegistry(store).promote(
                run_id=str(args.run_id),
                model_version=str(run["model_version"]),
                report=report,
            )
            payload = {"status": "PASS", "model": result}
    except Exception as exc:
        payload = {"status": "BLOCKED", "reason": f"{exc.__class__.__name__}:{exc}"}
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
