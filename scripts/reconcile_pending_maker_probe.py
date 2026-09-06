#!/usr/bin/env python3
"""Reconcile one accepted Maker probe without resubmitting the order."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.calibration.probe_plan import load_probe_plan
from quant.calibration.real_live_adapter import PolymarketV2LiveAdapter
from quant.calibration.user_ws_recorder import UserWsRecorder
from quant.maker.pending_probe_reconciler import PendingMakerProbeReconciler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--watch-seconds", type=float, default=0)
    parser.add_argument("--max-reconnects", type=int, default=5)
    parser.add_argument(
        "--cancel-open",
        action="store_true",
        help="Cancel only the exact checkpoint order ID; never submits an order",
    )
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.env_file:
            from dotenv import load_dotenv

            for path in args.env_file:
                load_dotenv(path, override=True)
        checkpoint = args.checkpoint.resolve()
        stem = checkpoint.name.removesuffix(".submission.json")
        journal = args.journal or checkpoint.with_name(f"{stem}.user-ws.jsonl")
        output = args.output or checkpoint.with_name(f"{stem}.recovery.json")
        plan = load_probe_plan(args.config)
        payload = PendingMakerProbeReconciler(
            adapter=PolymarketV2LiveAdapter(plan),
            user_ws=UserWsRecorder(plan),
        ).reconcile(
            checkpoint_path=checkpoint,
            journal_path=journal,
            output_path=output,
            watch_seconds=args.watch_seconds,
            cancel_open=args.cancel_open,
            max_reconnects=args.max_reconnects,
        )
    except Exception as exc:  # noqa: BLE001
        payload = {
            "schema_version": "maker_pending_probe_recovery_v1",
            "status": "BLOCKED",
            "exchange_submit_called": False,
            "resubmit_forbidden": True,
            "errors": [f"{exc.__class__.__name__}:{str(exc)[:500]}"],
        }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload.get("status") == "CALIBRATABLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
