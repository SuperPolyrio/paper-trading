"""Event-resolution payout scenarios for portfolio risk, not price forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


@dataclass(frozen=True)
class OutcomeScenario:
    scenario_id: str
    event_id: str
    payout_by_asset: Mapping[str, Decimal]
    condition_id: str = ""

    def payout_for(self, asset_id: str) -> Decimal:
        return Decimal(self.payout_by_asset.get(str(asset_id), Decimal("0")))
