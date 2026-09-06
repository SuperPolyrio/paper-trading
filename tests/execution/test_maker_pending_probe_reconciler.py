from __future__ import annotations

import json
from pathlib import Path

from quant.calibration.user_ws_recorder import DurableUserWsEventJournal
from quant.maker.pending_probe_reconciler import PendingMakerProbeReconciler


class _Account:
    def as_dict(self):
        return {
            "collateral": {"balance": "100"},
            "conditional": {"balance": "0"},
        }


class _Adapter:
    def __init__(self, *, open_order: bool = False):
        self.open_order = open_order
        self.cancel_calls = []
        self.rest_calls = []

    def get_order_reconciliation_snapshot(self, **kwargs):
        self.rest_calls.append(kwargs)
        return {
            "order": {"id": "order-1", "status": "CANCELED"},
            "trades": [],
            "open_orders": ([{"id": "order-1"}] if self.open_order else []),
            "order_lookup_error": None,
        }

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        self.open_order = False
        return {"canceled": [order_id]}

    def get_account_snapshot(self, *, asset_id):
        assert asset_id == "asset-1"
        return _Account()


class _UserWs:
    def record_until_terminal(self, **_kwargs):
        raise AssertionError("watch disabled")


class _OrderFilled:
    def __init__(self):
        self.calls = []

    def reconcile(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "status": "NOT_REQUIRED_NO_FILL",
            "matched_size": "0",
            "matched_rows": [],
        }


def _checkpoint(path: Path) -> Path:
    payload = {
        "run_id": "run-1",
        "order_id": "order-1",
        "asset_id": "asset-1",
        "condition_id": "condition-1",
        "side": "BUY",
        "size": "5",
        "submitted_at": "2026-08-26T00:00:00+00:00",
        "placement": "AT_BEST",
        "resting_seconds": "120",
        "post_cancel_seconds": "30",
        "cancel_on_first_fill": True,
        "probe_target": "PARTIAL",
        "probe_sizing": {
            "target": "PARTIAL",
            "selected_size": "5",
        },
        "candidate": {"condition_id": "condition-1", "asset_id": "asset-1"},
        "market_snapshot": {"book_checkpoint_id": "book-1"},
        "intent": {"side": "BUY", "size": "5"},
        "maker_trade_forecast": {"median_trade_size": "2"},
        "model_predictions": {"PROBABILISTIC_QUEUE": {"p_partial": "0.4"}},
        "maker_probability_calibration": {"artifact_hash": "artifact-1"},
        "prediction_snapshot": {
            "frozen_at": "2026-08-25T23:59:59+00:00",
            "book_checkpoint_id": "book-1",
        },
        "prediction_snapshot_hash": "prediction-1",
        "account_before": {
            "collateral": {"balance": "100"},
            "conditional": {"balance": "0"},
        },
        "signed_order_audit": {"amount": "5", "maker_amount": "5000000"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _journal(path: Path) -> Path:
    DurableUserWsEventJournal(path).append(
        {
            "event_key": "event-1",
            "event_type": "ORDER",
            "payload": {"event_type": "ORDER", "id": "order-1", "status": "CANCELED"},
        }
    )
    return path


def test_recovery_uses_existing_order_and_never_resubmits(tmp_path: Path) -> None:
    adapter = _Adapter()
    output = tmp_path / "recovery.json"
    result = PendingMakerProbeReconciler(
        adapter=adapter,
        user_ws=_UserWs(),
        onchain_truth_reconciler=_OrderFilled(),
    ).reconcile(
        checkpoint_path=_checkpoint(tmp_path / "probe.submission.json"),
        journal_path=_journal(tmp_path / "probe.user-ws.jsonl"),
        output_path=output,
    )

    assert result["status"] == "CALIBRATABLE"
    assert result["exchange_submit_called"] is False
    assert result["resubmit_forbidden"] is True
    assert result["exact_cancel_called"] is False
    assert result["actual_outcome"] == "NO_FILL"
    assert result["probe_target"] == "PARTIAL"
    assert result["cancel_on_first_fill"] is True
    assert result["market_snapshot"]["book_checkpoint_id"] == "book-1"
    assert result["model_predictions"]["PROBABILISTIC_QUEUE"]["p_partial"] == "0.4"
    assert result["prediction_snapshot_hash"] == "prediction-1"
    assert result["outcome_observation"]["label"] == "CENSORED_AT_120S"
    assert output.exists()


def test_open_order_requires_explicit_exact_cancel(tmp_path: Path) -> None:
    adapter = _Adapter(open_order=True)
    reconciler = PendingMakerProbeReconciler(
        adapter=adapter,
        user_ws=_UserWs(),
        onchain_truth_reconciler=_OrderFilled(),
    )
    checkpoint = _checkpoint(tmp_path / "probe.submission.json")
    journal = _journal(tmp_path / "probe.user-ws.jsonl")

    blocked = reconciler.reconcile(
        checkpoint_path=checkpoint,
        journal_path=journal,
        output_path=tmp_path / "blocked.json",
    )
    recovered = reconciler.reconcile(
        checkpoint_path=checkpoint,
        journal_path=journal,
        output_path=tmp_path / "recovered.json",
        cancel_open=True,
    )

    assert blocked["status"] == "OPEN_ORDER_REQUIRES_EXACT_CANCEL"
    assert blocked["exact_cancel_called"] is False
    assert recovered["status"] == "CALIBRATABLE"
    assert recovered["exact_cancel_called"] is True
    assert adapter.cancel_calls == ["order-1"]


def test_recovery_uses_user_ws_transaction_hash_for_chain_truth(tmp_path: Path) -> None:
    adapter = _Adapter()
    chain = _OrderFilled()
    journal_path = _journal(tmp_path / "probe.user-ws.jsonl")
    DurableUserWsEventJournal(journal_path).append(
        {
            "event_key": "trade-event-1",
            "event_type": "TRADE",
            "payload": {
                "event_type": "TRADE",
                "id": "trade-1",
                "status": "CONFIRMED",
                "transaction_hash": "0xABC",
                "maker_orders": [
                    {
                        "order_id": "order-1",
                        "matched_amount": "5",
                        "price": "0.4",
                    }
                ],
            },
        }
    )

    PendingMakerProbeReconciler(
        adapter=adapter,
        user_ws=_UserWs(),
        onchain_truth_reconciler=chain,
    ).reconcile(
        checkpoint_path=_checkpoint(tmp_path / "probe.submission.json"),
        journal_path=journal_path,
        output_path=tmp_path / "recovery.json",
    )

    assert chain.calls[0]["transaction_hashes"] == ("0xabc",)


def test_recovery_excludes_other_orders_on_the_same_market(tmp_path: Path) -> None:
    adapter = _Adapter()
    adapter.get_order_reconciliation_snapshot = lambda **_kwargs: {
        "order": {"id": "order-1", "status": "MATCHED"},
        "trades": [
            {
                "event_type": "TRADE",
                "status": "CONFIRMED",
                "transaction_hash": "0xGOOD",
                "maker_orders": [
                    {"order_id": "order-1", "matched_amount": "5", "price": "0.4"}
                ],
            },
            {
                "event_type": "TRADE",
                "status": "CONFIRMED",
                "transaction_hash": "0xLATER",
                "maker_orders": [
                    {"order_id": "order-2", "matched_amount": "1", "price": "0.4"}
                ],
            },
        ],
        "open_orders": [],
        "order_lookup_error": None,
    }
    chain = _OrderFilled()

    PendingMakerProbeReconciler(
        adapter=adapter,
        user_ws=_UserWs(),
        onchain_truth_reconciler=chain,
    ).reconcile(
        checkpoint_path=_checkpoint(tmp_path / "probe.submission.json"),
        journal_path=_journal(tmp_path / "probe.user-ws.jsonl"),
        output_path=tmp_path / "recovery.json",
    )

    assert chain.calls[0]["transaction_hashes"] == ("0xgood",)
