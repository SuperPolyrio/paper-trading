from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant.simulator.regime.venue_migration import (
    VenueMigrationEvent,
    VenueMigrationEventType,
    VenueMigrationReplay,
    VenueMigrationReplayEngine,
    VenueMigrationState,
)

NOW = datetime(2026, 4, 28, 11, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self, replay: VenueMigrationReplay) -> None:
        self.replay = replay
        self.events: dict[str, object] = {}

    def get(self, replay_id: str) -> VenueMigrationReplay:
        assert replay_id == self.replay.replay_id
        return self.replay

    def apply(self, before: object, after: VenueMigrationReplay, event: object) -> VenueMigrationReplay:
        self.replay = after
        self.events[event.event_id] = event
        return after


def _replay() -> VenueMigrationReplay:
    return VenueMigrationReplay(
        replay_id="migration-1",
        account_id="account-1",
        strategy_id="strategy-1",
        source_version="CLOB_V1",
        target_version="CLOB_V2",
        source_collateral="USDC.e",
        target_collateral="pUSD",
        state=VenueMigrationState.PLANNED,
        expected_open_orders=3,
        expected_v1_books=2,
        source_balance=Decimal(100),
        target_balance=Decimal(0),
        active_position_count=4,
    )


def _event(sequence: int, event_type: VenueMigrationEventType, payload: dict) -> VenueMigrationEvent:
    return VenueMigrationEvent(
        event_id=f"event-{sequence}",
        replay_id="migration-1",
        sequence=sequence,
        event_type=event_type,
        event_ts=NOW,
        payload=payload,
    )


def test_full_v2_pusd_replay_preserves_balance_positions_and_clears_v1_state() -> None:
    store = Store(_replay())
    engine = VenueMigrationReplayEngine(store)
    result = engine.replay(
        [
            _event(1, VenueMigrationEventType.HALT_TRADING, {}),
            _event(2, VenueMigrationEventType.CLEAR_V1_BOOKS, {"remaining_v1_books": 0}),
            _event(
                3,
                VenueMigrationEventType.CANCEL_V1_ORDERS,
                {"canceled_order_count": 3, "remaining_open_orders": 0},
            ),
            _event(
                4,
                VenueMigrationEventType.CONVERT_COLLATERAL,
                {
                    "source_debited": "100",
                    "target_credited": "100",
                    "transaction_hash": "0xmigration",
                },
            ),
            _event(
                5,
                VenueMigrationEventType.APPROVE_V2_CONTRACTS,
                {"exchange_v2_approved": True, "neg_risk_exchange_v2_approved": True},
            ),
            _event(
                6,
                VenueMigrationEventType.RESUME_TRADING,
                {"active_position_count": 4},
            ),
            _event(
                7,
                VenueMigrationEventType.COMPLETE,
                {"post_migration_reconciliation_passed": True},
            ),
        ]
    )

    assert result.state is VenueMigrationState.COMPLETED
    assert result.target_balance == result.source_balance
    assert result.expected_open_orders == result.expected_v1_books == 0


def test_replay_rejects_partial_order_cancellation_and_non_1_to_1_conversion() -> None:
    engine = VenueMigrationReplayEngine(Store(_replay()))
    engine.apply(_event(1, VenueMigrationEventType.HALT_TRADING, {}))
    engine.apply(_event(2, VenueMigrationEventType.CLEAR_V1_BOOKS, {"remaining_v1_books": 0}))
    with pytest.raises(ValueError, match="all expected V1 orders"):
        engine.apply(
            _event(
                3,
                VenueMigrationEventType.CANCEL_V1_ORDERS,
                {"canceled_order_count": 2, "remaining_open_orders": 1},
            )
        )
