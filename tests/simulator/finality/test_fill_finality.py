from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant.simulator.finality import FillFinalityLedger, FillFinalityState, FillFragment

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _fragment(name: str, *, size: str = "2", side: str = "BUY") -> FillFragment:
    return FillFragment(
        trade_id=f"trade:{name}",
        account_id="account:one",
        strategy_id="strategy:one",
        asset_id="asset:one",
        side=side,
        size=Decimal(size),
        price=Decimal("0.4"),
        fee=Decimal("0.01"),
        matched_at=NOW,
    )


def test_matched_retry_confirm_is_idempotent_and_only_confirmed_nav_counts_performance() -> None:
    ledger = FillFinalityLedger()
    fragment = _fragment("one")
    ledger.record_match(fragment, event_id="match")

    assert ledger.provisional_nav().position_by_asset == {"asset:one": Decimal("2")}
    assert ledger.confirmed_nav().position_by_asset == {}
    ledger.mark_retrying(fragment.trade_id, event_id="retry", event_ts=NOW)
    confirmed = ledger.confirm(fragment.trade_id, event_id="confirm", event_ts=NOW)
    assert confirmed.state is FillFinalityState.CONFIRMED_FINAL
    assert ledger.confirm(fragment.trade_id, event_id="confirm-again", event_ts=NOW) == confirmed
    assert ledger.confirmed_nav().position_by_asset == {"asset:one": Decimal("2")}
    assert len(ledger.journal()) == 3


def test_failed_fragment_gets_explicit_void_and_compensation_without_opposite_fill() -> None:
    ledger = FillFinalityLedger()
    fragment = _fragment("failed")
    ledger.record_match(fragment, event_id="match")
    failed = ledger.fail_and_void(fragment.trade_id, event_id="failed", event_ts=NOW, reason="venue_failed")

    assert failed.state is FillFinalityState.REVERSAL_APPLIED
    assert ledger.provisional_nav().position_by_asset == {}
    assert ledger.confirmed_nav().position_by_asset == {}
    assert [entry.event_type for entry in ledger.journal()] == [
        "PROVISIONAL_FILL",
        "FINALITY_FAILED",
        "FILL_VOIDED",
        "CASH_REVERSAL",
        "POSITION_REVERSAL",
        "FEE_REVERSAL",
    ]
    assert sum((entry.cash_delta for entry in ledger.journal()), Decimal("0")) == Decimal("0")
    assert sum((entry.shares_delta for entry in ledger.journal()), Decimal("0")) == Decimal("0")
    assert sum((entry.fee_delta for entry in ledger.journal()), Decimal("0")) == Decimal("0")


def test_voiding_one_partial_fragment_does_not_revoke_confirmed_sibling() -> None:
    ledger = FillFinalityLedger()
    first, second = _fragment("first", size="1"), _fragment("second", size="1")
    ledger.record_match(first, event_id="match:first")
    ledger.record_match(second, event_id="match:second")
    ledger.confirm(first.trade_id, event_id="confirm:first", event_ts=NOW)
    ledger.fail_and_void(second.trade_id, event_id="void:second", event_ts=NOW, reason="fragment_void")

    assert ledger.trade(first.trade_id).state is FillFinalityState.CONFIRMED_FINAL
    assert ledger.trade(second.trade_id).state is FillFinalityState.REVERSAL_APPLIED
    assert ledger.confirmed_nav().position_by_asset == {"asset:one": Decimal("1")}
    assert ledger.provisional_nav().position_by_asset == {"asset:one": Decimal("1")}


def test_restart_snapshot_preserves_void_idempotency() -> None:
    ledger = FillFinalityLedger()
    fragment = _fragment("restart")
    ledger.record_match(fragment, event_id="match")
    voided = ledger.fail_and_void(fragment.trade_id, event_id="void", event_ts=NOW, reason="failure")
    restarted = FillFinalityLedger.from_snapshot(ledger.snapshot())

    assert restarted.fail_and_void(fragment.trade_id, event_id="void-again", event_ts=NOW, reason="failure") == voided
    assert len(restarted.journal()) == len(ledger.journal())
    with pytest.raises(ValueError, match="not confirmable"):
        restarted.confirm(fragment.trade_id, event_id="late-confirm", event_ts=NOW)
