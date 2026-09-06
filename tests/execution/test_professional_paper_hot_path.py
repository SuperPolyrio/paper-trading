import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from quant.execution.models.marks import MarkInput, build_marks
from quant.paper.execution_profile import (
    ExecutionProfile,
    ExecutionProfileDecision,
    ExecutionProfileResolver,
)
from quant.paper.market_terms import CachedPolymarketTermsResolver, PaperMarketTerms
from quant.paper.live_shadow_service import (
    LivePaperShadowService,
    LiveShadowStats,
    PendingIntent,
)
from quant.paper.portfolio_analytics import (
    PaperAssetMark,
    PaperNavPosition,
    calculate_portfolio_nav,
)
from quant.paper.professional_execution import (
    CentralPaperRiskGate,
    PaperFidelityContext,
    PaperRiskContext,
    PaperRiskLimits,
    ProfessionalPaperExecutionKernel,
)
from quant.paper.taker_execution import (
    ArrivalBookCheckpoint,
    InMemoryPaperLedger,
    OrderIntent,
    PaperBookLevel,
    PaperPortfolioSnapshot,
    TakerExecutionConfig,
    TakerOnlyPaperExecutionEngine,
)
from quant.risk.event_risk import EventRiskAdmissionInput

NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


def _intent(*, side: str = "BUY", size: str = "2") -> OrderIntent:
    return OrderIntent(
        strategy_id="strategy",
        market_id="market",
        condition_id="condition",
        asset_id="asset",
        side=side,
        order_type="FOK",
        limit_price=Decimal("0.5"),
        size=Decimal(size),
        post_only=False,
        decision_ts=NOW,
        client_order_id=f"order-{side}-{size}",
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
        bids=(PaperBookLevel(Decimal("0.49"), Decimal(100)),),
        asks=(PaperBookLevel(Decimal("0.50"), Decimal(100)),),
    )


def test_central_risk_gate_rejects_kill_switch_and_exposure() -> None:
    gate = CentralPaperRiskGate(
        PaperRiskLimits(
            max_order_notional=Decimal(10),
            max_strategy_gross_notional=Decimal(3),
        )
    )
    killed = gate.evaluate(
        _intent(),
        arrival_checkpoint=_checkpoint(),
        context=PaperRiskContext(kill_switch=True),
    )
    assert killed.status == "REJECT"
    assert "strategy_kill_switch" in killed.reasons
    exposure = gate.evaluate(
        _intent(),
        arrival_checkpoint=_checkpoint(),
        context=PaperRiskContext(gross_notional=Decimal(2)),
    )
    assert exposure.status == "REJECT"
    assert "max_strategy_gross_notional" in exposure.reasons


def test_reduce_only_sell_remains_available_during_loss_halt() -> None:
    decision = CentralPaperRiskGate(
        PaperRiskLimits(max_daily_loss=Decimal(1))
    ).evaluate(
        _intent(side="SELL"),
        arrival_checkpoint=_checkpoint(),
        context=PaperRiskContext(
            gross_notional=Decimal(10),
            daily_realized_pnl=Decimal(-100),
        ),
    )
    assert decision.accepted
    assert decision.reduce_only


def test_central_gate_fails_closed_when_required_event_input_is_missing() -> None:
    decision = CentralPaperRiskGate().evaluate(
        _intent(),
        arrival_checkpoint=_checkpoint(),
        context=PaperRiskContext(event_risk_required=True),
    )

    assert decision.status == "REJECT"
    assert decision.reasons == ("event_risk:input_unavailable",)


def test_reduce_only_sell_remains_available_when_event_input_is_unavailable() -> None:
    decision = CentralPaperRiskGate().evaluate(
        _intent(side="SELL"),
        arrival_checkpoint=_checkpoint(),
        context=PaperRiskContext(
            event_risk_required=True,
            event_risk_input=EventRiskAdmissionInput(
                positions=(),
                scenarios=(),
                status="UNAVAILABLE",
                reasons=("missing_payout_scenarios",),
            ),
        ),
    )

    assert decision.accepted
    assert decision.reduce_only
    assert decision.event_risk is not None
    assert decision.event_risk["gate_enforced"] is False


def test_professional_kernel_is_the_only_taker_entry_point() -> None:
    audit = InMemoryPaperLedger()
    engine = TakerOnlyPaperExecutionEngine(audit_sink=audit)
    kernel = ProfessionalPaperExecutionKernel(
        engine,
        risk_gate=CentralPaperRiskGate(
            PaperRiskLimits(max_order_notional=Decimal("0.5"))
        ),
    )
    result, decision = kernel.execute(
        _intent(),
        decision_checkpoint=_checkpoint(),
        arrival_checkpoint=_checkpoint(),
        portfolio=PaperPortfolioSnapshot(
            cash_balance=Decimal(100),
            position_size=Decimal(0),
        ),
        risk_context=PaperRiskContext(),
        intent_sequence=1,
    )
    assert decision.status == "REJECT"
    assert result.status == "REJECTED"
    assert result.reason == "risk:max_order_notional"
    assert audit.rows == [result]


def test_professional_kernel_honors_live_arrival_override() -> None:
    live_arrival = NOW + timedelta(seconds=1)
    checkpoint = replace(_checkpoint(), observed_at=live_arrival)
    result, decision = ProfessionalPaperExecutionKernel(
        TakerOnlyPaperExecutionEngine()
    ).execute(
        _intent(),
        decision_checkpoint=_checkpoint(),
        arrival_checkpoint=checkpoint,
        portfolio=PaperPortfolioSnapshot(
            cash_balance=Decimal(100),
            position_size=Decimal(0),
        ),
        risk_context=PaperRiskContext(),
        intent_sequence=1,
        arrival_ts_override=live_arrival,
    )
    assert decision.accepted
    assert result.arrival_ts == live_arrival


def test_professional_kernel_attaches_evidence_based_fidelity_before_audit() -> None:
    audit = InMemoryPaperLedger()
    result, _decision = ProfessionalPaperExecutionKernel(
        TakerOnlyPaperExecutionEngine(audit_sink=audit)
    ).execute(
        _intent(),
        decision_checkpoint=_checkpoint(),
        arrival_checkpoint=_checkpoint(),
        portfolio=PaperPortfolioSnapshot(Decimal(100), Decimal(0)),
        intent_sequence=1,
        fidelity_context=PaperFidelityContext(
            venue_regime_id="venue-test-v1",
            venue_emulated=True,
            global_liquidity_overlay_verified=True,
            taker_calibrated_in_domain=True,
            finality_model_version="finality-v1",
            valuation_model_version="valuation-v1",
            calibration_domain_status="TAKER_HOLDOUT_IN_DOMAIN",
        ),
    )
    assert result.fidelity["fidelity_level"] == "F4_TAKER_CALIBRATED_IN_DOMAIN"
    assert result.fidelity["venue_regime_id"] == "venue-test-v1"
    assert audit.rows[0].fidelity == result.fidelity


def test_operations_read_only_rejects_before_venue_or_market_work(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        status_path = tmp_path / "operations.json"
        status_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "FAIL",
                    "operational_level": "ORANGE",
                    "admission_mode": "READ_ONLY",
                }
            ),
            encoding="utf-8",
        )
        service = object.__new__(LivePaperShadowService)
        service.lifecycle_scheduler_enforce_order = False
        service.operations_admission_shadow = False
        service.operations_admission_enforce = True
        service.operations_status_path = status_path
        service.operations_status_max_age_seconds = 90.0
        service.operations_yellow_max_notional = Decimal(20)
        service.stats = LiveShadowStats(worker_id="worker")
        service.engine = TakerOnlyPaperExecutionEngine()
        service.pending = {
            1: PendingIntent(
                row=SimpleNamespace(intent_id=1, intent=_intent()),
                decision_checkpoint=_checkpoint(),
            )
        }
        service._pending_modeled_arrival_ts = lambda _item: NOW
        completed = []

        async def complete_intent(intent_id, result, **_kwargs):
            completed.append((intent_id, result))

        async def forbidden_venue_lookup(_intent_id):
            raise AssertionError("operations rejection must precede venue lookup")

        service._complete_intent = complete_intent
        service._bind_venue_regime = forbidden_venue_lookup

        await service._execute_due()

        assert len(completed) == 1
        assert completed[0][1].reason == "operations:operations_read_only"
        assert service.stats.operations_admission_enforced_rejections == 1
        assert service.pending == {}

    asyncio.run(exercise())


def test_execution_profile_resolver_freezes_safe_small_taker_evidence() -> None:
    decision = ExecutionProfileResolver().resolve(
        intent_id=7,
        intent=_intent(),
        checkpoint=_checkpoint(),
        risk_status="ACCEPT",
        config=TakerExecutionConfig(),
    )

    assert decision.profile == ExecutionProfile.SMALL_TAKER_L2
    assert decision.execution_allowed
    assert decision.depth_haircut == 1
    assert decision.calibration_domain == "UNCALIBRATED"
    assert decision.hash_is_valid
    assert ExecutionProfileDecision.from_dict(decision.as_dict()) == decision


def test_execution_profile_resolver_degrades_grade_b_and_stressed_capacity() -> None:
    resolver = ExecutionProfileResolver()
    config = TakerExecutionConfig(grade_b_depth_haircut=Decimal("0.20"))
    grade_b = resolver.resolve(
        intent_id=8,
        intent=_intent(),
        checkpoint=replace(_checkpoint(), coverage_grade="B"),
        risk_status="ACCEPT",
        config=config,
    )
    stressed = resolver.resolve(
        intent_id=9,
        intent=_intent(),
        checkpoint=_checkpoint(),
        risk_status="ACCEPT_STRESSED",
        config=config,
    )

    assert grade_b.profile == ExecutionProfile.CONSERVATIVE_DEPTH
    assert grade_b.depth_haircut == Decimal("0.20")
    assert grade_b.reason_codes == ("coverage_grade_b",)
    assert stressed.profile == ExecutionProfile.CONSERVATIVE_DEPTH
    assert stressed.reason_codes == ("capacity_stressed",)


def test_execution_profile_resolver_fails_closed_without_safe_book() -> None:
    decision = ExecutionProfileResolver().resolve(
        intent_id=10,
        intent=_intent(),
        checkpoint=None,
        risk_status="ACCEPT",
        config=TakerExecutionConfig(),
    )

    assert decision.profile == ExecutionProfile.STRICT_NO_FILL
    assert not decision.execution_allowed
    assert decision.depth_haircut == 0


def test_execution_profile_controls_depth_and_is_audited() -> None:
    config = TakerExecutionConfig(grade_b_depth_haircut=Decimal("0.25"))
    checkpoint = replace(
        _checkpoint(),
        coverage_grade="B",
        asks=(PaperBookLevel(Decimal("0.50"), Decimal("4")),),
    )
    profile = ExecutionProfileResolver().resolve(
        intent_id=11,
        intent=_intent(),
        checkpoint=checkpoint,
        risk_status="ACCEPT",
        config=config,
    )
    result, risk = ProfessionalPaperExecutionKernel(
        TakerOnlyPaperExecutionEngine(config)
    ).execute(
        _intent(),
        decision_checkpoint=checkpoint,
        arrival_checkpoint=checkpoint,
        portfolio=PaperPortfolioSnapshot(Decimal("100"), Decimal("0")),
        intent_sequence=11,
        execution_profile=profile,
    )

    assert risk.accepted
    assert result.status == "REJECTED"
    assert result.reason == "fok_insufficient_arrival_depth"
    assert result.fidelity["execution_profile"]["decision_hash"] == (
        profile.decision_hash
    )


class _TermsRepository:
    def __init__(self) -> None:
        self.row: PaperMarketTerms | None = None

    def load_market_terms(self, asset_id: str, *, now: datetime):
        return self.row if self.row and self.row.expires_at > now else None

    def upsert_market_terms(self, terms: PaperMarketTerms) -> None:
        self.row = terms


class _TermsClient:
    def __init__(self) -> None:
        self.fee_calls = 0
        self.market_calls = 0

    async def get_fee_rate(self, asset_id: str) -> int:
        self.fee_calls += 1
        return 1000

    async def get_clob_market_info(self, condition_id: str):
        self.market_calls += 1
        return {"fd": {"r": "0.25", "e": "2", "to": True}, "itode": True}

    async def get_market(self, condition_id: str):
        return {"seconds_delay": 2}


def test_dynamic_market_terms_are_authoritative_and_cached() -> None:
    client = _TermsClient()
    repo = _TermsRepository()
    resolver = CachedPolymarketTermsResolver(client, repo, ttl_seconds=3600)
    first = asyncio.run(resolver.resolve(_intent()))
    second = asyncio.run(resolver.resolve(_intent()))
    assert first.fee_rate_bps == 1000
    assert first.fee_rate == Decimal("0.25")
    assert first.fee_exponent == Decimal(2)
    assert first.itode is True
    assert first.seconds_delay == 2
    assert first.taker_delay_ms == 2_000
    assert first.apply(_intent()).venue_taker_delay_ms == 2_000
    assert second == first
    assert client.fee_calls == 1
    assert client.market_calls == 1


def test_dynamic_market_terms_deduplicate_concurrent_prefetch() -> None:
    class _SlowTermsClient(_TermsClient):
        async def get_fee_rate(self, asset_id: str) -> int:
            await asyncio.sleep(0.01)
            return await super().get_fee_rate(asset_id)

    client = _SlowTermsClient()
    resolver = CachedPolymarketTermsResolver(
        client,
        _TermsRepository(),
        ttl_seconds=300,
    )

    async def resolve_many() -> list[PaperMarketTerms]:
        return await asyncio.gather(
            *(
                resolver.resolve_asset(
                    asset_id="asset-1",
                    condition_id="condition-1",
                )
                for _ in range(8)
            )
        )

    terms = asyncio.run(resolve_many())

    assert all(item == terms[0] for item in terms)
    assert client.fee_calls == 1
    assert client.market_calls == 1
    assert resolver.needs_refresh("asset-1") is False

    asyncio.run(resolver.close())
    assert resolver._inflight == {}


def test_market_terms_parse_false_itode_string_without_enabling_delay() -> None:
    class _StringBooleanClient(_TermsClient):
        async def get_clob_market_info(self, condition_id: str):
            self.market_calls += 1
            return {
                "fd": {"r": "0.25", "e": "2", "to": True},
                "itode": "false",
            }

        async def get_market(self, condition_id: str):
            return {"seconds_delay": 0}

    client = _StringBooleanClient()
    resolver = CachedPolymarketTermsResolver(
        client,
        _TermsRepository(),
        ttl_seconds=300,
    )

    terms = asyncio.run(resolver.resolve(_intent()))

    assert terms.itode is False
    assert terms.taker_delay_ms == 0
    assert terms.delay_source == "clob_market_no_delay"


def test_nav_marks_unmarkable_positions_conservatively() -> None:
    mark = build_marks(
        MarkInput(
            as_of=NOW,
            source_event_at=NOW,
            side="LONG",
            best_bid=Decimal("0.4"),
            best_ask=Decimal("0.5"),
        )
    )
    nav = calculate_portfolio_nav(
        initial_cash=Decimal(100),
        cash_balance=Decimal(90),
        positions=[
            PaperNavPosition("marked", Decimal(10), Decimal(4)),
            PaperNavPosition("missing", Decimal(2), Decimal(1)),
        ],
        marks={
            "marked": PaperAssetMark(
                "marked",
                NOW,
                mark,
                best_bid=Decimal("0.4"),
                best_ask=Decimal("0.5"),
            )
        },
        previous_high_watermark=Decimal(100),
    )
    assert nav.nav_complete is False
    assert nav.equity is None
    assert nav.conservative_position_value == Decimal(4)
    assert nav.conservative_equity == Decimal(94)
    assert nav.unmarkable_positions == 1
    assert nav.drawdown == Decimal(6)


def test_nav_uses_stale_bid_only_for_conservative_equity() -> None:
    stale = build_marks(
        MarkInput(
            as_of=NOW,
            source_event_at=NOW - timedelta(seconds=1),
            side="LONG",
            best_bid=Decimal("0.3"),
            best_ask=Decimal("0.4"),
            stale_after_ms=1,
        )
    )
    nav = calculate_portfolio_nav(
        initial_cash=Decimal(100),
        cash_balance=Decimal(90),
        positions=[PaperNavPosition("stale", Decimal(10), Decimal(4))],
        marks={
            "stale": PaperAssetMark(
                "stale",
                NOW,
                stale,
                best_bid=Decimal("0.3"),
                best_ask=Decimal("0.4"),
            )
        },
        previous_high_watermark=Decimal(100),
    )
    assert nav.nav_complete is False
    assert nav.equity is None
    assert nav.conservative_position_value == Decimal(3)
    assert nav.conservative_equity == Decimal(93)
