from datetime import datetime, timezone

from quant.calibration.official_execution_evidence import (
    _probe_transaction_hashes,
    render_official_execution_evidence_markdown,
)


def test_probe_transaction_hashes_only_accepts_explicit_transaction_fields() -> None:
    tx_hash = "0x" + "a" * 64
    order_hash = "0x" + "b" * 64
    row = {
        "lifecycle": {
            "transaction_hashes": [tx_hash],
            "order_hash": order_hash,
            "rest_trades": [{"transactionHash": tx_hash.upper()}],
        },
        "reconciliation": {},
    }

    assert _probe_transaction_hashes(row) == {tx_hash}


def test_markdown_keeps_trade_settlement_and_rewards_separate() -> None:
    report = {
        "generated_at": datetime(2026, 8, 19, tzinfo=timezone.utc).isoformat(),
        "status": "COLLECTING",
        "taker": {
            "calibratable_count": 25,
            "holdout_count": 3,
            "condition_count": 20,
            "utc_day_count": 8,
            "official_trade_transaction_count": 47,
            "official_transactions_linked_to_calibratable_probe": 24,
            "official_transactions_without_strict_probe_artifact": 23,
        },
        "settlement": {
            "status": "PASS",
            "status_counts": {"PASS": 5},
            "pending_count": 0,
            "official_activity_reconciled": 1,
            "chain_receipt_reconciled": 1,
        },
        "rewards": {"status": "NO_OFFICIAL_EVIDENCE", "activity_counts": {}},
        "next_actions": ["continue holdout"],
    }

    rendered = render_official_execution_evidence_markdown(report)

    assert "Linked to strict probe artifacts: `24`" in rendered
    assert "PASS / pending: `5` / `0`" in rendered
    assert "NO_OFFICIAL_EVIDENCE" in rendered
