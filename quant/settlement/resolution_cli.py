"""Paper-only resolution lifecycle command boundary."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.paper.paper_ledger import PostgresPaperLedgerSink

from .oracle_state import OracleResolutionState, ResolutionPhase
from .payout_vector import PayoutVector
from .resolution_store import PostgresResolutionLifecycleStore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("initialize")
    initialize.add_argument("condition_id")
    initialize.add_argument("--event-id", required=True)
    initialize.add_argument("--trading-stopped-at", required=True)
    initialize.add_argument("--expected-resolution-at")
    initialize.add_argument("--source", required=True)
    initialize.add_argument("--reason", default="trading_stopped")

    transition = sub.add_parser("transition")
    transition.add_argument("condition_id")
    transition.add_argument(
        "phase", choices=tuple(item.value for item in ResolutionPhase)
    )
    transition.add_argument("--event-id", required=True)
    transition.add_argument("--event-ts", required=True)
    transition.add_argument("--source", required=True)
    transition.add_argument("--reason", default="")
    transition.add_argument("--market-id")
    transition.add_argument(
        "--payout-json",
        type=Path,
        help=(
            "JSON object with payouts, resolution_source and oracle_finalized_at; "
            "required only for a final-resolution transition"
        ),
    )
    status = sub.add_parser("status")
    status.add_argument("condition_id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    PostgresPaperLedgerSink()
    store = PostgresResolutionLifecycleStore()
    store.ensure_schema()
    if args.command == "initialize":
        state = store.initialize(
            OracleResolutionState(
                condition_id=args.condition_id,
                phase=ResolutionPhase.TRADING_STOPPED,
                trading_stopped_at=_datetime(args.trading_stopped_at),
                expected_resolution_at=(
                    _datetime(args.expected_resolution_at)
                    if args.expected_resolution_at
                    else None
                ),
            ),
            event_id=args.event_id,
            source=args.source,
            reason=args.reason,
        )
        _print_state(state, store)
        return 0
    if args.command == "transition":
        payout = _payout(args.condition_id, args.payout_json)
        state = store.transition(
            args.condition_id,
            ResolutionPhase(args.phase),
            event_id=args.event_id,
            event_ts=_datetime(args.event_ts),
            source=args.source,
            reason=args.reason,
            payout_vector=payout,
            market_id=args.market_id,
        )
        _print_state(state, store)
        return 0
    state = store.state(args.condition_id)
    print(
        json.dumps(
            {
                "state": _state_payload(state),
                "events": list(store.events(args.condition_id)),
                "receivables": list(store.receivables(args.condition_id)),
                "paper_only": True,
                "live_submission_performed": False,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


def _payout(condition_id: str, path: Path | None) -> PayoutVector | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return PayoutVector(
        condition_id=condition_id,
        payouts={
            str(asset_id): Decimal(str(value))
            for asset_id, value in dict(payload["payouts"]).items()
        },
        resolution_source=str(payload["resolution_source"]),
        oracle_finalized_at=str(payload["oracle_finalized_at"]),
    )


def _print_state(
    state: OracleResolutionState, store: PostgresResolutionLifecycleStore
) -> None:
    print(
        json.dumps(
            {
                "state": _state_payload(state),
                "receivables": list(store.receivables(state.condition_id)),
                "paper_only": True,
                "live_submission_performed": False,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def _state_payload(state: OracleResolutionState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "condition_id": state.condition_id,
        "phase": state.phase.value,
        "trading_stopped_at": state.trading_stopped_at,
        "expected_resolution_at": state.expected_resolution_at,
        "actual_finalized_at": state.actual_finalized_at,
        "redeem_started_at": state.redeem_started_at,
        "redeemed_at": state.redeemed_at,
        "proposal_count": state.proposal_count,
        "dispute_round": state.dispute_round,
    }


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
