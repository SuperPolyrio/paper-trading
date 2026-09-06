"""Deterministic NAV views reconstructed from fragment-level finality states."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from .finality_model import FillFinalityState, FinalityTrade


@dataclass(frozen=True)
class FinalityNav:
    cash_delta: Decimal
    position_by_asset: dict[str, Decimal]
    fee_delta: Decimal


def rebuild_nav(
    trades: Iterable[FinalityTrade], *, confirmed_only: bool
) -> FinalityNav:
    cash = Decimal(0)
    fees = Decimal(0)
    positions: dict[str, Decimal] = {}
    eligible = (
        {FillFinalityState.CONFIRMED_FINAL}
        if confirmed_only
        else {
            FillFinalityState.MATCHED_PROVISIONAL,
            FillFinalityState.RETRYING,
            FillFinalityState.CONFIRMED_FINAL,
        }
    )
    for trade in trades:
        if trade.state not in eligible:
            continue
        fragment = trade.fragment
        cash += fragment.signed_cash
        fees += fragment.fee
        positions[fragment.asset_id] = (
            positions.get(fragment.asset_id, Decimal(0)) + fragment.signed_shares
        )
    return FinalityNav(cash, positions, fees)
