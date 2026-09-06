from decimal import Decimal
from types import SimpleNamespace

from quant.risk.event_risk import ExitLevel
from quant.simulator.analytics import (
    NavPositionInput,
    TcaInput,
    build_nav_snapshot,
    build_paper_tca_artifact,
    build_tca,
)


def test_nav_keeps_uncovered_quantity_out_of_bbo_liquidation_value() -> None:
    snapshot = build_nav_snapshot(
        accounting_cash=Decimal("5"),
        positions=(
            NavPositionInput(
                "asset",
                Decimal("5"),
                Decimal("0.6"),
                (
                    ExitLevel(Decimal("0.4"), Decimal("2")),
                    ExitLevel(Decimal("0.3"), Decimal("1")),
                ),
                expected_payout=Decimal("1"),
                worst_case_payout=Decimal("0"),
                provisional_value=Decimal("2"),
                confirmed_value=Decimal("1"),
            ),
        ),
    )

    assert snapshot.mid_mark_nav == Decimal("8")
    assert snapshot.bbo_liquidation_nav is None
    assert snapshot.walk_book_liquidation_nav == Decimal("6.1")
    assert snapshot.resolution_expected_nav == Decimal("10")
    assert snapshot.worst_case_resolution_nav == Decimal("5")
    assert snapshot.unliquidated_quantity == Decimal("2")
    assert snapshot.mark_status == "UNLIQUID_UNMARKED"


def test_accounting_nav_includes_position_cost_basis() -> None:
    snapshot = build_nav_snapshot(
        accounting_cash=Decimal("5"),
        positions=(
            NavPositionInput(
                "asset",
                Decimal("2"),
                Decimal("0.5"),
                (ExitLevel(Decimal("0.4"), Decimal("2")),),
                accounting_value=Decimal("1.2"),
            ),
        ),
    )

    assert snapshot.accounting_nav == Decimal("6.2")
    assert snapshot.walk_book_liquidation_nav == Decimal("5.8")
    assert snapshot.as_dict()["valuation_model_version"] == "paper-multiview-nav-v1"


def test_tca_decomposes_delay_walk_fee_and_unfilled_opportunity_cost() -> None:
    report = build_tca(
        TcaInput(
            "order",
            "BUY",
            Decimal("10"),
            Decimal("6"),
            Decimal("0.4"),
            Decimal("0.42"),
            Decimal("0.45"),
            Decimal("0.01"),
            Decimal("0.5"),
            {"1s": Decimal("-0.02")},
        )
    )

    assert report.delay_cost == Decimal("0.12")
    assert report.book_walk_cost == Decimal("0.18")
    assert report.fee_cost == Decimal("0.01")
    assert report.opportunity_cost == Decimal("0.40")
    assert report.implementation_shortfall == Decimal("0.71")
    assert report.adverse_selection_markouts == {"1s": Decimal("-0.02")}


def test_paper_tca_uses_checkpoint_bbo_and_never_limit_price_fallback() -> None:
    intent = SimpleNamespace(
        client_order_id="order",
        strategy_id="strategy",
        side="BUY",
        amount_unit="SHARES",
        limit_price=Decimal("0.9"),
    )
    result = SimpleNamespace(
        intent=intent,
        requested_amount=Decimal("2"),
        amount_unit="SHARES",
        filled_size=Decimal("2"),
        avg_fill_price=Decimal("0.52"),
        total_fee=Decimal("0.01"),
        audit_key="audit",
        fidelity={"fidelity_level": "F2_VENUE_EMULATED"},
    )
    decision = SimpleNamespace(capacity={"status": "IN_DOMAIN_CALIBRATED"})
    decision_book = SimpleNamespace(
        checkpoint_id="decision",
        asks=(SimpleNamespace(price=Decimal("0.50"), size=Decimal("10")),),
        bids=(),
    )
    arrival_book = SimpleNamespace(
        checkpoint_id="arrival",
        asks=(SimpleNamespace(price=Decimal("0.51"), size=Decimal("10")),),
        bids=(),
    )

    artifact = build_paper_tca_artifact(
        intent_id=7,
        result=result,
        decision_checkpoint=decision_book,
        arrival_checkpoint=arrival_book,
        risk_decision=decision,
    )
    unavailable = build_paper_tca_artifact(
        intent_id=8,
        result=result,
        decision_checkpoint=None,
        arrival_checkpoint=None,
        risk_decision=decision,
    )

    assert artifact.status == "IMMEDIATE_COMPLETE"
    assert artifact.decision_price == Decimal("0.50")
    assert artifact.arrival_price == Decimal("0.51")
    assert artifact.report is not None
    assert artifact.report.delay_cost == Decimal("0.02")
    assert artifact.report.book_walk_cost == Decimal("0.02")
    assert unavailable.status == "UNAVAILABLE"
    assert unavailable.decision_price is None
    assert unavailable.arrival_price is None
