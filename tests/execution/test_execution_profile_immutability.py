from datetime import datetime, timezone
from decimal import Decimal

from quant.paper.execution_profile import ExecutionProfileResolver
from quant.paper.live_shadow_service import LivePaperShadowService, PendingIntent
from quant.paper.live_shadow_store import QueuedIntent
from quant.paper.professional_execution import ProfessionalPaperExecutionKernel
from quant.paper.taker_execution import (
    ArrivalBookCheckpoint,
    OrderIntent,
    PaperBookLevel,
    PaperLatencyModel,
    TakerExecutionConfig,
    TakerOnlyPaperExecutionEngine,
)


def test_frozen_profile_reconstructs_config_and_overrides_drifted_kernel() -> None:
    now = datetime.now(timezone.utc)
    intent = OrderIntent(
        strategy_id="s",
        market_id="m",
        condition_id="c",
        asset_id="a",
        side="BUY",
        order_type="FAK",
        limit_price=Decimal("0.60"),
        size=Decimal(2),
        post_only=False,
        decision_ts=now,
        client_order_id="o",
        fee_rate=Decimal(0),
    )
    checkpoint = ArrivalBookCheckpoint(
        checkpoint_id="book-a",
        asset_id="a",
        market_id="m",
        condition_id="c",
        observed_at=now,
        generation=1,
        coverage_grade="A",
        bids=(PaperBookLevel(Decimal("0.49"), Decimal(10)),),
        asks=(PaperBookLevel(Decimal("0.50"), Decimal(10)),),
    )
    original = TakerExecutionConfig(
        latency=PaperLatencyModel(order_delay_ms=0),
        max_book_age_ms=60_000,
        fee_bps=Decimal(0),
        model_version="original",
    )
    drifted = TakerExecutionConfig(
        latency=PaperLatencyModel(order_delay_ms=999),
        max_book_age_ms=1,
        fee_bps=Decimal(100),
        model_version="drifted",
    )
    frozen = ExecutionProfileResolver().resolve(
        intent_id=1,
        intent=intent,
        checkpoint=checkpoint,
        risk_status="ACCEPT",
        config=original,
    )

    result, _ = ProfessionalPaperExecutionKernel(
        TakerOnlyPaperExecutionEngine(drifted)
    ).execute(
        intent,
        decision_checkpoint=checkpoint,
        arrival_checkpoint=checkpoint,
        portfolio=None,
        intent_sequence=1,
        arrival_ts_override=now,
        execution_profile=frozen,
    )

    assert frozen.to_execution_config() == original
    assert result.status == "FILLED"
    assert result.model_version == original.model_version
    assert result.config_hash == original.config_hash
    assert result.fidelity["execution_profile"] == frozen.as_dict()

    service = object.__new__(LivePaperShadowService)
    service.engine = TakerOnlyPaperExecutionEngine(drifted)
    pending = PendingIntent(
        row=QueuedIntent(1, intent, frozen),
        decision_checkpoint=checkpoint,
        execution_profile=frozen,
    )
    assert service._pending_modeled_arrival_ts(pending) == now
