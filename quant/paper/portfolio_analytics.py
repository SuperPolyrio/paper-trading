"""Canonical mark-to-market and paper portfolio NAV calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant.execution.models.marks import MarkResult
from quant.risk.event_risk import ExitLevel


@dataclass(frozen=True)
class PaperAssetMark:
    asset_id: str
    observed_at: datetime
    result: MarkResult
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    checkpoint_id: str | None = None
    exit_levels: tuple[ExitLevel, ...] = ()


@dataclass(frozen=True)
class PaperNavPosition:
    asset_id: str
    quantity: Decimal
    cost_basis: Decimal


@dataclass(frozen=True)
class PaperNavResult:
    position_market_value: Decimal | None
    conservative_position_value: Decimal
    equity: Decimal | None
    conservative_equity: Decimal
    unrealized_pnl: Decimal | None
    conservative_unrealized_pnl: Decimal
    total_pnl: Decimal | None
    conservative_total_pnl: Decimal
    gross_exposure: Decimal
    high_watermark: Decimal
    drawdown: Decimal
    drawdown_pct: Decimal
    open_positions: int
    unmarkable_positions: int
    nav_complete: bool


def calculate_portfolio_nav(
    *,
    initial_cash: Decimal,
    cash_balance: Decimal,
    positions: list[PaperNavPosition],
    marks: dict[str, PaperAssetMark],
    previous_high_watermark: Decimal,
) -> PaperNavResult:
    research_value = Decimal(0)
    conservative_value = Decimal(0)
    cost_basis = Decimal(0)
    unmarkable = 0
    for position in positions:
        if position.quantity <= 0:
            continue
        cost_basis += position.cost_basis
        mark = marks.get(position.asset_id)
        if mark is None or mark.result.research_mark is None:
            unmarkable += 1
        else:
            research_value += position.quantity * mark.result.research_mark
        conservative_mark = mark.result.conservative_mark if mark is not None else None
        if conservative_mark is None and mark is not None:
            conservative_mark = mark.best_bid
        if conservative_mark is not None:
            conservative_value += position.quantity * conservative_mark
    complete = unmarkable == 0
    equity = cash_balance + research_value if complete else None
    conservative_equity = cash_balance + conservative_value
    high_watermark = max(
        initial_cash,
        previous_high_watermark,
        conservative_equity,
    )
    drawdown = max(Decimal(0), high_watermark - conservative_equity)
    return PaperNavResult(
        position_market_value=research_value if complete else None,
        conservative_position_value=conservative_value,
        equity=equity,
        conservative_equity=conservative_equity,
        unrealized_pnl=(research_value - cost_basis) if complete else None,
        conservative_unrealized_pnl=conservative_value - cost_basis,
        total_pnl=(equity - initial_cash) if equity is not None else None,
        conservative_total_pnl=conservative_equity - initial_cash,
        gross_exposure=cost_basis,
        high_watermark=high_watermark,
        drawdown=drawdown,
        drawdown_pct=(drawdown / high_watermark if high_watermark > 0 else Decimal(0)),
        open_positions=sum(1 for item in positions if item.quantity > 0),
        unmarkable_positions=unmarkable,
        nav_complete=complete,
    )
