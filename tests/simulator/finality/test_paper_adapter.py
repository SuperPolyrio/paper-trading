from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from quant.simulator.finality.finality_model import (
    FillFinalityState,
    FinalityReconciliationCandidate,
    FinalityTrade,
)
from quant.simulator.finality.paper_adapter import DurablePaperFillFinality

NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


class _Store:
    def __init__(self) -> None:
        self.trades = {}
        self.intent_ids = {}
        self.candidates = ()

    def record_match(
        self,
        fragment,
        *,
        event_id,
        audit_key,
        fill_index,
        paper_intent_id,
    ):
        del event_id, audit_key, fill_index
        trade = self.trades.setdefault(
            fragment.trade_id,
            FinalityTrade(fragment, FillFinalityState.MATCHED_PROVISIONAL),
        )
        self.intent_ids[fragment.trade_id] = paper_intent_id
        return trade

    def confirm(self, trade_id, *, event_id, event_ts):
        del event_id
        current = self.trades[trade_id]
        trade = FinalityTrade(
            current.fragment,
            FillFinalityState.CONFIRMED_FINAL,
            event_ts,
        )
        self.trades[trade_id] = trade
        return trade

    def fail_and_void(self, trade_id, *, event_id, event_ts, reason):
        del event_id, reason
        current = self.trades[trade_id]
        trade = FinalityTrade(
            current.fragment,
            FillFinalityState.REVERSAL_APPLIED,
            event_ts,
        )
        self.trades[trade_id] = trade
        return trade

    def pending_lifecycle_reconciliation(self, *, limit):
        assert limit == 1000
        return self.candidates


def _result():
    return SimpleNamespace(
        audit_key="audit:auto",
        arrival_ts=NOW,
        intent=SimpleNamespace(
            strategy_id="strategy",
            asset_id="asset",
            side="BUY",
        ),
        fills=(
            SimpleNamespace(
                size=Decimal(1),
                price=Decimal("0.5"),
                fee=Decimal("0.01"),
            ),
        ),
    )


def test_result_is_bound_to_intent_then_confirmed_idempotently() -> None:
    store = _Store()
    adapter = DurablePaperFillFinality(store)
    result = _result()

    provisional = adapter.record_result(result, paper_intent_id=41)
    confirmed = adapter.reconcile_result(result)

    assert store.intent_ids[provisional[0].fragment.trade_id] == 41
    assert confirmed[0].state is FillFinalityState.CONFIRMED_FINAL


def test_restart_reconciler_uses_durable_lifecycle_candidate() -> None:
    store = _Store()
    adapter = DurablePaperFillFinality(store)
    result = _result()
    trade = adapter.record_result(result, paper_intent_id=42)[0]
    store.candidates = (
        FinalityReconciliationCandidate(
            trade_id=trade.fragment.trade_id,
            outcome="FAILED",
            event_ts=NOW,
            reason="paper_order_lifecycle_failed",
        ),
    )

    reconciled = adapter.reconcile_pending_from_lifecycle()

    assert reconciled == 1
    assert store.trades[trade.fragment.trade_id].state is FillFinalityState.REVERSAL_APPLIED
