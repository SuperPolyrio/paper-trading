#!/usr/bin/env python3
"""Run the no-submit Maker User WS/REST/chain calibration collector."""

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
from quant.maker.calibration_collector import MakerCalibrationCollector
from quant.maker.pending_probe_reconciler import PendingMakerProbeReconciler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/calibration/maker_live_probe.yaml"),
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=Path("runtime_outputs/maker_calibration/probes"),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("runtime_outputs/maker_calibration/collector-status.json"),
    )
    parser.add_argument("--watch-seconds", type=float, default=20)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--max-reconnects", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        for path in args.env_file:
            load_dotenv(path, override=True)
    plan = load_probe_plan(args.config)
    adapter = PolymarketV2LiveAdapter(plan)
    user_ws = UserWsRecorder(plan)
    collector = MakerCalibrationCollector(
        checkpoint_dir=args.checkpoint_dir,
        user_ws=user_ws,
        reconciler=PendingMakerProbeReconciler(
            adapter=adapter,
            user_ws=user_ws,
        ),
        state_path=args.state_path,
        identity_scope=(
            "maker-calibration-account-v1:"
            f"{plan.account.expected_funder_address.lower()}"
        ),
    )
    if args.once:
        payload = collector.run_cycle(
            watch_seconds=args.watch_seconds,
            max_reconnects=args.max_reconnects,
        )
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0 if payload["status"] in {"PASS", "DEGRADED"} else 2
    collector.run_forever(
        watch_seconds=args.watch_seconds,
        poll_seconds=args.poll_seconds,
        max_reconnects=args.max_reconnects,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
