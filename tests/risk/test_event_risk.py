from decimal import Decimal

from quant.risk.event_risk import (
    EventRiskAdmissionInput,
    EventRiskLimits,
    EventRiskPosition,
    ExitLevel,
    OutcomeScenario,
    evaluate_event_risk,
    evaluate_projected_order_risk,
    walk_exit_book,
)


def test_event_scenarios_capture_worst_case_and_50_50_outcome_without_mid_mark_claim() -> (
    None
):
    positions = (
        EventRiskPosition(
            "event",
            "politics",
            "strategy:a",
            "yes",
            Decimal("10"),
            Decimal("6"),
            Decimal("3"),
            Decimal("6"),
        ),
        EventRiskPosition(
            "event",
            "politics",
            "strategy:b",
            "no",
            Decimal("10"),
            Decimal("3"),
            None,
            Decimal("0"),
        ),
    )
    scenarios = (
        OutcomeScenario("yes-wins", "event", {"yes": Decimal("1"), "no": Decimal("0")}),
        OutcomeScenario("no-wins", "event", {"yes": Decimal("0"), "no": Decimal("1")}),
        OutcomeScenario(
            "unknown", "event", {"yes": Decimal("0.5"), "no": Decimal("0.5")}
        ),
    )
    report = evaluate_event_risk(
        positions,
        scenarios,
        EventRiskLimits(
            Decimal("2"), Decimal("2"), Decimal("2"), Decimal("5"), Decimal("10")
        ),
    )

    assert report["status"] == "REJECT"
    assert report["max_loss_at_resolution"] == Decimal("1")
    assert report["illiquid_position_value"] == Decimal("3")
    assert "illiquid_position_value" in report["breaches"]
    assert "dispute_locked_capital" in report["breaches"]


def test_walk_book_marks_uncovered_quantity_unliquid_and_never_prices_it_at_best_bid() -> (
    None
):
    result = walk_exit_book(
        Decimal("5"),
        (
            ExitLevel(Decimal("0.4"), Decimal("2")),
            ExitLevel(Decimal("0.3"), Decimal("1")),
        ),
    )

    assert result.executable_quantity == Decimal("3")
    assert result.executable_value == Decimal("1.1")
    assert result.unliquidated_quantity == Decimal("2")
    assert result.status == "UNLIQUID_UNMARKED"


def test_independent_conditions_sum_their_worst_cases_at_event_level() -> None:
    positions = (
        EventRiskPosition(
            "event",
            "politics",
            "strategy",
            "a-yes",
            Decimal("10"),
            Decimal("7"),
            Decimal("4"),
            condition_id="condition-a",
        ),
        EventRiskPosition(
            "event",
            "politics",
            "strategy",
            "b-yes",
            Decimal("10"),
            Decimal("6"),
            Decimal("4"),
            condition_id="condition-b",
        ),
    )
    scenarios = (
        OutcomeScenario("a-yes", "event", {"a-yes": Decimal(1)}, "condition-a"),
        OutcomeScenario("a-no", "event", {"a-yes": Decimal(0)}, "condition-a"),
        OutcomeScenario("b-yes", "event", {"b-yes": Decimal(1)}, "condition-b"),
        OutcomeScenario("b-no", "event", {"b-yes": Decimal(0)}, "condition-b"),
    )

    report = evaluate_event_risk(
        positions,
        scenarios,
        EventRiskLimits(
            Decimal("12"),
            Decimal("20"),
            Decimal("20"),
            Decimal("20"),
            Decimal("20"),
        ),
    )

    assert report["event_worst_case"] == {"event": Decimal("-13")}
    assert report["max_loss_at_resolution"] == Decimal("-13")
    assert "event:event" in report["breaches"]


def test_projected_buy_uses_executable_bid_depth_and_persists_inputs() -> None:
    class Intent:
        side = "BUY"
        amount_unit = "SHARES"
        size = Decimal("2")
        limit_price = Decimal("0.5")
        strategy_id = "strategy"
        asset_id = "yes"
        condition_id = "condition"

    class Level:
        def __init__(self, price: str, size: str) -> None:
            self.price = Decimal(price)
            self.size = Decimal(size)

    class Checkpoint:
        bids = (Level("0.4", "2"),)

    report = evaluate_projected_order_risk(
        EventRiskAdmissionInput(
            positions=(),
            scenarios=(
                OutcomeScenario(
                    "yes-wins",
                    "event",
                    {"yes": Decimal(1), "no": Decimal(0)},
                    "condition",
                ),
                OutcomeScenario(
                    "no-wins",
                    "event",
                    {"yes": Decimal(0), "no": Decimal(1)},
                    "condition",
                ),
            ),
        ),
        intent=Intent(),
        arrival_checkpoint=Checkpoint(),
        event_id="event",
        category="politics",
        limits=EventRiskLimits(
            Decimal("2"),
            Decimal("2"),
            Decimal("2"),
            Decimal("2"),
            Decimal("2"),
        ),
    )

    assert report["status"] == "PASS"
    assert report["projected_positions"][0]["current_liquidation_value"] == "0.8"
    assert report["input"]["model_version"] == "paper-event-risk-v1"
