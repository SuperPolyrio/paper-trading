from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from quant.paper.taker_execution import OrderIntent
from quant.simulator.oms.paper_adapter import DurablePaperOmsGate


class _Store:
    def __init__(self) -> None:
        self.calls = []

    def finalize(self, **kwargs):
        self.calls.append(kwargs)
        return None


def _result(order_type: str):
    intent = OrderIntent(
        strategy_id="strategy",
        market_id="market",
        condition_id="condition",
        asset_id="asset",
        side="BUY",
        order_type=order_type,
        limit_price=Decimal("0.5"),
        size=Decimal(2),
        post_only=False,
        decision_ts=datetime(2026, 9, 3, tzinfo=timezone.utc),
        client_order_id=f"client-{order_type}",
    )
    return SimpleNamespace(
        status="PARTIAL",
        remaining_size=Decimal(1),
        audit_key=f"audit-{order_type}",
        intent=intent,
    )


def test_fak_partial_remainder_is_not_left_working_in_own_order_oms() -> None:
    store = _Store()
    gate = DurablePaperOmsGate(store=store, account_id="paper-account")

    gate.finalize(7, _result("FAK"))

    assert store.calls[0]["execution_status"] == "CANCELED"
    assert store.calls[0]["remaining_size"] == Decimal(1)


def test_gtc_partial_remainder_stays_working_in_own_order_oms() -> None:
    store = _Store()
    gate = DurablePaperOmsGate(store=store, account_id="paper-account")

    gate.finalize(8, _result("GTC"))

    assert store.calls[0]["execution_status"] == "PARTIAL"
