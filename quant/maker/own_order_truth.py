"""Corroborate authenticated Maker fills with canonical OrderFilled rows."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from quant.adapters.polygon_rpc_client import PolygonRpcClient
from quant.paper.paired_probe import OrderFilledEvidenceClient

ORDER_FILLED_V1_TOPIC = (
    "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
)
ORDER_FILLED_V2_TOPIC = (
    "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
)
OFFICIAL_EXCHANGE_EVENT_TOPICS = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e": ORDER_FILLED_V1_TOPIC,
    "0xc5d563a36ae78145c45a50134d48a1215220f80a": ORDER_FILLED_V1_TOPIC,
    "0xe111180000d2663c0091e4f400237545b87b996b": ORDER_FILLED_V2_TOPIC,
    "0xe2222d279d744050d28e00520010520000310f59": ORDER_FILLED_V2_TOPIC,
}


class OwnOrderFilledTruthReconciler:
    """Fail closed until an own order's exact on-chain fill rows are indexed."""

    def __init__(
        self,
        client: OrderFilledEvidenceClient | None = None,
        *,
        receipt_client: Any | None = None,
        require_receipts: bool | None = None,
    ) -> None:
        production_default = client is None
        self.client = client or OrderFilledEvidenceClient()
        if receipt_client is None and production_default:
            receipt_client = PolygonRpcClient(
                rpc_url=str(
                    os.environ.get("POLY_QUANT_POLYGON_RPC_URL")
                    or os.environ.get("POLYGON_RPC_URL")
                    or "https://polygon-bor-rpc.publicnode.com"
                ),
                proxy_url=str(os.environ.get("POLY_QUANT_POLYGON_RPC_PROXY_URL") or "")
                or None,
            )
        self.receipt_client = receipt_client
        self.require_receipts = (
            production_default or receipt_client is not None
            if require_receipts is None
            else bool(require_receipts)
        )

    def reconcile(
        self,
        *,
        order_id: str,
        asset_id: str,
        expected_matched_size: Decimal,
        transaction_hashes: Iterable[str],
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        expected = max(Decimal("0"), Decimal(expected_matched_size))
        normalized_order = _identity(order_id)
        normalized_asset = _asset_identity(asset_id)
        expected_transactions = {
            value for value in (_identity(item) for item in transaction_hashes) if value
        }
        base = {
            "schema_version": "maker_own_orderfilled_truth_v1",
            "order_id": normalized_order,
            "asset_id": str(asset_id),
            "expected_matched_size": format(expected, "f"),
            "expected_transaction_hashes": sorted(expected_transactions),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "source": "canonical_clickhouse_orderfilled",
            "used_for_actual_live_outcome": True,
        }
        if expected <= 0:
            return {
                **base,
                "status": "NOT_REQUIRED_NO_FILL",
                "matched_size": "0",
                "matched_rows": [],
                "matched_transaction_hashes": [],
                "transaction_coverage_complete": True,
                "receipt_truth": {
                    "status": "NOT_REQUIRED_NO_FILL",
                    "complete": True,
                    "receipts": [],
                },
            }

        rows = (
            self.client.fetch_transactions(expected_transactions)
            if expected_transactions
            else self.client.fetch_window(
                asset_id=str(asset_id),
                start=window_start,
                end=window_end,
            )
        )
        indexed_matches = [
            dict(row)
            for row in rows
            if _identity(row.get("order_hash") or row.get("order_id"))
            == normalized_order
            and _asset_identity(row.get("asset_id")) == normalized_asset
        ]
        receipt_truth = self._receipt_truth(
            sorted(expected_transactions),
            order_id=normalized_order,
            asset_id=normalized_asset,
        )
        receipt_matches = list(receipt_truth.get("orderfilled_rows") or ())
        source_conflict = bool(
            indexed_matches
            and receipt_matches
            and not _same_orderfilled_economics(indexed_matches, receipt_matches)
        )
        matches = indexed_matches or receipt_matches
        matched_size = _matched_size(matches)
        matched_transactions = {
            value
            for value in (
                _identity(row.get("tx_hash") or row.get("transaction_hash"))
                for row in matches
            )
            if value
        }
        tolerance = max(Decimal("0.000001"), expected * Decimal("0.000001"))
        transaction_coverage = expected_transactions <= matched_transactions
        if source_conflict:
            status = "SOURCE_MISMATCH"
        elif not matches:
            status = "PENDING_CHAIN_INDEX"
        elif abs(matched_size - expected) > tolerance:
            status = "SIZE_MISMATCH"
        elif not transaction_coverage:
            status = "TRANSACTION_MISMATCH"
        elif not receipt_truth["complete"]:
            status = (
                "RECEIPT_FAILED"
                if receipt_truth["status"] == "RECEIPT_FAILED"
                else "PENDING_RECEIPT"
            )
        else:
            status = "CONFIRMED_MATCH"
        return {
            **base,
            "status": status,
            "matched_size": format(matched_size, "f"),
            "matched_rows": matches,
            "indexed_matched_rows": indexed_matches,
            "receipt_matched_rows": receipt_matches,
            "matched_source": (
                "canonical_clickhouse_orderfilled"
                if indexed_matches
                else "polygon_receipt_orderfilled"
                if receipt_matches
                else None
            ),
            "index_pending": not bool(indexed_matches),
            "source_conflict": source_conflict,
            "matched_transaction_hashes": sorted(matched_transactions),
            "transaction_coverage_complete": transaction_coverage,
            "receipt_truth": receipt_truth,
            "size_tolerance": format(tolerance, "f"),
        }

    def _receipt_truth(
        self,
        transaction_hashes: list[str],
        *,
        order_id: str,
        asset_id: str,
    ) -> dict[str, Any]:
        if not self.require_receipts:
            return {
                "status": "NOT_REQUIRED_BY_CALLER",
                "complete": True,
                "receipts": [],
                "orderfilled_rows": [],
            }
        if not transaction_hashes:
            return {
                "status": "PENDING_TRANSACTION_HASH",
                "complete": False,
                "receipts": [],
                "orderfilled_rows": [],
            }
        if self.receipt_client is None:
            return {
                "status": "SOURCE_UNAVAILABLE",
                "complete": False,
                "receipts": [],
                "orderfilled_rows": [],
            }
        receipts: list[dict[str, Any]] = []
        orderfilled_rows: list[dict[str, Any]] = []
        for tx_hash in transaction_hashes:
            try:
                payload = self.receipt_client.get_transaction_receipt(tx_hash)
            except Exception as exc:  # noqa: BLE001
                return {
                    "status": "SOURCE_UNAVAILABLE",
                    "complete": False,
                    "receipts": receipts,
                    "orderfilled_rows": orderfilled_rows,
                    "error": f"{exc.__class__.__name__}:{str(exc)[:500]}",
                }
            result = payload.get("result") if isinstance(payload, Mapping) else None
            if not isinstance(result, Mapping):
                return {
                    "status": "INVALID_RECEIPT",
                    "complete": False,
                    "receipts": receipts,
                    "orderfilled_rows": orderfilled_rows,
                }
            observed_hash = _identity(result.get("transactionHash"))
            receipt_status = str(result.get("status") or "").lower()
            row = {
                "transaction_hash": observed_hash,
                "status": receipt_status,
                "block_number": result.get("blockNumber"),
                "block_hash": result.get("blockHash"),
                "payload_sha256": hashlib.sha256(
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                ).hexdigest(),
            }
            receipts.append(row)
            orderfilled_rows.extend(
                decode_orderfilled_logs(
                    result.get("logs"),
                    transaction_hash=observed_hash,
                    order_id=order_id,
                    asset_id=asset_id,
                    block_number=result.get("blockNumber"),
                )
            )
            if observed_hash != tx_hash or receipt_status != "0x1":
                return {
                    "status": "RECEIPT_FAILED",
                    "complete": False,
                    "receipts": receipts,
                    "orderfilled_rows": orderfilled_rows,
                }
        return {
            "status": "CONFIRMED_SUCCESS",
            "complete": True,
            "receipts": receipts,
            "orderfilled_rows": orderfilled_rows,
        }


def decode_orderfilled_logs(
    logs: Any,
    *,
    transaction_hash: str,
    order_id: str,
    asset_id: str,
    block_number: Any,
) -> list[dict[str, Any]]:
    if not isinstance(logs, list):
        return []
    expected_order = _identity(order_id)
    expected_asset = _asset_identity(asset_id)
    rows: list[dict[str, Any]] = []
    for raw in logs:
        if not isinstance(raw, Mapping):
            continue
        address = _identity(raw.get("address"))
        topics = raw.get("topics")
        if (
            address not in OFFICIAL_EXCHANGE_EVENT_TOPICS
            or not isinstance(topics, list)
            or len(topics) < 2
            or _identity(topics[0]) != OFFICIAL_EXCHANGE_EVENT_TOPICS[address]
            or _identity(topics[1]) != expected_order
        ):
            continue
        words = _abi_words(raw.get("data"))
        if OFFICIAL_EXCHANGE_EVENT_TOPICS[address] == ORDER_FILLED_V2_TOPIC:
            if len(words) != 7:
                continue
            side, token_id, maker_amount, taker_amount, fee, builder, metadata = words
            if _asset_identity(token_id) != expected_asset or side not in {0, 1}:
                continue
            maker_asset = 0 if side == 0 else token_id
            taker_asset = token_id if side == 0 else 0
            size_raw = taker_amount if side == 0 else maker_amount
            quote_raw = maker_amount if side == 0 else taker_amount
        else:
            if len(words) != 5:
                continue
            maker_asset, taker_asset, maker_amount, taker_amount, fee = words
            builder = metadata = 0
            side = 0 if maker_asset == 0 else 1
            if _asset_identity(maker_asset) == expected_asset:
                size_raw = maker_amount
                quote_raw = taker_amount if taker_asset == 0 else 0
            elif _asset_identity(taker_asset) == expected_asset:
                size_raw = taker_amount
                quote_raw = maker_amount if maker_asset == 0 else 0
            else:
                continue
        if size_raw <= 0:
            continue
        size = Decimal(size_raw) / Decimal("1000000")
        quote = Decimal(quote_raw) / Decimal("1000000")
        rows.append(
            {
                "tx_hash": transaction_hash,
                "log_index": _hex_int(raw.get("logIndex")),
                "block_number": _hex_int(block_number),
                "exchange_address": address,
                "order_hash": expected_order,
                "maker": "0x" + _identity(topics[2])[-40:] if len(topics) > 2 else "",
                "taker": "0x" + _identity(topics[3])[-40:] if len(topics) > 3 else "",
                "maker_asset_id": str(maker_asset),
                "taker_asset_id": str(taker_asset),
                "side_code": side,
                "asset_id": str(asset_id),
                "size": format(size, "f"),
                "quote_amount": format(quote, "f"),
                "price": format(quote / size, "f") if quote > 0 else None,
                "fee_raw": str(fee),
                "builder": f"0x{builder:064x}",
                "metadata": f"0x{metadata:064x}",
                "source": "polygon_receipt_orderfilled",
            }
        )
    return rows


# Kept for callers pinned to the original private helper name.
_decode_orderfilled_logs = decode_orderfilled_logs


def _abi_words(value: Any) -> list[int]:
    text = str(value or "").strip().lower().removeprefix("0x")
    if not text or len(text) % 64 != 0:
        return []
    try:
        return [int(text[index : index + 64], 16) for index in range(0, len(text), 64)]
    except ValueError:
        return []


def _hex_int(value: Any) -> int:
    text = str(value or "0").strip().lower()
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return 0


def _matched_size(rows: Iterable[Mapping[str, Any]]) -> Decimal:
    return sum(
        (max(Decimal("0"), _decimal(row.get("size"))) for row in rows),
        Decimal("0"),
    )


def _same_orderfilled_economics(
    indexed: Iterable[Mapping[str, Any]],
    receipt: Iterable[Mapping[str, Any]],
) -> bool:
    indexed_rows = list(indexed)
    receipt_rows = list(receipt)
    return bool(
        _matched_size(indexed_rows) == _matched_size(receipt_rows)
        and {
            _identity(row.get("tx_hash") or row.get("transaction_hash"))
            for row in indexed_rows
        }
        == {
            _identity(row.get("tx_hash") or row.get("transaction_hash"))
            for row in receipt_rows
        }
    )


def trade_transaction_hashes(
    rows: Iterable[Mapping[str, Any]],
    *,
    order_id: str | None = None,
) -> tuple[str, ...]:
    expected_order = _identity(order_id)
    return tuple(
        sorted(
            {
                value
                for row in rows
                if isinstance(row, Mapping)
                and (not expected_order or _row_references_order(row, expected_order))
                for value in (
                    _identity(row.get("transaction_hash") or row.get("tx_hash")),
                )
                if value
            }
        )
    )


def _row_references_order(row: Mapping[str, Any], order_id: str) -> bool:
    direct = {
        _identity(row.get(key))
        for key in ("order_id", "orderID", "id", "taker_order_id", "order_hash")
        if row.get(key) not in (None, "")
    }
    maker_orders = (
        row.get("maker_orders") if isinstance(row.get("maker_orders"), list) else []
    )
    direct.update(
        _identity(item.get("order_id"))
        for item in maker_orders
        if isinstance(item, Mapping)
    )
    return order_id in direct


def _asset_identity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    try:
        return str(int(text, 0) if text.startswith("0x") else int(text))
    except ValueError:
        return text.removeprefix("0x")


def _identity(value: Any) -> str:
    return str(value or "").strip().lower()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")
