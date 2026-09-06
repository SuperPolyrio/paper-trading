#!/usr/bin/env python3
"""Run one controlled post-only maker probe or its no-submit preflight."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.calibration.probe_plan import load_probe_plan
from quant.maker.live_probe_runner import MakerLiveProbeRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--no-submit", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--market-id", required=True)
    parser.add_argument("--side", choices=("BUY", "SELL"), default="BUY")
    parser.add_argument("--order-type", choices=("GTC", "GTD"), default="GTC")
    parser.add_argument("--size", required=True)
    parser.add_argument(
        "--placement",
        choices=(
            "AT_BEST",
            "ONE_TICK_BEHIND",
            "ONE_TICK_INSIDE_SPREAD",
            "NEAR_OPPOSITE",
            "ADAPTIVE_FRONT",
        ),
        default="ADAPTIVE_FRONT",
    )
    parser.add_argument(
        "--required-predicted-outcome",
        choices=("ANY", "NO_FILL", "PARTIAL", "FULL", "PARTIAL_OR_FULL"),
        default="ANY",
        help="Recheck the strict queue prediction immediately before submit",
    )
    parser.add_argument("--resting-seconds", type=float, default=10.0)
    parser.add_argument("--post-cancel-seconds", type=float, default=30.0)
    parser.add_argument(
        "--cancel-on-first-fill",
        action="store_true",
        help="Cancel the exact order after the first observed positive partial fill",
    )
    parser.add_argument(
        "--probe-target",
        choices=("GENERAL", "FULL", "PARTIAL"),
        default="GENERAL",
        help="Apply target-specific size gates; PARTIAL also cancels on first fill",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runtime_outputs/maker_calibration/probes"),
    )
    parser.add_argument(
        "--holdout-path",
        type=Path,
        default=Path("runtime_outputs/maker_calibration/holdout.jsonl"),
    )
    parser.add_argument(
        "--maker-probability-calibration-artifact",
        type=Path,
        default=Path("runtime_outputs/maker_calibration/probability-current.json"),
        help=(
            "validated research-only probability artifact; strict fill truth "
            "is unchanged"
        ),
    )
    parser.add_argument(
        "--targeted-hot-preflight",
        action="store_true",
        help=(
            "Use a current GCP BookState plus a one-token native WS baseline; "
            "this does not claim historical L2 continuity"
        ),
    )
    parser.add_argument(
        "--hot-preflight-proxy-url",
        default="http://127.0.0.1:18080",
        help="isolated read-only proxy for the one-token Market WS baseline",
    )
    parser.add_argument("--hot-preflight-timeout-seconds", type=float, default=10.0)
    parser.add_argument(
        "--hot-preflight-activity-seconds",
        type=float,
        default=0.0,
        help="Observe bounded native WS activity after the current book baseline",
    )
    parser.add_argument(
        "--public-trade-lookback-seconds",
        type=int,
        default=900,
        help="Recent official taker-trade window used only for probe selection",
    )
    parser.add_argument(
        "--allow-incomplete-trade-evidence-for-calibration",
        action="store_true",
        help=(
            "Allow a bounded FULL/PARTIAL label-collection probe when current "
            "WS+REST books agree but the predecision trade window is incomplete"
        ),
    )
    args = parser.parse_args(argv)
    try:
        base = load_probe_plan(args.config)
        plan = replace(
            base,
            market_policy=replace(
                base.market_policy,
                allow_market_ids=(str(args.market_id),),
            ),
            execution=replace(
                base.execution,
                count=1,
                sides=(str(args.side),),
                order_types=(str(args.order_type),),
                amount=base.execution.amount.__class__(str(args.size)),
                amount_unit="SHARES",
                expected_outcome="ANY",
                price_mode="BBO_ONLY",
            ),
        )
        payload = MakerLiveProbeRunner(
            plan,
            asset_id=args.asset_id,
            market_id=args.market_id,
            placement=args.placement,
            resting_seconds=args.resting_seconds,
            post_cancel_seconds=args.post_cancel_seconds,
            output_dir=args.output_dir,
            holdout_path=args.holdout_path,
            required_predicted_outcome=args.required_predicted_outcome,
            cancel_on_first_fill=args.cancel_on_first_fill,
            probe_target=args.probe_target,
            probability_calibration_path=(args.maker_probability_calibration_artifact),
            targeted_hot_preflight=args.targeted_hot_preflight,
            hot_preflight_proxy_url=args.hot_preflight_proxy_url,
            hot_preflight_timeout_seconds=args.hot_preflight_timeout_seconds,
            hot_preflight_activity_seconds=args.hot_preflight_activity_seconds,
            public_trade_lookback_seconds=args.public_trade_lookback_seconds,
            allow_incomplete_trade_evidence_for_calibration=(
                args.allow_incomplete_trade_evidence_for_calibration
            ),
        ).run_probe(live=bool(args.live))
    except Exception as exc:
        payload = {
            "schema_version": "maker_post_only_live_probe_v1",
            "status": "BLOCKED",
            "exchange_submit_called": False,
            "errors": [f"{exc.__class__.__name__}:{str(exc)[:500]}"],
        }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return (
        0
        if payload.get("status")
        in {
            "NO_SUBMIT_READY",
            "CALIBRATABLE",
            "PENDING_RECONCILIATION",
        }
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
