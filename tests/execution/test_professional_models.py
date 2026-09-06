from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant.execution.accounting_journal import (
    IndependentJournal,
    JournalGroup,
    JournalLine,
    transfer,
)
from quant.execution.backpressure import BackpressureSnapshot, evaluate_backpressure
from quant.execution.models.fee_rebate import (
    RebateRecord,
    RebateState,
    transition_rebate,
)
from quant.execution.models.maker_queue import (
    MakerQueueEngine,
    MakerQueueState,
    QueueModel,
    poisson_arrival_probability,
)
from quant.execution.models.marks import MarkInput, MarkQuality, build_marks
from quant.risk.event_exposure import (
    ExposureLimits,
    ExposurePosition,
    evaluate_exposure,
)

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


def test_rebate_is_not_cash_until_received() -> None:
    estimated = RebateRecord(
        "r1", "MAKER", "v1", Decimal("1"), RebateState.ESTIMATED, NOW
    )
    assert estimated.confirmed_cash == 0
    accrued = transition_rebate(estimated, RebateState.ACCRUED, at=NOW)
    assert accrued.confirmed_cash == 0
    assert transition_rebate(accrued, RebateState.RECEIVED, at=NOW).confirmed_cash == 1


def test_mark_hierarchy_uses_liquidation_side() -> None:
    result = build_marks(
        MarkInput(
            as_of=NOW,
            source_event_at=NOW,
            side="LONG",
            best_bid=Decimal("0.4"),
            best_ask=Decimal("0.5"),
        )
    )
    assert result.mark_quality == MarkQuality.TWO_SIDED_MID
    assert result.research_mark == Decimal("0.45")
    assert result.liquidation_mark == Decimal("0.4")
    assert result.conservative_mark == Decimal("0.4")


def test_journal_is_balanced_and_idempotent() -> None:
    journal = IndependentJournal()
    group = transfer(
        "buy:1",
        "BUY_FILL",
        debit_account="TOKEN_POSITION",
        credit_account="CASH_AVAILABLE",
        amount=Decimal("1"),
    )
    journal.append(group)
    journal.append(group)
    assert (
        journal.verify_materialized(
            {"TOKEN_POSITION": Decimal("1"), "CASH_AVAILABLE": Decimal("-1")}
        )["status"]
        == "PASS"
    )
    with pytest.raises(ValueError):
        JournalGroup(
            "bad",
            "BAD",
            (JournalLine("CASH_AVAILABLE", debit=Decimal("1")),),
        ).assert_balanced()


def _queue() -> MakerQueueState:
    return MakerQueueState(
        paper_order_id="p1",
        asset_id="a",
        side="BUY",
        price_tick=Decimal("0.4"),
        queue_model_version="v1",
        displayed_size_at_accept=Decimal("10"),
        own_orders_ahead=Decimal("1"),
        estimated_external_queue_ahead=Decimal("9"),
        order_size=Decimal("2"),
    )


@pytest.mark.parametrize(
    "model",
    [QueueModel.STRICT_TRADE_EVIDENCE, QueueModel.RISK_AVERSE_QUEUE],
)
def test_book_decrease_does_not_advance_conservative_queue(model: QueueModel) -> None:
    state = MakerQueueEngine(model).on_book_decrease(
        _queue(), decrease=Decimal("10"), event_id="e1"
    )
    assert state.cumulative_cancel_ahead_estimate == 0
    assert (
        MakerQueueEngine(model)
        .predict(
            state, forecast_trade_volume=Decimal("0"), horizon_seconds=Decimal("60")
        )
        .expected_filled_size
        == 0
    )


def test_wrong_aggressor_does_not_advance_queue() -> None:
    state = MakerQueueEngine(QueueModel.STRICT_TRADE_EVIDENCE).on_trade(
        _queue(), aggressor_side="BUY", volume=Decimal("20"), event_id="e1"
    )
    assert state.cumulative_trade_volume_at_price == 0


def test_probability_model_conditions_fill_on_aggressor_arrival() -> None:
    state = MakerQueueState(
        paper_order_id="arrival",
        asset_id="asset",
        side="BUY",
        price_tick=Decimal("0.41"),
        queue_model_version="test",
        displayed_size_at_accept=Decimal(0),
        own_orders_ahead=Decimal(0),
        estimated_external_queue_ahead=Decimal(0),
        order_size=Decimal(5),
    )
    arrival = poisson_arrival_probability(
        observed_count=1,
        lookback_seconds=300,
        horizon_seconds=30,
    )
    prediction = MakerQueueEngine(QueueModel.PROBABILISTIC_QUEUE).predict(
        state,
        forecast_trade_volume=Decimal(10),
        horizon_seconds=Decimal(30),
        aggressor_arrival_probability=arrival,
    )
    strict = MakerQueueEngine(QueueModel.STRICT_TRADE_EVIDENCE).predict(
        state,
        forecast_trade_volume=Decimal(10),
        horizon_seconds=Decimal(30),
        aggressor_arrival_probability=arrival,
    )

    assert Decimal("0") < arrival < Decimal("0.1")
    assert prediction.fill_probability == Decimal("0.95") * arrival
    assert prediction.fill_probability < Decimal("0.1")
    assert strict.fill_probability == 1


def test_strict_trade_queue_consumes_ahead_before_incremental_fill() -> None:
    engine = MakerQueueEngine(QueueModel.STRICT_TRADE_EVIDENCE)
    first = engine.advance_trade(
        _queue(),
        aggressor_side="SELL",
        volume=Decimal("9"),
        event_id="e1",
    )
    assert first.incremental_fill_size == 0
    second = engine.advance_trade(
        first.state,
        aggressor_side="SELL",
        volume=Decimal("2"),
        event_id="e2",
        cumulative_filled_size=first.cumulative_filled_size,
    )
    assert second.incremental_fill_size == Decimal("1")
    third = engine.advance_trade(
        second.state,
        aggressor_side="SELL",
        volume=Decimal("2"),
        event_id="e3",
        cumulative_filled_size=second.cumulative_filled_size,
    )
    assert third.incremental_fill_size == Decimal("1")
    assert third.cumulative_filled_size == Decimal("2")


def test_strict_trade_queue_duplicate_and_wrong_side_never_double_fill() -> None:
    engine = MakerQueueEngine(QueueModel.STRICT_TRADE_EVIDENCE)
    first = engine.advance_trade(
        _queue(),
        aggressor_side="SELL",
        volume=Decimal("11"),
        event_id="e1",
    )
    duplicate = engine.advance_trade(
        first.state,
        aggressor_side="SELL",
        volume=Decimal("11"),
        event_id="e1",
        cumulative_filled_size=first.cumulative_filled_size,
    )
    wrong_side = engine.advance_trade(
        first.state,
        aggressor_side="BUY",
        volume=Decimal("100"),
        event_id="e2",
        cumulative_filled_size=first.cumulative_filled_size,
    )
    assert first.incremental_fill_size == Decimal("1")
    assert duplicate.incremental_fill_size == 0
    assert wrong_side.incremental_fill_size == 0


def test_maker_queue_rebase_starts_new_conservative_epoch() -> None:
    engine = MakerQueueEngine(QueueModel.STRICT_TRADE_EVIDENCE)
    old = engine.advance_trade(
        _queue(),
        aggressor_side="SELL",
        volume=Decimal("9"),
        event_id="old-trade",
        event_ts_ns=100,
    ).state

    rebased = engine.rebase(
        old,
        book_generation=2,
        displayed_external_queue=Decimal("6"),
        event_id="snapshot-2",
        event_ts_ns=200,
    )

    assert rebased.book_generation == 2
    assert rebased.queue_epoch == 1
    assert rebased.estimated_external_queue_ahead == Decimal("6")
    assert rebased.cumulative_trade_volume_at_price == 0
    assert rebased.last_event_ts_ns == 200


def test_maker_queue_ignores_unique_but_late_trade() -> None:
    engine = MakerQueueEngine(QueueModel.STRICT_TRADE_EVIDENCE)
    current = engine.advance_trade(
        _queue(),
        aggressor_side="SELL",
        volume=Decimal("11"),
        event_id="newer",
        event_ts_ns=200,
    ).state

    late = engine.advance_trade(
        current,
        aggressor_side="SELL",
        volume=Decimal("100"),
        event_id="unique-but-late",
        event_ts_ns=199,
        cumulative_filled_size=Decimal("1"),
    )

    assert late.state == current
    assert late.incremental_fill_size == 0
    assert late.cumulative_filled_size == Decimal("1")


def test_maker_queue_accepts_distinct_trades_with_same_timestamp() -> None:
    engine = MakerQueueEngine(QueueModel.STRICT_TRADE_EVIDENCE)
    first = engine.advance_trade(
        _queue(),
        aggressor_side="SELL",
        volume=Decimal("9"),
        event_id="same-ts-one",
        event_ts_ns=200,
    )
    second = engine.advance_trade(
        first.state,
        aggressor_side="SELL",
        volume=Decimal("2"),
        event_id="same-ts-two",
        event_ts_ns=200,
        cumulative_filled_size=first.cumulative_filled_size,
    )

    assert second.incremental_fill_size == Decimal("1")
    assert second.state.last_event_id == "same-ts-two"


def test_backpressure_and_event_exposure_fail_closed() -> None:
    backpressure = evaluate_backpressure(BackpressureSnapshot(0, 0, 0, False, 0, True))
    assert backpressure["fail_closed"] is True
    exposure = evaluate_exposure(
        [ExposurePosition("c", "e", "politics", Decimal("11"))],
        ExposureLimits(
            Decimal("10"),
            Decimal("20"),
            Decimal("20"),
            Decimal("20"),
            Decimal("20"),
            Decimal("20"),
        ),
    )
    assert exposure["status"] == "REJECT"
