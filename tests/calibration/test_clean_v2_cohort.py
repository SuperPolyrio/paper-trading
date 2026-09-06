from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock

import quant.calibration.clean_v2_cohort as clean_v2_module
from quant.calibration.clean_v2_cohort import (
    CleanV2CohortService,
    _staging_baseline_entries,
    _terminal_probe_truth_is_frozen,
    _transaction_receipt_status,
    build_operation_evidence,
)

STRATEGY_ID = "post-v2-clean-cohort-1"
STAGING_STRATEGY_ID = "post-v2-stage-calrun-1"
TX_HASH = "0x" + "a" * 64


def test_staging_baseline_journals_cloned_cash_positions_and_realized_pnl() -> None:
    entries = _staging_baseline_entries(
        run_id="calrun-1",
        staging_strategy_id=STAGING_STRATEGY_ID,
        source_strategy_id=STRATEGY_ID,
        baseline_hash="b" * 64,
        account={
            "initial_cash": Decimal("10000"),
            "cash_balance": Decimal("9997.5"),
            "realized_pnl": Decimal("0.25"),
        },
        positions=(
            {
                "asset_id": "asset-1",
                "market_id": "market-1",
                "condition_id": "condition-1",
                "quantity": Decimal("5"),
                "cost_basis": Decimal("2.5"),
                "realized_pnl": Decimal("0.1"),
            },
        ),
    )

    assert sum((row["cash_delta"] for row in entries), Decimal(0)) == Decimal(
        "-2.5"
    )
    assert sum((row["shares_delta"] for row in entries), Decimal(0)) == Decimal(
        "5"
    )
    assert sum(
        (row["realized_pnl_delta"] for row in entries), Decimal(0)
    ) == Decimal("0.25")
    assert all(row["metadata"]["non_economic_clone"] for row in entries)


def _filled_probe() -> dict:
    return {
        "run_id": "calrun-1",
        "probe_id": "calprobe-1",
        "paired_probe_id": "paired-1",
        "paper_strategy_id": STRATEGY_ID,
        "probe_state": "CALIBRATABLE",
        "exchange_submit_called": True,
        "prediction": {"status": "FILLED", "filled_size": "2"},
        "lifecycle": {
            "order_id": "order-1",
            "user_ws_capture": {"status": "TERMINAL"},
            "rest_order": {"id": "order-1", "status": "MATCHED"},
            "rest_trades": [
                {
                    "id": "trade-1",
                    "size": "2",
                    "price": "0.5",
                    "taker_order_id": "order-1",
                    "transaction_hash": TX_HASH,
                }
            ],
            "trade_ids": ["trade-1"],
            "transaction_hashes": [TX_HASH],
        },
        "reconciliation": {
            "predicted_class": "FULL",
            "actual_class": "FULL",
            "price_error_ticks": "0",
            "fee_error": "0",
            "order": {"actual_matched_size": "2"},
            "accounting": {"accounting_reconciled": True},
            "paired_probe_sync": {"status": "PASS"},
            "pnl": {"status": "PASS", "pnl_reconciled": True},
        },
        "timestamps": {
            "http_send_started_ts": "2026-09-03T01:00:00+00:00",
            "trade_confirmed_ts": "2026-09-03T01:00:02+00:00",
        },
    }


def _paired() -> dict:
    return {
        "probe_id": "paired-1",
        "paper_intent_id": 91,
        "strategy_id": STAGING_STRATEGY_ID,
        "paper_prediction": {"status": "FILLED"},
    }


def _receipt() -> dict:
    return {
        "transaction_hash": TX_HASH,
        "content_sha256": "b" * 64,
        "artifact_path": "/tmp/receipt.json",
        "rpc_source": "https://polygon.example",
        "status": "CONFIRMED",
        "orderfilled_rows": [
            {
                "order_hash": "order-1",
                "asset_id": "asset-1",
                "size": "2",
                "quote_amount": "1",
                "fee_raw": "0",
            }
        ],
    }


def _fee_finality(status: str = "FINALITY_EXACT") -> dict:
    return {
        "status": status,
        "modeled_total_fee": "0",
        "official_total_fee": "0",
        "cash_correction": "0",
        "evidence_id": "polygon-orderfilled-v2:order-1:evidence",
        "evidence_sha256": "e" * 64,
    }


def test_transaction_receipt_status_accepts_rpc_envelope_and_bare_receipt() -> None:
    assert _transaction_receipt_status({"status": "0x1"}) == "0x1"
    assert (
        _transaction_receipt_status(
            {"jsonrpc": "2.0", "id": 1, "result": {"status": "0x1"}}
        )
        == "0x1"
    )
    assert _transaction_receipt_status({"result": None}) == ""


def test_only_fully_reconciled_terminal_probe_truth_is_frozen() -> None:
    probe = _filled_probe()
    probe["reconciliation"]["order"]["final_trade_status_present"] = True

    assert _terminal_probe_truth_is_frozen(probe) is True

    probe["reconciliation"]["accounting"]["accounting_reconciled"] = False
    assert _terminal_probe_truth_is_frozen(probe) is False


def test_refresh_preserves_frozen_terminal_truth(monkeypatch) -> None:
    probe = _filled_probe()
    probe["reconciliation"]["order"]["final_trade_status_present"] = True
    probe["reconciliation"]["order"]["sentinel"] = "immutable"
    calibration_store = Mock()
    calibration_store.load_events.return_value = []
    calibration_store.upsert_probe.side_effect = lambda row: dict(row)
    service = CleanV2CohortService(
        store=Mock(),
        account_truth=Mock(),
        calibration_store=calibration_store,
        shadow_store=Mock(),
        live_adapter=Mock(),
    )
    service.store.load_operation_by_run.return_value = {
        "staged_paper_audit_key": "a" * 64,
        "paper_staging_strategy_id": STAGING_STRATEGY_ID,
        "signed_paper_audit_key": "b" * 64,
        "signed_prediction_frozen_at": "2026-09-03T01:00:00+00:00",
        "committed_paper_audit_key": "c" * 64,
    }
    monkeypatch.setattr(
        clean_v2_module,
        "sync_calibration_probe_to_paired",
        lambda *_args, **_kwargs: {"probe_id": "paired-1", "status": "PASS"},
    )

    persisted, _events = service._refresh_probe_truth(probe)

    service.live_adapter.get_order_reconciliation_snapshot.assert_not_called()
    calibration_store.apply_probe_pnl.assert_not_called()
    assert persisted["reconciliation"]["order"]["sentinel"] == "immutable"


def test_filled_operation_requires_all_independent_truth_sources() -> None:
    result = build_operation_evidence(
        probe=_filled_probe(),
        paired=_paired(),
        receipt_manifest=[_receipt()],
        expected_paper_strategy_id=STRATEGY_ID,
        expected_staging_strategy_id=STAGING_STRATEGY_ID,
        committed_paper_audit_key="c" * 64,
        signed_paper_audit_key="d" * 64,
        user_ws_events=[{"source": "polymarket-user-ws-v2", "payload": {}}],
        fee_finality=_fee_finality(),
    )

    assert result["evidence_status"] == "EVIDENCE_READY"
    assert all(result["checks"].values())
    assert result["raw_sources"]["rest_trades"][0]["id"] == "trade-1"


def test_filled_operation_without_ledger_fee_finality_remains_pending() -> None:
    result = build_operation_evidence(
        probe=_filled_probe(),
        paired=_paired(),
        receipt_manifest=[_receipt()],
        expected_paper_strategy_id=STRATEGY_ID,
        expected_staging_strategy_id=STAGING_STRATEGY_ID,
        committed_paper_audit_key="c" * 64,
        signed_paper_audit_key="d" * 64,
        user_ws_events=[{"source": "polymarket-user-ws-v2", "payload": {}}],
    )

    assert result["evidence_status"] == "AWAITING_ACCOUNT_TRUTH"
    assert result["checks"]["paper_ledger_fee_finalized"] is False


def test_filled_operation_without_receipt_remains_pending() -> None:
    result = build_operation_evidence(
        probe=_filled_probe(),
        paired=_paired(),
        receipt_manifest=[],
        receipt_errors=["receipt unavailable"],
        expected_paper_strategy_id=STRATEGY_ID,
        expected_staging_strategy_id=STAGING_STRATEGY_ID,
        committed_paper_audit_key="c" * 64,
        signed_paper_audit_key="d" * 64,
        user_ws_events=[{"source": "polymarket-user-ws-v2", "payload": {}}],
    )

    assert result["evidence_status"] == "AWAITING_RECEIPT"
    assert result["checks"]["all_transaction_receipts_confirmed"] is False


def test_strategy_mismatch_cannot_create_ready_evidence() -> None:
    paired = deepcopy(_paired())
    paired["strategy_id"] = "another-staging-account"

    result = build_operation_evidence(
        probe=_filled_probe(),
        paired=paired,
        receipt_manifest=[_receipt()],
        expected_paper_strategy_id=STRATEGY_ID,
        expected_staging_strategy_id=STAGING_STRATEGY_ID,
        committed_paper_audit_key="c" * 64,
        signed_paper_audit_key="d" * 64,
        user_ws_events=[{"source": "polymarket-user-ws-v2", "payload": {}}],
    )

    assert result["evidence_status"] == "AWAITING_ACCOUNT_TRUTH"
    assert result["checks"]["paper_strategy_matches"] is False


def test_no_fill_needs_no_fake_receipt() -> None:
    probe = _filled_probe()
    probe["prediction"] = {"status": "NO_FILL", "filled_size": "0"}
    probe["lifecycle"]["rest_trades"] = []
    probe["lifecycle"]["trade_ids"] = []
    probe["lifecycle"]["transaction_hashes"] = []
    probe["reconciliation"]["predicted_class"] = "NO_FILL"
    probe["reconciliation"]["actual_class"] = "NO_FILL"
    probe["reconciliation"]["order"]["actual_matched_size"] = "0"
    probe["reconciliation"]["pnl"] = {
        "status": "NO_FILL",
        "pnl_reconciled": True,
    }

    result = build_operation_evidence(
        probe=probe,
        paired=_paired(),
        receipt_manifest=[],
        expected_paper_strategy_id=STRATEGY_ID,
        expected_staging_strategy_id=STAGING_STRATEGY_ID,
        committed_paper_audit_key="c" * 64,
        signed_paper_audit_key="d" * 64,
    )

    assert result["evidence_status"] == "NO_FILL_VERIFIED"
    assert result["checks"]["transaction_hash_present_when_filled"] is True


def test_submitted_operation_without_cohort_paper_commit_cannot_pass() -> None:
    result = build_operation_evidence(
        probe=_filled_probe(),
        paired=_paired(),
        receipt_manifest=[_receipt()],
        expected_paper_strategy_id=STRATEGY_ID,
        expected_staging_strategy_id=STAGING_STRATEGY_ID,
        committed_paper_audit_key=None,
        signed_paper_audit_key="d" * 64,
        user_ws_events=[{"source": "polymarket-user-ws-v2", "payload": {}}],
    )

    assert result["evidence_status"] == "AWAITING_ACCOUNT_TRUTH"
    assert result["checks"]["cohort_paper_prediction_committed"] is False


def test_complete_sources_cannot_hide_paper_live_execution_mismatch() -> None:
    probe = _filled_probe()
    probe["reconciliation"]["price_error_ticks"] = "2"

    result = build_operation_evidence(
        probe=probe,
        paired=_paired(),
        receipt_manifest=[_receipt()],
        expected_paper_strategy_id=STRATEGY_ID,
        expected_staging_strategy_id=STAGING_STRATEGY_ID,
        committed_paper_audit_key="c" * 64,
        signed_paper_audit_key="d" * 64,
        user_ws_events=[{"source": "polymarket-user-ws-v2", "payload": {}}],
    )

    assert result["evidence_status"] == "AWAITING_ACCOUNT_TRUTH"
    assert result["checks"]["paper_live_vwap_within_one_tick"] is False
    assert result["execution_comparison"]["price_error_ticks"] == "2"


def test_chain_fee_is_authoritative_over_reconstructed_trade_fee() -> None:
    probe = _filled_probe()
    probe["prediction"]["total_fee"] = "0.05828"
    probe["reconciliation"]["fee_error"] = "0"
    receipt = _receipt()
    receipt["orderfilled_rows"][0]["fee_raw"] = "58270"

    result = build_operation_evidence(
        probe=probe,
        paired=_paired(),
        receipt_manifest=[receipt],
        expected_paper_strategy_id=STRATEGY_ID,
        expected_staging_strategy_id=STAGING_STRATEGY_ID,
        committed_paper_audit_key="c" * 64,
        signed_paper_audit_key="d" * 64,
        user_ws_events=[{"source": "polymarket-user-ws-v2", "payload": {}}],
        fee_finality=_fee_finality("FINALITY_CORRECTED"),
    )

    comparison = result["execution_comparison"]
    assert comparison["paper_fee"] == "0.05828"
    assert comparison["onchain_fee"] == "0.05827"
    assert comparison["fee_error"] == "0.00001"
    assert comparison["fee_truth_source"] == "POLYGON_ORDER_FILLED_V2"
    assert result["checks"]["paper_live_fee_matches"] is True
    assert result["checks"]["paper_ledger_fee_finalized"] is True


def test_same_asset_trade_for_another_order_is_excluded() -> None:
    probe = _filled_probe()
    probe["lifecycle"]["rest_trades"].append(
        {
            "id": "later-sell",
            "size": "2",
            "price": "0.4",
            "taker_order_id": "order-2",
            "transaction_hash": "0x" + "c" * 64,
        }
    )
    probe["lifecycle"]["transaction_hashes"].append("0x" + "c" * 64)

    result = build_operation_evidence(
        probe=probe,
        paired=_paired(),
        receipt_manifest=[_receipt()],
        expected_paper_strategy_id=STRATEGY_ID,
        expected_staging_strategy_id=STAGING_STRATEGY_ID,
        committed_paper_audit_key="c" * 64,
        signed_paper_audit_key="d" * 64,
        user_ws_events=[{"source": "polymarket-user-ws-v2", "payload": {}}],
        fee_finality=_fee_finality(),
    )

    assert result["evidence_status"] == "EVIDENCE_READY"
    assert [row["id"] for row in result["raw_sources"]["rest_trades"]] == [
        "trade-1"
    ]
    assert result["lifecycle"]["transaction_hashes"] == [TX_HASH]
