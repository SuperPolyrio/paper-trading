from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant.execution.models.settlement_finality import (
    ProvisionalTrade,
    SettlementFinalityModel,
)
from quant.paper.live_order_lifecycle import result_transitions
from quant.paper.paper_ledger import (
    _persist_execution_finality,
    _persist_finality_journal,
)
from quant.paper.taker_execution import (
    ArrivalBookCheckpoint,
    OrderIntent,
    PaperBookLevel,
    PaperLatencyModel,
    TakerOnlyPaperExecutionEngine,
)

NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


def _intent(
    *,
    order_type: str = "FOK",
    limit_price: str = "0.5",
    size: str = "2",
) -> OrderIntent:
    return OrderIntent(
        strategy_id="strategy",
        market_id="market",
        condition_id="condition",
        asset_id="asset",
        side="BUY",
        order_type=order_type,
        limit_price=Decimal(limit_price),
        size=Decimal(size),
        post_only=False,
        decision_ts=NOW,
        client_order_id=f"{order_type}-{limit_price}-{size}",
    )


def _checkpoint() -> ArrivalBookCheckpoint:
    return ArrivalBookCheckpoint(
        checkpoint_id="book",
        asset_id="asset",
        market_id="market",
        condition_id="condition",
        observed_at=NOW,
        generation=1,
        coverage_grade="A",
        bids=(PaperBookLevel(Decimal("0.49"), Decimal("10")),),
        asks=(PaperBookLevel(Decimal("0.50"), Decimal("1")),),
    )


def _execute(intent: OrderIntent):
    checkpoint = _checkpoint()
    return TakerOnlyPaperExecutionEngine().execute(
        intent,
        decision_checkpoint=checkpoint,
        arrival_checkpoint=checkpoint,
        arrival_ts_override=NOW,
        intent_sequence=1,
    )


def test_modeled_submit_timestamps_do_not_include_worker_processing_delay() -> None:
    latency = PaperLatencyModel(
        feed_delay_ms=20,
        strategy_delay_ms=30,
        order_delay_ms=100,
    )

    assert latency.submit_request_ts(NOW) == NOW + timedelta(milliseconds=50)
    assert latency.arrival_ts(NOW) == NOW + timedelta(milliseconds=150)


def test_fok_fill_has_provisional_pending_and_confirmed_states() -> None:
    result = _execute(_intent(size="1"))
    assert result.status == "FILLED"
    assert [item.to_state for item in result_transitions(result)] == [
        "MATCHED_PROVISIONAL",
        "SETTLEMENT_PENDING",
        "CONFIRMED",
    ]


def test_fak_no_fill_is_canceled() -> None:
    result = _execute(_intent(order_type="FAK", limit_price="0.49"))
    assert result.status == "CANCELLED"
    assert [item.to_state for item in result_transitions(result)] == ["CANCELED"]


def test_gtc_partial_fill_returns_to_working_after_confirmation() -> None:
    result = _execute(_intent(order_type="GTC", size="2"))
    assert result.status == "PARTIAL"
    assert result.remaining_size == Decimal("1")
    assert [item.to_state for item in result_transitions(result)] == [
        "PARTIALLY_MATCHED_PROVISIONAL",
        "SETTLEMENT_PENDING",
        "CONFIRMED",
        "WORKING",
    ]


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.batches: list[list[tuple[object, ...]]] = []

    def execute(self, sql: str, params) -> None:
        self.calls.append((sql, tuple(params)))

    def executemany(self, _sql: str, rows) -> None:
        self.batches.append(list(rows))


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_persisted_finality_journals_are_double_entry_balanced(side: str) -> None:
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
    _, confirmation = model.confirm(pending, confirmed_at=NOW)
    cursor = _RecordingCursor()
    for journal in (provisional, confirmation):
        _persist_finality_journal(
            cursor,
            strategy_id="strategy",
            event_ts=NOW,
            journal=journal,
            metadata={"trade_id": trade.trade_id},
        )

    assert len(cursor.batches) == 2
    for lines in cursor.batches:
        assert sum((line[5] for line in lines), Decimal("0")) == sum(
            (line[6] for line in lines),
            Decimal("0"),
        )


def test_failed_finality_is_persisted_with_balanced_reversal() -> None:
    result = _execute(_intent(size="1"))
    cursor = _RecordingCursor()
    _persist_execution_finality(cursor, result, outcome="FAILED_REVERSED")

    finality_rows = [
        params
        for sql, params in cursor.calls
        if "INSERT INTO quant.paper_execution_finality (" in sql
    ]
    event_rows = [
        params
        for sql, params in cursor.calls
        if "INSERT INTO quant.paper_execution_finality_events" in sql
    ]
    assert finality_rows[0][3] == "FAILED_REVERSED"
    assert finality_rows[0][5] == Decimal("0")
    assert [row[3] for row in event_rows] == [
        "MATCHED_PROVISIONAL",
        "SETTLEMENT_PENDING",
        "FAILED_REVERSED",
    ]
    assert len(cursor.batches) == 2
    all_lines = [line for batch in cursor.batches for line in batch]
    by_account: dict[str, Decimal] = {}
    for line in all_lines:
        by_account[str(line[4])] = (
            by_account.get(str(line[4]), Decimal("0"))
            + Decimal(line[5])
            - Decimal(line[6])
        )
    assert all(value == 0 for value in by_account.values())
