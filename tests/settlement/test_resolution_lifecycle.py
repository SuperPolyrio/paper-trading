from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant.settlement import OracleResolutionState, PayoutVector, ResolutionExposure, ResolutionPhase, value_resolution_exposure

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _state() -> OracleResolutionState:
    return OracleResolutionState("condition", ResolutionPhase.TRADING_STOPPED, NOW, expected_resolution_at=NOW + timedelta(hours=1))


def _exposure() -> ResolutionExposure:
    return ResolutionExposure(
        "yes",
        Decimal("10"),
        Decimal("0.3"),
        PayoutVector("condition", {"yes": Decimal("1"), "no": Decimal("0")}, "oracle", NOW.isoformat()),
    )


def test_too_early_proposal_is_rejected_then_undisputed_path_becomes_redeemable() -> None:
    state = _state().transition(ResolutionPhase.RESOLUTION_ELIGIBLE, at=NOW)
    with pytest.raises(ValueError, match="before expected"):
        state.transition(ResolutionPhase.PROPOSAL_SUBMITTED, at=NOW)
    state = state.transition(ResolutionPhase.PROPOSAL_SUBMITTED, at=NOW + timedelta(hours=1))
    state = state.transition(ResolutionPhase.CHALLENGE_WINDOW, at=NOW + timedelta(hours=1))
    state = state.transition(ResolutionPhase.RESOLUTION_FINAL, at=NOW + timedelta(hours=3))
    state = state.transition(ResolutionPhase.REDEEMABLE, at=NOW + timedelta(hours=3))

    assert state.proposal_count == 1
    assert state.is_final is True
    assert value_resolution_exposure(state, _exposure()).confirmed_redeemable_payout == Decimal("10")


def test_two_disputes_lead_to_dvm_final_and_failed_redeem_can_retry() -> None:
    state = _state().transition(ResolutionPhase.RESOLUTION_ELIGIBLE, at=NOW)
    state = state.transition(ResolutionPhase.PROPOSAL_SUBMITTED, at=NOW + timedelta(hours=1))
    state = state.transition(ResolutionPhase.CHALLENGE_WINDOW, at=NOW + timedelta(hours=1))
    state = state.transition(ResolutionPhase.DISPUTED_ROUND_1, at=NOW + timedelta(hours=2))
    state = state.transition(ResolutionPhase.SECOND_PROPOSAL, at=NOW + timedelta(hours=3))
    state = state.transition(ResolutionPhase.DISPUTED_ROUND_2, at=NOW + timedelta(hours=4))
    state = state.transition(ResolutionPhase.DVM_VOTING, at=NOW + timedelta(hours=5))
    state = state.transition(ResolutionPhase.RESOLUTION_FINAL, at=NOW + timedelta(hours=6))
    state = state.transition(ResolutionPhase.REDEEMABLE, at=NOW + timedelta(hours=6))
    state = state.transition(ResolutionPhase.REDEEMING, at=NOW + timedelta(hours=7))
    state = state.transition(ResolutionPhase.REDEEMABLE, at=NOW + timedelta(hours=7, minutes=1))
    state = state.transition(ResolutionPhase.REDEEMING, at=NOW + timedelta(hours=8))
    state = state.transition(ResolutionPhase.REDEEMED, at=NOW + timedelta(hours=8, minutes=1))

    assert state.dispute_round == 2
    assert state.proposal_count == 2
    assert state.redeemed_at == NOW + timedelta(hours=8, minutes=1)
    assert state.capital_locked_seconds(now=NOW + timedelta(days=1)) == 8 * 3600 + 60
    assert state.is_capital_locked is False


def test_disputed_capital_keeps_optimistic_and_stressed_views_separate() -> None:
    state = _state().transition(ResolutionPhase.RESOLUTION_ELIGIBLE, at=NOW)
    state = state.transition(ResolutionPhase.PROPOSAL_SUBMITTED, at=NOW + timedelta(hours=1))
    state = state.transition(ResolutionPhase.DISPUTED_ROUND_1, at=NOW + timedelta(hours=2))

    value = value_resolution_exposure(state, _exposure(), dispute_haircut=Decimal("0.5"))

    assert value.optimistic_resolution_value == Decimal("10")
    assert value.liquidation_value == Decimal("3")
    assert value.stressed_dispute_value == Decimal("3")
    assert value.confirmed_redeemable_payout == Decimal("0")
    assert value.capital_locked is True
