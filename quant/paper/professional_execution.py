"""Unified paper execution entry point with centralized pre-trade risk."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from quant.execution.domain import CapacityStatus
from quant.risk.capacity_gate import (
    CapacityGate,
    CapacityLimits,
    CapacitySnapshot,
)
from quant.risk.event_risk import (
    EventRiskAdmissionInput,
    EventRiskLimits,
    evaluate_projected_order_risk,
)

from .execution_profile import ExecutionProfileDecision
from .taker_execution import (
    ArrivalBookCheckpoint,
    OrderIntent,
    PaperExecutionResult,
    PaperPortfolioSnapshot,
    TakerOnlyPaperExecutionEngine,
)


@dataclass(frozen=True)
class PaperRiskLimits:
    max_order_notional: Decimal = Decimal(1000)
    max_strategy_gross_notional: Decimal = Decimal(10000)
    max_condition_notional: Decimal = Decimal(2500)
    max_event_notional: Decimal = Decimal(5000)
    max_neg_risk_group_notional: Decimal = Decimal(5000)
    max_category_notional: Decimal = Decimal(7500)
    max_daily_loss: Decimal = Decimal(1000)
    max_open_orders: int = 100
    max_order_rate_per_minute: int = 120
    max_visible_depth_ratio: Decimal = Decimal(1)
    capacity_reject_multiplier: Decimal = Decimal(2)
    max_event_worst_case_loss: Decimal = Decimal(5000)
    max_category_worst_case_loss: Decimal = Decimal(7500)
    max_illiquid_position_value: Decimal = Decimal(2500)
    max_dispute_locked_capital: Decimal = Decimal(5000)
    max_strategy_concentration: Decimal = Decimal(10000)


@dataclass(frozen=True)
class PaperRiskContext:
    trading_enabled: bool = True
    kill_switch: bool = False
    gross_notional: Decimal = Decimal(0)
    condition_notional: Decimal = Decimal(0)
    event_notional: Decimal = Decimal(0)
    neg_risk_group_notional: Decimal = Decimal(0)
    category_notional: Decimal = Decimal(0)
    daily_realized_pnl: Decimal = Decimal(0)
    open_order_count: int = 0
    recent_order_count: int = 0
    event_id: str = ""
    category: str = "unknown"
    neg_risk_group: str | None = None
    event_risk_input: EventRiskAdmissionInput | None = None
    event_risk_required: bool = False
    degradation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperRiskDecision:
    status: str
    reasons: tuple[str, ...]
    reduce_only: bool
    order_notional: Decimal
    projected_gross_notional: Decimal
    projected_condition_notional: Decimal
    projected_event_notional: Decimal
    projected_neg_risk_group_notional: Decimal
    projected_category_notional: Decimal
    capacity: dict[str, Any] | None
    event_risk: dict[str, Any] | None = None

    @property
    def accepted(self) -> bool:
        return self.status in {"ACCEPT", "ACCEPT_STRESSED"}

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return _json_value(payload)


@dataclass(frozen=True)
class PaperFidelityContext:
    """Explicit evidence supplied by a worker when it uses advanced models."""

    venue_regime_id: str = "UNBOUND"
    venue_emulated: bool = False
    global_liquidity_overlay_verified: bool = False
    taker_calibrated_in_domain: bool = False
    maker_calibrated_in_domain: bool = False
    impact_aware_scenario: bool = False
    latency_model_version: str = "paper_latency_model_v1"
    finality_model_version: str = "UNBOUND"
    valuation_model_version: str = "UNBOUND"
    calibration_domain_status: str = "UNCALIBRATED"
    confidence_reasons: tuple[str, ...] = ()


class CentralPaperRiskGate:
    """Single fail-closed risk authority for live paper order intents."""

    def __init__(self, limits: PaperRiskLimits | None = None) -> None:
        self.limits = limits or PaperRiskLimits()
        ratio = max(Decimal("0.00000001"), self.limits.max_visible_depth_ratio)
        self.capacity_gate = CapacityGate(
            CapacityLimits(
                max_top1_ratio=ratio,
                max_top5_ratio=ratio,
                max_visible_ratio=ratio,
                max_trailing_volume_ratio=Decimal(1000000000),
                max_strategy_participation=Decimal(1000000000),
                max_account_participation=Decimal(1000000000),
                max_event_notional=self.limits.max_event_notional,
                reject_multiplier=max(
                    Decimal(1), self.limits.capacity_reject_multiplier
                ),
            )
        )

    def evaluate(
        self,
        intent: OrderIntent,
        *,
        arrival_checkpoint: ArrivalBookCheckpoint | None,
        context: PaperRiskContext,
    ) -> PaperRiskDecision:
        notional = _order_notional(intent)
        exposure = _order_exposure(intent)
        reduce_only = intent.side == "SELL"
        projected_gross = (
            max(Decimal(0), context.gross_notional - exposure)
            if reduce_only
            else context.gross_notional + exposure
        )
        projected_condition = _project(
            context.condition_notional, exposure, reduce_only
        )
        projected_event = _project(context.event_notional, exposure, reduce_only)
        projected_neg_risk = _project(
            context.neg_risk_group_notional, exposure, reduce_only
        )
        projected_category = _project(context.category_notional, exposure, reduce_only)
        reasons: list[str] = []
        if not context.trading_enabled:
            reasons.append("strategy_trading_disabled")
        if context.kill_switch:
            reasons.append("strategy_kill_switch")
        reasons.extend(
            f"degradation:{reason}" for reason in context.degradation_reasons
        )

        if not reduce_only:
            if notional > self.limits.max_order_notional:
                reasons.append("max_order_notional")
            if projected_gross > self.limits.max_strategy_gross_notional:
                reasons.append("max_strategy_gross_notional")
            if projected_condition > self.limits.max_condition_notional:
                reasons.append("max_condition_notional")
            if projected_event > self.limits.max_event_notional:
                reasons.append("max_event_notional")
            if (
                context.neg_risk_group
                and projected_neg_risk > self.limits.max_neg_risk_group_notional
            ):
                reasons.append("max_neg_risk_group_notional")
            if projected_category > self.limits.max_category_notional:
                reasons.append("max_category_notional")
            if -context.daily_realized_pnl > self.limits.max_daily_loss:
                reasons.append("max_daily_loss")
            if context.open_order_count >= self.limits.max_open_orders:
                reasons.append("max_open_orders")
            if context.recent_order_count >= self.limits.max_order_rate_per_minute:
                reasons.append("max_order_rate_per_minute")

        capacity = _capacity_snapshot(
            intent,
            arrival_checkpoint,
            event_notional=projected_event,
        )
        capacity_payload: dict[str, Any] | None = None
        stressed = False
        if capacity is not None:
            capacity_decision = self.capacity_gate.evaluate(capacity)
            capacity_payload = capacity_decision.as_dict()
            stressed = capacity_decision.status == CapacityStatus.CAPACITY_STRESSED
            if (
                not reduce_only
                and capacity_decision.status
                == CapacityStatus.REJECT_UNCALIBRATED_CAPACITY
            ):
                reasons.append("uncalibrated_capacity")

        event_risk_payload: dict[str, Any] | None = None
        if context.event_risk_input is not None:
            event_risk_payload = evaluate_projected_order_risk(
                context.event_risk_input,
                intent=intent,
                arrival_checkpoint=arrival_checkpoint,
                event_id=context.event_id or intent.condition_id,
                category=context.category,
                limits=EventRiskLimits(
                    max_event_worst_case_loss=self.limits.max_event_worst_case_loss,
                    max_category_worst_case_loss=(
                        self.limits.max_category_worst_case_loss
                    ),
                    max_illiquid_position_value=(
                        self.limits.max_illiquid_position_value
                    ),
                    max_dispute_locked_capital=(self.limits.max_dispute_locked_capital),
                    max_strategy_concentration=(self.limits.max_strategy_concentration),
                ),
            )
            event_risk_payload["gate_enforced"] = not reduce_only
            if not reduce_only and event_risk_payload["status"] != "PASS":
                reasons.extend(
                    f"event_risk:{breach}"
                    for breach in event_risk_payload.get("breaches", ())
                )
        elif context.event_risk_required and not reduce_only:
            reasons.append("event_risk:input_unavailable")

        status = "REJECT" if reasons else "ACCEPT_STRESSED" if stressed else "ACCEPT"
        return PaperRiskDecision(
            status=status,
            reasons=tuple(dict.fromkeys(reasons)),
            reduce_only=reduce_only,
            order_notional=notional,
            projected_gross_notional=projected_gross,
            projected_condition_notional=projected_condition,
            projected_event_notional=projected_event,
            projected_neg_risk_group_notional=projected_neg_risk,
            projected_category_notional=projected_category,
            capacity=capacity_payload,
            event_risk=event_risk_payload,
        )


class ProfessionalPaperExecutionKernel:
    """The only live-paper entry point allowed to call the taker engine."""

    def __init__(
        self,
        engine: TakerOnlyPaperExecutionEngine,
        *,
        risk_gate: CentralPaperRiskGate | None = None,
    ) -> None:
        self.engine = engine
        self.risk_gate = risk_gate or CentralPaperRiskGate()

    @property
    def config(self):
        return self.engine.config

    def execute(
        self,
        intent: OrderIntent,
        *,
        decision_checkpoint: ArrivalBookCheckpoint | None,
        arrival_checkpoint: ArrivalBookCheckpoint | None,
        portfolio: PaperPortfolioSnapshot | None,
        intent_sequence: int,
        risk_context: PaperRiskContext | None = None,
        risk_decision: PaperRiskDecision | None = None,
        arrival_ts_override: datetime | None = None,
        fidelity_context: PaperFidelityContext | None = None,
        execution_profile: ExecutionProfileDecision | None = None,
    ) -> tuple[PaperExecutionResult, PaperRiskDecision]:
        execution_engine = self.engine
        if (
            execution_profile is not None
            and self.engine.config.config_hash
            != execution_profile.execution_config_hash
        ):
            execution_engine = TakerOnlyPaperExecutionEngine(
                execution_profile.to_execution_config(),
                audit_sink=self.engine.audit_sink,
                liquidity_overlay=self.engine.liquidity_overlay,
            )
        decision = risk_decision or self.evaluate_risk(
            intent,
            arrival_checkpoint=arrival_checkpoint,
            context=risk_context or PaperRiskContext(),
        )
        fidelity = self._fidelity(
            decision,
            arrival_checkpoint,
            fidelity_context or PaperFidelityContext(),
        )
        if execution_profile is not None:
            fidelity["execution_profile"] = execution_profile.as_dict()
        if not decision.accepted:
            result = execution_engine.reject(
                intent,
                reason="risk:" + ",".join(decision.reasons),
                decision_checkpoint=decision_checkpoint,
                arrival_checkpoint=arrival_checkpoint,
                fidelity_metadata=fidelity,
            )
            return result, decision
        if execution_profile is not None and not execution_profile.execution_allowed:
            result = execution_engine.reject(
                intent,
                reason=(
                    "execution_profile:"
                    + execution_profile.profile.value
                    + ":"
                    + ",".join(execution_profile.reason_codes)
                ),
                decision_checkpoint=decision_checkpoint,
                arrival_checkpoint=arrival_checkpoint,
                status="DATA_NOT_READY",
                fidelity_metadata=fidelity,
            )
            return result, decision
        result = execution_engine.execute(
            intent,
            decision_checkpoint=decision_checkpoint,
            arrival_checkpoint=arrival_checkpoint,
            portfolio=portfolio,
            arrival_ts_override=arrival_ts_override,
            intent_sequence=intent_sequence,
            fidelity_metadata=fidelity,
            depth_haircut_override=(
                execution_profile.depth_haircut if execution_profile else None
            ),
        )
        return result, decision

    def evaluate_risk(
        self,
        intent: OrderIntent,
        *,
        arrival_checkpoint: ArrivalBookCheckpoint | None,
        context: PaperRiskContext,
    ) -> PaperRiskDecision:
        return self.risk_gate.evaluate(
            intent,
            arrival_checkpoint=arrival_checkpoint,
            context=context,
        )

    def _fidelity(
        self,
        decision: PaperRiskDecision,
        checkpoint: ArrivalBookCheckpoint | None,
        context: PaperFidelityContext,
    ) -> dict[str, Any]:
        from quant.simulator.fidelity import FidelityInputs, assess_fidelity

        data_safe = bool(
            checkpoint
            and checkpoint.coverage_grade in {"A_PLUS", "A"}
            and checkpoint.book_status.upper()
            in {"READY", "READY_HIGH", "READY_MEDIUM"}
            and not checkpoint.has_gap
        )
        capacity_status = str(
            (decision.capacity or {}).get("status") or "NOT_EVALUATED"
        )
        return assess_fidelity(
            FidelityInputs(
                data_quality_grade=checkpoint.coverage_grade if checkpoint else None,
                data_safe=data_safe,
                venue_emulated=context.venue_emulated,
                capacity_safe=(
                    context.global_liquidity_overlay_verified
                    and capacity_status == CapacityStatus.IN_DOMAIN_CALIBRATED.value
                ),
                taker_calibrated_in_domain=context.taker_calibrated_in_domain,
                maker_calibrated_in_domain=context.maker_calibrated_in_domain,
                impact_aware_scenario=context.impact_aware_scenario,
                capacity_status=capacity_status,
                venue_regime_id=context.venue_regime_id,
                fill_model_version=self.engine.config.model_version,
                latency_model_version=context.latency_model_version,
                finality_model_version=context.finality_model_version,
                valuation_model_version=context.valuation_model_version,
                calibration_domain_status=context.calibration_domain_status,
                confidence_reasons=context.confidence_reasons,
            )
        ).as_dict()


def _order_notional(intent: OrderIntent) -> Decimal:
    if intent.side == "BUY" and intent.amount_unit == "QUOTE":
        return max(Decimal(0), intent.size)
    return max(Decimal(0), intent.size * intent.limit_price)


def _order_exposure(intent: OrderIntent) -> Decimal:
    if intent.side == "BUY" and intent.amount_unit == "QUOTE":
        return max(Decimal(0), intent.size / intent.limit_price)
    return max(Decimal(0), intent.size)


def _project(current: Decimal, order_notional: Decimal, reduce_only: bool) -> Decimal:
    if reduce_only:
        return max(Decimal(0), current - order_notional)
    return current + order_notional


def _capacity_snapshot(
    intent: OrderIntent,
    checkpoint: ArrivalBookCheckpoint | None,
    *,
    event_notional: Decimal,
) -> CapacitySnapshot | None:
    if checkpoint is None:
        return None
    levels = checkpoint.asks if intent.side == "BUY" else checkpoint.bids
    eligible = [
        level
        for level in levels
        if (
            level.price <= intent.limit_price
            if intent.side == "BUY"
            else level.price >= intent.limit_price
        )
    ]
    if not eligible:
        return None
    notional = _order_notional(intent)
    top1 = max(Decimal("0.00000001"), eligible[0].price * eligible[0].size)
    top5 = max(
        Decimal("0.00000001"),
        sum((level.price * level.size for level in eligible[:5]), Decimal(0)),
    )
    visible = max(
        Decimal("0.00000001"),
        sum((level.price * level.size for level in eligible), Decimal(0)),
    )
    return CapacitySnapshot(
        order_to_top1_depth=notional / top1,
        order_to_top5_depth=notional / top5,
        order_to_visible_eligible_depth=notional / visible,
        order_to_trailing_real_volume=Decimal(0),
        strategy_market_window_participation=Decimal(0),
        account_market_window_participation=Decimal(0),
        event_level_notional=event_notional,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
