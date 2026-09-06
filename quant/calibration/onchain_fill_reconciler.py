"""Bounded OrderFilled lookup with complete self-identity exclusion."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from quant.paper.paired_probe import OrderFilledEvidenceClient

from .self_trade_filter import SelfIdentity, filter_self_evidence


class OnchainFillReconciler:
    def __init__(self, client: OrderFilledEvidenceClient | None = None) -> None:
        self.client = client or OrderFilledEvidenceClient()

    def reconcile(
        self,
        *,
        asset_id: str,
        window_start: datetime,
        window_end: datetime,
        identity: SelfIdentity,
    ) -> dict[str, Any]:
        rows = self.client.fetch_window(
            asset_id=str(asset_id),
            start=window_start,
            end=window_end,
        )
        result = filter_self_evidence(rows, identity)
        return {
            **result,
            "asset_id": str(asset_id),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "used_for_actual_live_outcome": False,
            "source": "clickhouse_orderfilled_delayed",
        }
