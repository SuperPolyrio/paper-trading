"""Small explicit-route Polygon JSON-RPC adapter for immutable chain evidence."""

from __future__ import annotations

from typing import Any, Mapping

import httpx


class PolygonRpcError(RuntimeError):
    pass


class PolygonRpcClient:
    def __init__(
        self,
        *,
        rpc_url: str,
        proxy_url: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.rpc_url = str(rpc_url)
        self.proxy_url = str(proxy_url or "").strip() or None
        self.timeout_seconds = max(0.1, float(timeout_seconds))

    def get_transaction_receipt(self, transaction_hash: str) -> dict[str, Any]:
        tx_hash = str(transaction_hash).lower()
        if not tx_hash.startswith("0x"):
            raise ValueError("transaction hash must start with 0x")
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
        }
        try:
            with httpx.Client(
                proxy=self.proxy_url,
                timeout=self.timeout_seconds,
                trust_env=False,
            ) as client:
                response = client.post(self.rpc_url, json=request)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - normalize external transport errors.
            raise PolygonRpcError(f"Polygon receipt request failed: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise PolygonRpcError("Polygon receipt response is not an object")
        if payload.get("error"):
            raise PolygonRpcError(f"Polygon receipt RPC error: {payload['error']}")
        receipt = payload.get("result")
        if not isinstance(receipt, Mapping):
            raise PolygonRpcError("Polygon transaction receipt is absent")
        if str(receipt.get("transactionHash") or "").lower() != tx_hash:
            raise PolygonRpcError("Polygon receipt transaction hash mismatch")
        return dict(payload)
