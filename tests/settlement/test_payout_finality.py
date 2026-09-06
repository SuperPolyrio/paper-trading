from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant.execution.models.settlement_finality import (
    ProvisionalTrade,
    SettlementFinalityModel,
)
from quant.paper.paper_ledger import PaperPortfolioState, apply_settlement
from quant.settlement.oracle_state import OracleResolutionState, ResolutionPhase
from quant.settlement.payout_vector import PayoutVector

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


def test_unknown_binary_market_pays_half_each() -> None:
    vector = PayoutVector(
        condition_id="condition",
        payouts={"yes": Decimal("0.5"), "no": Decimal("0.5")},
        resolution_source="UMA_UNKNOWN",
        oracle_finalized_at=NOW.isoformat(),
    )
    state = PaperPortfolioState(
        cash_balance=Decimal("5"),
        position_size=Decimal("10"),
        cost_basis=Decimal("4"),
        realized_pnl=Decimal("0"),
    )
    after, payout, realized = apply_settlement(
        state,
        payout_per_share=vector.payout_for("yes"),
    )
    assert payout == Decimal("5")
    assert realized == Decimal("1")
    assert after.cash_balance == Decimal("10")


def test_payout_vector_rejects_invalid_sum() -> None:
    with pytest.raises(ValueError, match="sum"):
        PayoutVector(
            condition_id="condition",
            payouts={"yes": Decimal("1"), "no": Decimal("1")},
            resolution_source="bad",
            oracle_finalized_at=NOW.isoformat(),
        )


def test_failed_provisional_trade_is_reversed() -> None:
    model = SettlementFinalityModel()
    trade = ProvisionalTrade(
        trade_id="trade",
        asset_id="asset",
        side="BUY",
        size=Decimal("2"),
        price=Decimal("0.4"),
        fee=Decimal("0.01"),
        state="MATCHED",
        matched_at=NOW,
    )
    pending, provisional = model.provisional(trade)
    failed, reversal = model.fail_and_reverse(pending, failed_at=NOW)
    assert failed.state == "FAILED_REVERSED"
    assert provisional.trial_balance == 0
    assert reversal.trial_balance == 0
    assert provisional.shares_delta + reversal.shares_delta == 0
    assert provisional.inventory_value_delta + reversal.inventory_value_delta == 0
    assert provisional.receivable_delta + reversal.receivable_delta == 0


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_confirmed_trade_journals_balance(side: str) -> None:
    model = SettlementFinalityModel()
    trade = ProvisionalTrade(
        trade_id=f"trade-{side.lower()}",
        asset_id="asset",
        side=side,
        size=Decimal("2"),
        price=Decimal("0.4"),
        fee=Decimal("0.01"),
        state="MATCHED",
        matched_at=NOW,
    )
    pending, provisional = model.provisional(trade)
    confirmed, confirmation = model.confirm(pending, confirmed_at=NOW)
    assert confirmed.state == "CONFIRMED"
    assert provisional.trial_balance == 0
    assert confirmation.trial_balance == 0


def test_dispute_must_finalize_before_redeemable() -> None:
    state = OracleResolutionState(
        condition_id="condition",
        phase=ResolutionPhase.DISPUTED,
        trading_stopped_at=NOW,
    )
    with pytest.raises(ValueError):
        state.transition(ResolutionPhase.REDEEMABLE, at=NOW)
    state = state.transition(ResolutionPhase.DVM_VOTING, at=NOW)
    state = state.transition(ResolutionPhase.FINALIZED, at=NOW)
    assert (
        state.transition(ResolutionPhase.REDEEMABLE, at=NOW).phase
        == ResolutionPhase.REDEEMABLE
    )
