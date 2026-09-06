"""Run a frozen taker calibration plan; live submission remains explicit."""

from __future__ import annotations

import argparse
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

from .preflight import DEFAULT_CONFIG
from .live_probe_runner import LiveProbeRunner
from .probe_plan import ProbeExecution, load_probe_plan
from .probe_scheduler import NoSubmitProbeRunner
from quant.paper.paired_probe import load_live_probe_candidates


DEFAULT_OUTPUT = Path("runtime_outputs/taker_calibration/latest.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--no-submit", action="store_true")
    mode.add_argument("--prepare-live", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--market-id")
    parser.add_argument("--asset-id")
    parser.add_argument(
        "--clean-cohort-id",
        help="Bind live execution to one immutable post-CLOB-V2 clean cohort.",
    )
    parser.add_argument("--side", choices=("BUY", "SELL"))
    parser.add_argument("--order-type", choices=("FOK", "FAK"))
    parser.add_argument("--amount", type=Decimal)
    parser.add_argument("--amount-unit", choices=("QUOTE", "SHARES"))
    parser.add_argument(
        "--frozen-manifest",
        type=Path,
        help="Reuse a previously accepted manifest (or a report containing one) for no-submit.",
    )
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        plan = load_probe_plan(args.config)
        if args.market_id:
            plan = replace(
                plan,
                market_policy=replace(plan.market_policy, allow_market_ids=(str(args.market_id),)),
            )
        if any(
            value is not None
            for value in (args.side, args.order_type, args.amount, args.amount_unit)
        ):
            plan = replace(
                plan,
                execution=_override_execution(
                    plan.execution,
                    side=args.side,
                    order_type=args.order_type,
                    amount=args.amount,
                    amount_unit=args.amount_unit,
                ),
            )
        if args.live:
            if not args.run_id:
                raise ValueError("--live requires the previously prepared --run-id")
            payload = LiveProbeRunner(
                plan,
                approved_asset_id=args.asset_id,
                clean_cohort_id=args.clean_cohort_id,
            ).execute(str(args.run_id))
        elif args.prepare_live:
            payload = LiveProbeRunner(
                plan,
                approved_asset_id=args.asset_id,
                clean_cohort_id=args.clean_cohort_id,
            ).prepare()
        else:
            candidate_loader = None
            if args.asset_id:
                approved_asset_id = str(args.asset_id)

                def candidate_loader(store, **kwargs):
                    kwargs["asset_id"] = approved_asset_id
                    return load_live_probe_candidates(store, **kwargs)

            runner_options = {}
            if candidate_loader:
                runner_options["candidate_loader"] = candidate_loader
                runner_options["target_asset_id"] = str(args.asset_id)
            if args.frozen_manifest:
                frozen_payload = json.loads(args.frozen_manifest.read_text(encoding="utf-8"))
                runner_options["frozen_manifest"] = (
                    frozen_payload.get("manifest")
                    if isinstance(frozen_payload.get("manifest"), dict)
                    else frozen_payload
                )
            payload = NoSubmitProbeRunner(plan, **runner_options).run()
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "reason": f"{exc.__class__.__name__}:{str(exc)}",
            "exchange_order_submitted": False,
        }
    _write(args.json_out, payload)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload.get("status") in {
        "PASS",
        "SMOKE_PASS",
        "AWAITING_APPROVAL",
        "READY_TO_EXECUTE",
        "PROBE_COMPLETE",
        "PENDING_RECONCILIATION",
    } else 2


def _override_execution(
    execution: ProbeExecution,
    *,
    side: str | None = None,
    order_type: str | None = None,
    amount: Decimal | None = None,
    amount_unit: str | None = None,
) -> ProbeExecution:
    selected_side = str(side or execution.sides[0]).upper()
    selected_unit = str(
        amount_unit or ("QUOTE" if selected_side == "BUY" else "SHARES")
    ).upper()
    return replace(
        execution,
        sides=(selected_side,),
        order_types=(str(order_type).upper(),) if order_type else execution.order_types,
        amount=amount if amount is not None else execution.amount,
        amount_unit=selected_unit,
    )


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
