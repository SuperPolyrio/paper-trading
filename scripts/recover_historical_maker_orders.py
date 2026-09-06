#!/usr/bin/env python3
"""Recover authenticated historical Maker outcomes without any write request."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.calibration.probe_plan import load_probe_plan
from quant.calibration.real_live_adapter import PolymarketV2LiveAdapter
from quant.maker.historical_order_recovery import HistoricalMakerOrderRecovery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--before", help="UTC ISO timestamp; defaults to now")
    parser.add_argument("--max-orders", type=int, default=500)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runtime_outputs/maker_calibration/historical-recovery"),
    )
    parser.add_argument(
        "--local-evidence-root",
        type=Path,
        action="append",
        default=[],
    )
    args = parser.parse_args(argv)
    try:
        plan = load_probe_plan(args.config)
        before = _timestamp(args.before) if args.before else datetime.now(timezone.utc)
        roots = args.local_evidence_root or [
            Path("runtime_outputs/maker_calibration/probes"),
            Path("livevspaper"),
        ]
        payload = HistoricalMakerOrderRecovery(
            adapter=PolymarketV2LiveAdapter(plan),
            maker_address=plan.account.expected_funder_address,
            output_dir=args.output_dir,
            local_evidence_roots=roots,
        ).recover(
            after=before - timedelta(days=max(1, args.lookback_days)),
            before=before,
            max_orders=max(1, args.max_orders),
        )
    except Exception as exc:  # noqa: BLE001
        payload = {
            "schema_version": "historical_maker_order_recovery_v1",
            "status": "BLOCKED",
            "exchange_submit_called": False,
            "exchange_cancel_called": False,
            "errors": [f"{exc.__class__.__name__}:{str(exc)[:500]}"],
        }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload.get("status") == "PASS_READ_ONLY" else 2


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
