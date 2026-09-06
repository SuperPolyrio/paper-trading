from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant.maker.own_order_truth import (
    OwnOrderFilledTruthReconciler,
    trade_transaction_hashes,
)

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)
ORDER_ID = "0x" + "12" * 32
TX_HASH = "0x" + "34" * 32


class _Client:
    def __init__(self, rows):
        self.rows = rows
        self.transaction_queries = []

    def fetch_transactions(self, hashes):
        self.transaction_queries.append(tuple(sorted(hashes)))
        return list(self.rows)

    def fetch_window(self, **_kwargs):
        return list(self.rows)


class _ReceiptClient:
    def __init__(self, *, status="0x1", logs=None):
        self.status = status
        self.logs = list(logs or [])
        self.queries = []

    def get_transaction_receipt(self, transaction_hash):
        self.queries.append(transaction_hash)
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "transactionHash": transaction_hash,
                "status": self.status,
                "blockNumber": "0x10",
                "blockHash": "0x" + "78" * 32,
                "logs": self.logs,
            },
        }


def test_exact_orderfilled_rows_confirm_authenticated_size() -> None:
    client = _Client(
        [
            {
                "order_hash": ORDER_ID,
                "asset_id": "123",
                "size": "2",
                "tx_hash": TX_HASH,
                "log_index": 7,
            },
            {
                "order_hash": "0x" + "56" * 32,
                "asset_id": "123",
                "size": "99",
                "tx_hash": TX_HASH,
                "log_index": 8,
            },
        ]
    )

    receipts = _ReceiptClient()
    result = OwnOrderFilledTruthReconciler(
        client,
        receipt_client=receipts,
    ).reconcile(
        order_id=ORDER_ID,
        asset_id="123",
        expected_matched_size=Decimal("2"),
        transaction_hashes=(TX_HASH,),
        window_start=NOW - timedelta(minutes=1),
        window_end=NOW + timedelta(minutes=1),
    )

    assert result["status"] == "CONFIRMED_MATCH"
    assert result["matched_size"] == "2"
    assert len(result["matched_rows"]) == 1
    assert result["receipt_truth"]["status"] == "CONFIRMED_SUCCESS"
    assert receipts.queries == [TX_HASH]
    assert client.transaction_queries == [(TX_HASH,)]


def test_missing_or_wrong_sized_chain_rows_fail_closed() -> None:
    missing = OwnOrderFilledTruthReconciler(_Client([])).reconcile(
        order_id=ORDER_ID,
        asset_id="123",
        expected_matched_size=Decimal("2"),
        transaction_hashes=(TX_HASH,),
        window_start=NOW,
        window_end=NOW,
    )
    mismatched = OwnOrderFilledTruthReconciler(
        _Client(
            [
                {
                    "order_hash": ORDER_ID,
                    "asset_id": "0x7b",
                    "size": "1",
                    "tx_hash": TX_HASH,
                }
            ]
        )
    ).reconcile(
        order_id=ORDER_ID,
        asset_id="123",
        expected_matched_size=Decimal("2"),
        transaction_hashes=(TX_HASH,),
        window_start=NOW,
        window_end=NOW,
    )

    assert missing["status"] == "PENDING_CHAIN_INDEX"
    assert mismatched["status"] == "SIZE_MISMATCH"


def test_no_fill_does_not_fabricate_orderfilled_evidence() -> None:
    client = _Client([])
    result = OwnOrderFilledTruthReconciler(client).reconcile(
        order_id=ORDER_ID,
        asset_id="123",
        expected_matched_size=Decimal("0"),
        transaction_hashes=(),
        window_start=NOW,
        window_end=NOW,
    )

    assert result["status"] == "NOT_REQUIRED_NO_FILL"
    assert result["matched_rows"] == []
    assert result["receipt_truth"]["status"] == "NOT_REQUIRED_NO_FILL"
    assert client.transaction_queries == []


def test_transaction_hashes_are_scoped_to_exact_order() -> None:
    later_order = "0x" + "56" * 32
    later_tx = "0x" + "78" * 32
    rows = [
        {
            "event_type": "TRADE",
            "transaction_hash": TX_HASH,
            "maker_orders": [{"order_id": ORDER_ID, "matched_amount": "2"}],
        },
        {
            "event_type": "TRADE",
            "transaction_hash": later_tx,
            "maker_orders": [{"order_id": later_order, "matched_amount": "1"}],
        },
    ]

    assert trade_transaction_hashes(rows, order_id=ORDER_ID) == (TX_HASH,)


def test_failed_polygon_receipt_blocks_confirmed_match() -> None:
    client = _Client(
        [
            {
                "order_hash": ORDER_ID,
                "asset_id": "123",
                "size": "2",
                "tx_hash": TX_HASH,
            }
        ]
    )
    result = OwnOrderFilledTruthReconciler(
        client,
        receipt_client=_ReceiptClient(status="0x0"),
    ).reconcile(
        order_id=ORDER_ID,
        asset_id="123",
        expected_matched_size=Decimal("2"),
        transaction_hashes=(TX_HASH,),
        window_start=NOW,
        window_end=NOW,
    )

    assert result["status"] == "RECEIPT_FAILED"
    assert result["receipt_truth"]["complete"] is False


def test_exact_receipt_orderfilled_is_authoritative_while_index_lags() -> None:
    asset_id = 123
    size_raw = 2_000_000
    quote_raw = 800_000
    data = "0x" + "".join(
        f"{value:064x}" for value in (0, asset_id, quote_raw, size_raw, 0, 0, 0)
    )
    receipt = _ReceiptClient(
        logs=[
            {
                "address": "0xE111180000d2663C0091e4f400237545B87B996B",
                "topics": [
                    "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee",
                    ORDER_ID,
                    "0x" + "00" * 12 + "11" * 20,
                    "0x" + "00" * 12 + "22" * 20,
                ],
                "data": data,
                "logIndex": "0x7",
            }
        ]
    )

    result = OwnOrderFilledTruthReconciler(
        _Client([]),
        receipt_client=receipt,
    ).reconcile(
        order_id=ORDER_ID,
        asset_id=str(asset_id),
        expected_matched_size=Decimal("2"),
        transaction_hashes=(TX_HASH,),
        window_start=NOW,
        window_end=NOW,
    )

    assert result["status"] == "CONFIRMED_MATCH"
    assert result["matched_source"] == "polygon_receipt_orderfilled"
    assert result["index_pending"] is True
    assert result["matched_size"] == "2"
    assert result["matched_rows"][0]["price"] == "0.4"
    assert result["matched_rows"][0]["order_hash"] == ORDER_ID
