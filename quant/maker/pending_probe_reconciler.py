"""Recover an accepted Maker probe without ever submitting it again."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.calibration.order_rest_reconciler import reconcile_order_lifecycle
from quant.calibration.user_ws_recorder import (
    DurableUserWsEventJournal,
    UserWsRecorder,
)
from quant.maker.own_order_truth import (
    OwnOrderFilledTruthReconciler,
    trade_transaction_hashes,
)
from quant.maker.live_probe_runner import (
    maker_account_delta_matches_truth,
    maker_finality_label,
    maker_outcome_observation,
    maker_probe_artifact_complete,
)


class PendingMakerProbeReconciler:
    """Merge durable User WS, REST and OrderFilled truth for one order ID."""

    def __init__(
        self,
        *,
        adapter: Any,
        user_ws: UserWsRecorder,
        onchain_truth_reconciler: OwnOrderFilledTruthReconciler | None = None,
    ) -> None:
        self.adapter = adapter
        self.user_ws = user_ws
        self.onchain_truth_reconciler = (
            onchain_truth_reconciler or OwnOrderFilledTruthReconciler()
        )

    def reconcile(
        self,
        *,
        checkpoint_path: Path | str,
        journal_path: Path | str,
        output_path: Path | str,
        watch_seconds: float = 0,
        cancel_open: bool = False,
        max_reconnects: int = 5,
    ) -> dict[str, Any]:
        checkpoint = _read_json(Path(checkpoint_path))
        run_id = _required(checkpoint, "run_id")
        order_id = _required(checkpoint, "order_id")
        asset_id = _required(checkpoint, "asset_id")
        condition_id = _required(checkpoint, "condition_id")
        submitted_at = _timestamp(_required(checkpoint, "submitted_at"))
        journal = DurableUserWsEventJournal(journal_path)
        capture: dict[str, Any] = {
            "status": "NOT_REQUESTED",
            "events": [],
            "credentials_persisted": False,
        }
        errors: list[str] = []
        if watch_seconds > 0:
            try:
                capture = self.user_ws.record_until_terminal(
                    probe_id=run_id,
                    condition_ids=[condition_id],
                    order_id=order_id,
                    timeout_seconds=watch_seconds,
                    event_sink=journal.append,
                    max_reconnects=max_reconnects,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"user_ws_recovery:{exc.__class__.__name__}:{str(exc)[:500]}"
                )
                capture = {
                    "status": "SOURCE_UNAVAILABLE",
                    "events": [],
                    "credentials_persisted": False,
                }

        rest = self._rest_snapshot(
            order_id=order_id,
            condition_id=condition_id,
            asset_id=asset_id,
            submitted_at=submitted_at,
        )
        open_order = _is_open_order(rest, order_id)
        cancellation: Mapping[str, Any] | None = None
        exact_cancel_called = False
        if open_order and cancel_open:
            exact_cancel_called = True
            cancellation_value = self.adapter.cancel_order(order_id)
            cancellation = (
                dict(cancellation_value)
                if isinstance(cancellation_value, Mapping)
                else {"value": cancellation_value}
            )
            rest = self._rest_snapshot(
                order_id=order_id,
                condition_id=condition_id,
                asset_id=asset_id,
                submitted_at=submitted_at,
            )
            open_order = _is_open_order(rest, order_id)

        journal_events = journal.load_events()
        journal_payloads = [
            dict(row.get("payload") or {})
            for row in journal_events
            if isinstance(row, Mapping)
        ]
        truth = reconcile_order_lifecycle(
            order_id=order_id,
            user_ws_events=journal_payloads,
            rest_order=rest.get("order"),
            rest_trades=rest.get("trades") or (),
        )
        matched_size = Decimal(str(truth.get("actual_matched_size") or 0))
        try:
            orderfilled = self.onchain_truth_reconciler.reconcile(
                order_id=order_id,
                asset_id=asset_id,
                expected_matched_size=matched_size,
                transaction_hashes=trade_transaction_hashes(
                    [*(rest.get("trades") or ()), *journal_payloads],
                    order_id=order_id,
                ),
                window_start=submitted_at - timedelta(minutes=2),
                window_end=datetime.now(timezone.utc) + timedelta(minutes=2),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"orderfilled_recovery:{exc.__class__.__name__}:{str(exc)[:500]}"
            )
            orderfilled = {
                "schema_version": "maker_own_orderfilled_truth_v1",
                "status": "SOURCE_UNAVAILABLE",
                "order_id": order_id,
                "asset_id": asset_id,
                "expected_matched_size": format(matched_size, "f"),
                "used_for_actual_live_outcome": True,
            }

        account_after = self.adapter.get_account_snapshot(asset_id=asset_id).as_dict()
        account_before = checkpoint.get("account_before")
        delta = (
            _account_delta(account_before, account_after)
            if isinstance(account_before, Mapping)
            else None
        )
        checkpoint_intent = (
            checkpoint.get("intent")
            if isinstance(checkpoint.get("intent"), Mapping)
            else {}
        )
        account_delta_reconciled = bool(
            isinstance(delta, Mapping)
            and maker_account_delta_matches_truth(
                side=str(checkpoint_intent.get("side") or checkpoint.get("side") or ""),
                matched_size=matched_size,
                quote_amount=Decimal(str(truth.get("actual_quote_amount") or 0)),
                fee=Decimal(str(truth.get("actual_fee") or 0)),
                delta=delta,
            )
        )
        user_ws_evidence = bool(journal_events)
        order_terminal = not open_order and bool(
            rest.get("order")
            or rest.get("order_lookup_error") == "HTTP_404_ORDER_NOT_FOUND"
        )
        chain_complete = bool(
            matched_size <= 0 or orderfilled.get("status") == "CONFIRMED_MATCH"
        )
        role_complete = bool(
            matched_size <= 0 or truth.get("liquidity_role_truth") == "MAKER"
        )
        artifact_complete = maker_probe_artifact_complete(
            order_terminal_evidence=bool(order_terminal and user_ws_evidence),
            order_still_open=open_order,
            rest_order_reconciled=bool(truth.get("rest_order_reconciled")),
            order_not_found=(
                rest.get("order_lookup_error") == "HTTP_404_ORDER_NOT_FOUND"
            ),
            matched_size=matched_size,
            ledger_truth=str(truth.get("ledger_truth") or ""),
            orderfilled_status=str(orderfilled.get("status") or ""),
            account_delta_reconciled=account_delta_reconciled,
        )
        if open_order:
            status = "OPEN_ORDER_REQUIRES_EXACT_CANCEL"
        elif artifact_complete:
            status = "CALIBRATABLE"
        elif order_terminal and user_ws_evidence and chain_complete and role_complete:
            status = "ACCOUNT_DELTA_UNPROVEN"
        elif matched_size > 0 and not chain_complete:
            status = "PENDING_CHAIN_INDEX"
        else:
            status = "PENDING_RECONCILIATION"
        outcome = _actual_outcome(matched_size, checkpoint)
        finality = maker_finality_label(
            matched_size=matched_size,
            orderfilled=orderfilled,
        )
        requested_size = _checkpoint_size(checkpoint)
        observation = maker_outcome_observation(
            outcome=outcome,
            matched_size=matched_size,
            requested_size=requested_size,
            resting_seconds=Decimal(str(checkpoint.get("resting_seconds") or 0)),
            capture=capture,
        )
        submission = (
            dict(checkpoint.get("submission") or {})
            if isinstance(checkpoint.get("submission"), Mapping)
            else {}
        )
        signed_audit = (
            dict(checkpoint.get("signed_order_audit") or {})
            if isinstance(checkpoint.get("signed_order_audit"), Mapping)
            else {}
        )
        original_submit_called = bool(
            signed_audit.get("exchange_submit_called")
            and str(submission.get("orderID") or submission.get("order_id") or "")
            .strip()
            .lower()
            == order_id.lower()
        )
        recovered_capture = {
            **capture,
            "status": (
                capture.get("status")
                if capture.get("status") not in {None, "NOT_REQUESTED"}
                else "RECOVERED_FROM_DURABLE_JOURNAL"
            ),
            "events": journal_events,
            "order_id": order_id,
            "submitted_at": submitted_at.isoformat(),
            "journal_path": str(journal.path),
            "journal_sha256": journal.sha256(),
            "journal_event_count": len(journal_events),
            "credentials_persisted": False,
        }
        payload = {
            "schema_version": "maker_pending_probe_recovery_v1",
            "status": status,
            "mode": "LIVE" if original_submit_called else checkpoint.get("mode"),
            "run_id": run_id,
            "order_id": order_id,
            "asset_id": asset_id,
            "condition_id": condition_id,
            "submitted_at": submitted_at.isoformat(),
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
            "exchange_submit_called": original_submit_called,
            "recovery_exchange_submit_called": False,
            "resubmit_forbidden": True,
            "exact_cancel_called": exact_cancel_called,
            "cancellation": cancellation,
            "order_still_open": open_order,
            "artifact_complete": artifact_complete,
            "user_ws_capture": recovered_capture,
            "user_ws_journal": {
                "path": str(journal.path),
                "sha256": journal.sha256(),
                "event_count": len(journal_events),
            },
            "rest_reconciliation": rest,
            "truth": truth,
            "onchain_orderfilled": orderfilled,
            "account_before": account_before,
            "account_after": account_after,
            "account_delta": delta,
            "account_delta_reconciled": account_delta_reconciled,
            "actual_outcome": outcome,
            "outcome_observation": observation,
            "finality_label": finality,
            "submission": submission,
            "signed_order_audit": signed_audit,
            "started_at": str(checkpoint.get("started_at") or submitted_at.isoformat()),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "cancel_acknowledged": bool(
                cancellation
                or checkpoint.get("cancel_acknowledged")
                or (outcome == "FULL" and order_terminal)
            ),
            "placement": checkpoint.get("placement"),
            "resting_seconds": checkpoint.get("resting_seconds"),
            "post_cancel_seconds": checkpoint.get("post_cancel_seconds"),
            "cancel_on_first_fill": bool(checkpoint.get("cancel_on_first_fill")),
            "probe_target": checkpoint.get("probe_target"),
            "probe_sizing": checkpoint.get("probe_sizing"),
            "candidate": checkpoint.get("candidate"),
            "market_snapshot": checkpoint.get("market_snapshot"),
            "intent": checkpoint.get("intent"),
            "maker_trade_forecast": checkpoint.get("maker_trade_forecast"),
            "model_predictions": checkpoint.get("model_predictions"),
            "maker_probability_calibration": checkpoint.get(
                "maker_probability_calibration"
            ),
            "prediction_snapshot": checkpoint.get("prediction_snapshot"),
            "prediction_snapshot_hash": checkpoint.get("prediction_snapshot_hash"),
            "errors": errors,
        }
        _write_json(Path(output_path), payload)
        return payload

    def _rest_snapshot(
        self,
        *,
        order_id: str,
        condition_id: str,
        asset_id: str,
        submitted_at: datetime,
    ) -> dict[str, Any]:
        return self.adapter.get_order_reconciliation_snapshot(
            order_id=order_id,
            condition_id=condition_id,
            asset_id=asset_id,
            after=int(submitted_at.timestamp()) - 60,
            before=int(datetime.now(timezone.utc).timestamp()) + 60,
        )


def _actual_outcome(matched_size: Decimal, checkpoint: Mapping[str, Any]) -> str:
    order_size = _checkpoint_size(checkpoint)
    if matched_size <= 0:
        return "NO_FILL"
    if order_size > 0 and matched_size + Decimal("0.000001") >= order_size:
        return "FULL"
    return "PARTIAL"


def _checkpoint_size(checkpoint: Mapping[str, Any]) -> Decimal:
    audit = checkpoint.get("signed_order_audit")
    fallback_size = audit.get("amount") if isinstance(audit, Mapping) else 0
    intent = checkpoint.get("intent")
    intent_size = intent.get("size") if isinstance(intent, Mapping) else 0
    return Decimal(str(checkpoint.get("size") or intent_size or fallback_size or 0))


def _is_open_order(rest: Mapping[str, Any], order_id: str) -> bool:
    expected = str(order_id).lower()
    return any(
        str(row.get("id") or row.get("orderID") or row.get("order_id") or "").lower()
        == expected
        for row in rest.get("open_orders") or ()
        if isinstance(row, Mapping)
    )


def _account_delta(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, str]:
    return {
        name: format(
            Decimal(str((after.get(name) or {}).get("balance") or 0))
            - Decimal(str((before.get(name) or {}).get("balance") or 0)),
            "f",
        )
        for name in ("collateral", "conditional")
    }


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"Maker submission checkpoint missing {key}")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Maker submission checkpoint is not an object: {path}")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
