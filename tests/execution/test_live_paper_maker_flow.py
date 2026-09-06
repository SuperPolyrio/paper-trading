import asyncio
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

from quant.execution.models.maker_queue import MakerQueueState
from quant.orderbook.local_event_bus import filter_execution_messages
from quant.orderbook.polymarket_adapter import (
    NormalizedTradeEvent,
    normalize_polymarket_trade,
)
from quant.paper.live_shadow_service import (
    LivePaperShadowService,
    LiveShadowStats,
    _execution_result_from_payload,
    _maker_execution_result,
    _trade_event_signature,
)
from quant.paper.live_shadow_store import MakerQueueAdvancePlan
from quant.paper.taker_execution import (
    OrderIntent,
    TakerExecutionConfig,
    TakerOnlyPaperExecutionEngine,
)

NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)


def _intent(*, fee_taker_only: bool = True) -> OrderIntent:
    return OrderIntent(
        strategy_id="maker-test",
        market_id="m1",
        condition_id="c1",
        asset_id="a1",
        side="BUY",
        order_type="GTC",
        limit_price=Decimal("0.40"),
        size=Decimal("2"),
        post_only=True,
        decision_ts=NOW,
        client_order_id="maker-1",
        amount_unit="SHARES",
        fee_rate=Decimal("0.02"),
        fee_exponent=Decimal("1"),
        fee_taker_only=fee_taker_only,
    )


def _plan(*, fee_taker_only: bool = True) -> MakerQueueAdvancePlan:
    intent = _intent(fee_taker_only=fee_taker_only)
    state = MakerQueueState(
        paper_order_id="7",
        asset_id=intent.asset_id,
        side=intent.side,
        price_tick=intent.limit_price,
        queue_model_version="paper_maker_queue_strict_ws_trade_v1",
        displayed_size_at_accept=Decimal("10"),
        own_orders_ahead=Decimal("0"),
        estimated_external_queue_ahead=Decimal("10"),
        order_size=intent.size,
        cumulative_trade_volume_at_price=Decimal("11"),
        last_event_id="trade-1",
    )
    return MakerQueueAdvancePlan(
        intent_id=7,
        intent=intent,
        event_id="trade-1",
        event_ts=NOW,
        prior_last_event_id="",
        prior_last_event_ts=None,
        prior_queue_epoch=0,
        next_state=state,
        incremental_fill_size=Decimal("1"),
        cumulative_filled_size=Decimal("1"),
        remaining_size=Decimal("1"),
        arrival_checkpoint_id="cp-1",
        coverage_grade="A",
        book_generation=12,
        fidelity={"fidelity_level": "F4"},
    )


def test_last_trade_price_normalizes_and_has_stable_cross_feed_identity() -> None:
    payload = {
        "event_type": "last_trade_price",
        "asset_id": "a1",
        "price": "0.40",
        "size": "3.5",
        "side": "SELL",
        "timestamp": "1785897600000",
        "transaction_hash": "0xabc",
    }
    left = normalize_polymarket_trade(payload)
    right = normalize_polymarket_trade(dict(payload))
    assert left is not None and right is not None
    assert left.aggressor_side == "SELL"
    assert left.size == Decimal("3.5")
    assert _trade_event_signature(left) == _trade_event_signature(right)


def test_execution_event_filter_keeps_last_trade_price() -> None:
    payload = {
        "event_type": "last_trade_price",
        "asset_id": "a1",
        "price": "0.40",
        "size": "3.5",
        "side": "SELL",
        "timestamp": "1785897600000",
    }

    assert filter_execution_messages([payload], {"a1"}) == (payload,)
    assert filter_execution_messages([payload], {"a2"}) == ()


def test_invalid_trade_is_not_maker_fill_evidence() -> None:
    assert normalize_polymarket_trade({"event_type": "price_change"}) is None
    assert normalize_polymarket_trade(
        {
            "event_type": "last_trade_price",
            "asset_id": "a1",
            "price": "1",
            "size": "1",
            "side": "BUY",
        }
    ) is None


def test_maker_result_is_incremental_auditable_and_uncalibrated() -> None:
    result = _maker_execution_result(
        _plan(),
        price=Decimal("0.40"),
        config=TakerExecutionConfig(),
    )
    assert result.status == "PARTIAL"
    assert result.filled_size == Decimal("1")
    assert result.remaining_size == Decimal("1")
    assert result.total_fee == 0
    assert result.fidelity["maker_fill_evidence"] == "last_trade_price"
    assert result.fidelity["maker_calibrated_in_domain"] is False


def test_durable_maker_result_round_trips_for_accounting_recovery() -> None:
    original = _maker_execution_result(
        _plan(fee_taker_only=False),
        price=Decimal("0.40"),
        config=TakerExecutionConfig(),
    )

    recovered = _execution_result_from_payload(original.as_dict())

    assert recovered == original


def test_maker_fee_only_applies_when_market_terms_charge_makers() -> None:
    result = _maker_execution_result(
        _plan(fee_taker_only=False),
        price=Decimal("0.40"),
        config=TakerExecutionConfig(),
    )
    assert result.total_fee == Decimal("0.00480")


def test_maker_fill_is_bound_to_intent_before_automatic_finality() -> None:
    observed: list[tuple[str, int | str]] = []

    class _Store:
        def plan_maker_trade(self, **_kwargs):
            return (_plan(),)

        def commit_maker_trade(self, _plan, _result):
            observed.append(("commit", _plan.intent_id))
            return True

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service._maker_trades = deque(
            [
                NormalizedTradeEvent(
                    token_id="a1",
                    price=Decimal("0.40"),
                    size=Decimal("11"),
                    aggressor_side="SELL",
                    event_ts_ms=int(NOW.timestamp() * 1000),
                    transaction_hash="0xmaker",
                )
            ]
        )
        service.store = _Store()
        service.engine = TakerOnlyPaperExecutionEngine()
        service.portfolio_store = None
        service.own_order_oms_gate = None
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0
        async def apply_side_effects(intent_id, result):
            observed.append(("account", intent_id))
            assert result.filled_size == Decimal("1")

        service._apply_committed_result_side_effects = apply_side_effects

        await service._advance_maker_trades()

        assert observed == [("commit", 7), ("account", 7)]
        assert service.stats.maker_fills == 1
        assert service.stats.maker_errors == 0

    asyncio.run(exercise())


def test_rejected_maker_commit_never_reaches_accounting() -> None:
    observed: list[str] = []

    class _Store:
        def plan_maker_trade(self, **_kwargs):
            return (_plan(),)

        def commit_maker_trade(self, _plan, _result):
            observed.append("commit_rejected")
            return False

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service._maker_trades = deque(
            [
                NormalizedTradeEvent(
                    token_id="a1",
                    price=Decimal("0.40"),
                    size=Decimal("11"),
                    aggressor_side="SELL",
                    event_ts_ms=int(NOW.timestamp() * 1000),
                    transaction_hash="0xmaker-rejected",
                )
            ]
        )
        service.store = _Store()
        service.engine = TakerOnlyPaperExecutionEngine()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        async def unexpected_accounting(_intent_id, _result):
            observed.append("accounting")

        service._apply_committed_result_side_effects = unexpected_accounting

        await service._advance_maker_trades()

        assert observed == ["commit_rejected"]
        assert service.stats.maker_fills == 0
        assert service.stats.maker_queue_advances == 0

    asyncio.run(exercise())


def test_unapplied_durable_maker_result_is_recovered_idempotently() -> None:
    original = _maker_execution_result(
        _plan(),
        price=Decimal("0.40"),
        config=TakerExecutionConfig(),
    )
    observed: list[tuple[str, int | str]] = []

    class _Store:
        def load_unapplied_results(self, *, limit):
            assert limit == 100
            return [{"intent_id": 7, "result": original.as_dict()}]

    class _Portfolio:
        def reconcile_terminal_reservations(self):
            observed.append(("reservations", 7))

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.store = _Store()
        service.portfolio_store = _Portfolio()
        service.stats = LiveShadowStats(worker_id="worker")
        service._last_accounting_reconcile = 0.0
        service.db_operation_timeout_seconds = 1.0

        async def apply(intent_id, result):
            observed.append(("account", intent_id))
            assert result == original

        service._apply_committed_result_side_effects = apply

        await service._reconcile_unapplied_accounting(force=True)

        assert observed == [("account", 7), ("reservations", 7)]
        assert service.stats.accounting_reconciliations == 1
        assert service.stats.accounting_reconciliation_failures == 0

    asyncio.run(exercise())


def test_committed_side_effects_write_accounting_after_retryable_dependencies() -> None:
    result = _maker_execution_result(
        _plan(),
        price=Decimal("0.40"),
        config=TakerExecutionConfig(),
    )
    observed: list[str] = []

    class _Portfolio:
        def finalize_order_reservation(self, *_args):
            observed.append("reservation")

    class _Oms:
        def finalize(self, *_args):
            observed.append("oms")

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.portfolio_store = _Portfolio()
        service.own_order_oms_gate = _Oms()
        service.fill_finality_shadow = object()
        service.fill_finality_auto_reconcile = False
        service.engine = TakerOnlyPaperExecutionEngine()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        async def observe_finality(_intent_id, _result):
            observed.append("finality_record")

        async def account(_intent_id, _result):
            observed.append("accounting_marker")

        async def confirm(_result):
            observed.append("finality_confirm")
            return False

        async def complete_evidence(_intent_id, _result):
            observed.append("completion_evidence")

        service._observe_fill_finality = observe_finality
        service._apply_result_accounting = account
        service._confirm_fill_finality = confirm
        service._observe_completed_result = complete_evidence

        await service._apply_committed_result_side_effects(7, result)

        assert observed == [
            "reservation",
            "oms",
            "finality_record",
            "accounting_marker",
            "finality_confirm",
            "completion_evidence",
        ]

    asyncio.run(exercise())


def test_retryable_side_effect_failure_does_not_reach_accounting_marker() -> None:
    result = _maker_execution_result(
        _plan(),
        price=Decimal("0.40"),
        config=TakerExecutionConfig(),
    )
    observed: list[str] = []

    class _Portfolio:
        def finalize_order_reservation(self, *_args):
            observed.append("reservation")

    class _Oms:
        def finalize(self, *_args):
            observed.append("oms_failed")
            raise RuntimeError("oms unavailable")

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.portfolio_store = _Portfolio()
        service.own_order_oms_gate = _Oms()
        service.fill_finality_shadow = None
        service.engine = TakerOnlyPaperExecutionEngine()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        async def account(_intent_id, _result):
            observed.append("accounting_marker")

        service._apply_result_accounting = account

        try:
            await service._apply_committed_result_side_effects(7, result)
        except RuntimeError as exc:
            assert "oms unavailable" in str(exc)
        else:
            raise AssertionError("expected retryable OMS failure")

        assert observed == ["reservation", "oms_failed"]

    asyncio.run(exercise())
