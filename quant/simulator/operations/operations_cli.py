"""Paper-only position-operation and relayer reconciliation command boundary."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.paper.paper_ledger import PostgresPaperLedgerSink
from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    ExposureEffect,
    PostgresAdmissionStore,
    UnifiedAdmissionService,
)

from .domain import (
    PositionOperationIntent,
    PositionOperationState,
    PositionOperationType,
)
from .operation_store import PostgresPositionOperationStore, RelayerOperationUpdate


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--input", type=Path, required=True)
    create.add_argument("--event-id", required=True)

    allowance = sub.add_parser("allowance")
    allowance.add_argument("operation_id")
    allowance.add_argument("--event-id", required=True)
    allowance.add_argument("--event-ts", required=True)
    allowance.add_argument(
        "--approved", action=argparse.BooleanOptionalAction, required=True
    )
    allowance.add_argument("--reason", default="")

    nonce = sub.add_parser("reserve-nonce")
    nonce.add_argument("operation_id")
    nonce.add_argument("--event-id", required=True)
    nonce.add_argument("--event-ts", required=True)
    nonce.add_argument("--nonce", type=int, required=True)

    update = sub.add_parser("relayer-update")
    update.add_argument("operation_id")
    update.add_argument(
        "--state",
        choices=("SUBMITTED", "MINED", "CONFIRMED", "FAILED"),
        required=True,
    )
    update.add_argument("--event-id", required=True)
    update.add_argument("--event-ts", required=True)
    update.add_argument("--transaction-hash")
    update.add_argument("--reason", default="")

    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("operation_id")
    reconcile.add_argument("--event-id", required=True)
    reconcile.add_argument("--event-ts", required=True)
    reconcile.add_argument("--reason", default="relayer_reconciled")

    status = sub.add_parser("status")
    status.add_argument("operation_id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    PostgresPaperLedgerSink()
    store = PostgresPositionOperationStore()
    store.ensure_schema()
    admission_store = PostgresAdmissionStore()
    admission_store.ensure_schema()
    admission_service = UnifiedAdmissionService(store=admission_store)
    admission = None
    if args.command == "create":
        intent = _intent(args.input)
        admission = admission_service.decide(
            AdmissionRequest(
                request_id=f"position-operation:{intent.event_id}",
                operation=AdmissionOperation(intent.operation_type.value),
                account_id=intent.account_id,
                strategy_id=intent.strategy_id,
                condition_id=intent.condition_id,
                market_id=intent.market_id,
                exposure_effect=ExposureEffect.NEUTRAL,
                observed_at=intent.decision_ts,
                metadata={"source": "position_operations_cli"},
            )
        )
        if not admission.allowed:
            raise RuntimeError(
                "position operation rejected by unified admission: "
                + ",".join(admission.reason_codes)
            )
        record = store.create(intent, event_id=args.event_id)
    elif args.command == "allowance":
        record = store.allowance_checked(
            args.operation_id,
            event_id=args.event_id,
            event_ts=_datetime(args.event_ts),
            approved=bool(args.approved),
            reason=args.reason,
        )
    elif args.command == "reserve-nonce":
        record = store.reserve_nonce(
            args.operation_id,
            event_id=args.event_id,
            event_ts=_datetime(args.event_ts),
            nonce=args.nonce,
        )
    elif args.command == "relayer-update":
        current = store.operation(args.operation_id)
        admission = _reconciliation_admission(
            admission_service,
            current,
            request_id=f"position-operation-relayer:{args.event_id}",
            observed_at=_datetime(args.event_ts),
            action=f"relayer_{args.state.lower()}",
        )
        record = store.reconcile_relayer(
            RelayerOperationUpdate(
                operation_id=args.operation_id,
                event_id=args.event_id,
                state=PositionOperationState(args.state),
                observed_at=_datetime(args.event_ts),
                transaction_hash=args.transaction_hash,
                reason=args.reason,
            )
        )
    elif args.command == "reconcile":
        current = store.operation(args.operation_id)
        admission = _reconciliation_admission(
            admission_service,
            current,
            request_id=f"position-operation-reconcile:{args.event_id}",
            observed_at=_datetime(args.event_ts),
            action="reconcile",
        )
        record = store.reconcile(
            args.operation_id,
            event_id=args.event_id,
            event_ts=_datetime(args.event_ts),
            reason=args.reason,
        )
    else:
        record = store.operation(args.operation_id)
    print(
        json.dumps(
            {
                "operation": _record_payload(record),
                "events": (
                    list(store.events(record.intent.event_id)) if record else []
                ),
                "paper_only": True,
                "live_submission_performed": False,
                "admission": admission.as_dict() if admission is not None else None,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


def _reconciliation_admission(
    service: UnifiedAdmissionService,
    record: Any | None,
    *,
    request_id: str,
    observed_at: datetime,
    action: str,
):
    if record is None:
        raise LookupError("position operation not found")
    intent = record.intent
    decision = service.decide(
        AdmissionRequest(
            request_id=request_id,
            operation=AdmissionOperation.RECONCILIATION,
            account_id=intent.account_id,
            strategy_id=intent.strategy_id,
            condition_id=intent.condition_id,
            market_id=intent.market_id,
            exposure_effect=ExposureEffect.NEUTRAL,
            observed_at=observed_at,
            metadata={
                "source": "position_operations_cli",
                "action": action,
                "operation_id": intent.event_id,
            },
        )
    )
    if not decision.allowed:
        raise RuntimeError(
            "position reconciliation rejected by unified admission: "
            + ",".join(decision.reason_codes)
        )
    return decision


def _intent(path: Path) -> PositionOperationIntent:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return PositionOperationIntent(
        event_id=str(payload["operation_id"]),
        operation_type=PositionOperationType(str(payload["operation_type"])),
        account_id=str(payload["account_id"]),
        strategy_id=str(payload["strategy_id"]),
        market_id=str(payload["market_id"]),
        condition_id=str(payload["condition_id"]),
        amount=Decimal(str(payload["amount"])),
        decision_ts=_datetime(str(payload["decision_ts"])),
        collateral_delta=Decimal(str(payload["collateral_delta"])),
        token_deltas={
            str(asset): Decimal(str(value))
            for asset, value in dict(payload["token_deltas"]).items()
        },
        token_decimals=int(payload.get("token_decimals", 6)),
    )


def _record_payload(record: Any | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "operation_id": record.intent.event_id,
        "operation_type": record.intent.operation_type.value,
        "account_id": record.intent.account_id,
        "strategy_id": record.intent.strategy_id,
        "market_id": record.intent.market_id,
        "condition_id": record.intent.condition_id,
        "state": record.state.value,
        "nonce": record.nonce,
        "transaction_hash": record.transaction_hash,
        "reason": record.reason,
    }


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
