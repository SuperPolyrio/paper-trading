from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant.simulator.multileg import (
    AtomicityPolicy,
    ExecutionLeg,
    LegState,
    MultiLegExecutionPlan,
    PlanState,
    RfqLifecycle,
    RfqState,
)

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_non_atomic_first_leg_fill_exposes_hedge_risk_instead_of_claiming_atomicity() -> (
    None
):
    plan = MultiLegExecutionPlan(
        "plan:one",
        AtomicityPolicy.PARALLEL_NON_ATOMIC,
        (
            ExecutionLeg("leg:a", "asset:a", "BUY", Decimal("2"), Decimal("0.6")),
            ExecutionLeg("leg:b", "asset:b", "SELL", Decimal("2"), Decimal("0.4")),
        ),
        timedelta(seconds=2),
        NOW,
    )

    partial = plan.record_leg("leg:a", LegState.FILLED)

    assert partial.state is PlanState.HEDGE_REQUIRED
    assert partial.remaining_leg_exposure == Decimal("0.8")
    assert partial.worst_case_leg_loss == Decimal("2.0")


def test_partial_leg_fill_uses_only_remaining_quantity_for_residual_exposure() -> None:
    plan = MultiLegExecutionPlan(
        "plan:partial",
        AtomicityPolicy.PARALLEL_NON_ATOMIC,
        (
            ExecutionLeg("leg:a", "asset:a", "BUY", Decimal("2"), Decimal("0.6")),
            ExecutionLeg("leg:b", "asset:b", "SELL", Decimal("2"), Decimal("0.4")),
        ),
        timedelta(seconds=2),
        NOW,
    )

    partial = plan.record_leg_fill(
        "leg:a",
        filled_size=Decimal("0.5"),
        average_fill_price=Decimal("0.48"),
    )

    assert partial.state is PlanState.HEDGE_REQUIRED
    assert partial.legs[0].state is LegState.PARTIAL
    assert partial.legs[0].remaining_size == Decimal("1.5")
    assert partial.remaining_leg_exposure == Decimal("1.7")


def test_combo_rfq_honors_quote_accept_and_last_look_windows() -> None:
    rfq = RfqLifecycle("rfq:one", NOW)
    rfq = rfq.quote(at=NOW + timedelta(milliseconds=300))
    rfq = rfq.accept(at=NOW + timedelta(seconds=5))
    rfq = rfq.last_look(at=NOW + timedelta(seconds=5, milliseconds=500), accepted=True)
    assert rfq.execute().state is RfqState.EXECUTED

    expired = RfqLifecycle("rfq:expired", NOW).quote(
        at=NOW + timedelta(milliseconds=401)
    )
    assert expired.state is RfqState.EXPIRED
