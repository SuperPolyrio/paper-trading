"""Taker-only paper execution against an arrival-time L2 checkpoint."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, Protocol

from quant.execution.overlay.counterfactual_book import CounterfactualLiquidityOverlay
from quant.simulator.economics import (
    FeeCharge,
    FeeEngine,
    FeeSchedule,
    LiquidityRole,
    fee_schedule_id,
    maximum_order_fees,
)

Side = Literal["BUY", "SELL"]
TimeInForce = Literal["GTC", "GTD", "FOK", "FAK"]
CoverageGrade = Literal["A_PLUS", "A", "B", "C", "D"]
AmountUnit = Literal["SHARES", "QUOTE"]
PaperStatus = Literal[
    "FILLED",
    "PARTIAL",
    "CANCELLED",
    "WORKING",
    "REJECTED",
    "DATA_NOT_READY",
    "BOOK_STALE",
    "BOOK_GAP",
    "MARKET_NOT_TRADABLE",
]

GTD_MIN_STATED_LEAD = timedelta(minutes=3)
GTD_SECURITY_THRESHOLD = timedelta(minutes=1)


def gtd_effective_expires_at(expires_at: datetime) -> datetime:
    """Return the venue-effective expiry for a stated GTD expiration."""

    return expires_at - GTD_SECURITY_THRESHOLD


def validate_gtd_expiration(
    decision_ts: datetime,
    expires_at: datetime | None,
) -> str | None:
    """Validate the public GTD contract without consulting wall-clock time."""

    if expires_at is None:
        return "gtd_expiration_missing"
    try:
        if expires_at < decision_ts + GTD_MIN_STATED_LEAD:
            return "gtd_expiration_too_soon"
    except TypeError:
        return "gtd_expiration_timezone_mismatch"
    return None


@dataclass(frozen=True)
class OrderIntent:
    strategy_id: str
    market_id: str
    condition_id: str
    asset_id: str
    side: Side
    order_type: TimeInForce
    limit_price: Decimal
    size: Decimal
    post_only: bool
    decision_ts: datetime
    client_order_id: str
    amount_unit: AmountUnit = "SHARES"
    expires_at: datetime | None = None
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    fee_rate: Decimal | None = None
    fee_exponent: Decimal | None = None
    fee_taker_only: bool = True
    venue_regime_id: str | None = None
    venue_regime_source_hash: str | None = None
    venue_taker_delay_ms: int = 0
    venue_delay_source: str | None = None
    venue_itode: bool = False
    venue_seconds_delay: int = 0
    fee_schedule_id: str | None = None
    fee_schedule_source: str | None = None
    economics_regime_id: str | None = None
    builder_code: str | None = None
    builder_taker_fee_bps: int = 0
    builder_maker_fee_bps: int = 0


@dataclass(frozen=True)
class PaperBookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class ArrivalBookCheckpoint:
    checkpoint_id: str
    asset_id: str
    market_id: str
    condition_id: str
    observed_at: datetime
    generation: int
    coverage_grade: CoverageGrade
    bids: tuple[PaperBookLevel, ...]
    asks: tuple[PaperBookLevel, ...]
    market_state: str = "LIVE"
    book_status: str = "READY"
    has_gap: bool = False
    source_manifest_ids: tuple[int, ...] = ()
    source_files: tuple[str, ...] = ()
    source_event_start: str | None = None
    source_event_end: str | None = None
    rest_audit_at: datetime | None = None

    def age_ms(self, at: datetime) -> int:
        return max(0, int((at - self.observed_at).total_seconds() * 1000))


@dataclass(frozen=True)
class PaperLatencyModel:
    feed_delay_ms: int = 0
    strategy_delay_ms: int = 0
    order_delay_ms: int = 100

    def submit_request_ts(self, decision_ts: datetime) -> datetime:
        delay = max(0, self.feed_delay_ms) + max(0, self.strategy_delay_ms)
        return decision_ts + timedelta(milliseconds=delay)

    def arrival_ts(self, decision_ts: datetime) -> datetime:
        return self.submit_request_ts(decision_ts) + timedelta(
            milliseconds=max(0, self.order_delay_ms)
        )


@dataclass(frozen=True)
class TakerExecutionConfig:
    latency: PaperLatencyModel = field(default_factory=PaperLatencyModel)
    max_book_age_ms: int = 2_000
    grade_b_depth_haircut: Decimal = Decimal("0.25")
    fee_bps: Decimal = Decimal("0")
    model_version: str = "paper_taker_l2_v7_shadow_head"

    @property
    def config_hash(self) -> str:
        payload = json.dumps(
            _json_value(asdict(self)), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class PaperPortfolioSnapshot:
    cash_balance: Decimal
    position_size: Decimal


@dataclass(frozen=True)
class PaperTakerFill:
    price: Decimal
    size: Decimal
    fee: Decimal
    level_index: int
    settlement_match_type: str = "UNKNOWN"
    settlement_evidence_id: str | None = None
    settlement_evidence_source: str = "PAPER_L2_NO_COUNTERPARTY"
    settlement_conservation_status: str = "NOT_PROVABLE"
    settlement_conservation_hash: str | None = None
    fee_charge_id: str | None = None
    fee_schedule_id: str | None = None
    platform_fee_rate: Decimal = Decimal(0)
    platform_fee_exponent: Decimal = Decimal(1)
    platform_fee: Decimal = Decimal(0)
    builder_fee: Decimal = Decimal(0)
    builder_fee_rate_bps: int = 0
    rounding_policy: str | None = None
    economics_regime_id: str | None = None
    fee_source: str | None = None


@dataclass(frozen=True)
class PaperExecutionResult:
    audit_key: str
    status: PaperStatus
    reason: str
    intent: OrderIntent
    arrival_ts: datetime
    decision_checkpoint_id: str | None
    arrival_checkpoint_id: str | None
    book_generation: int | None
    coverage_grade: str | None
    book_age_ms: int | None
    fills: tuple[PaperTakerFill, ...]
    requested_amount: Decimal
    amount_unit: str
    filled_size: Decimal
    remaining_size: Decimal
    filled_notional: Decimal
    remaining_amount: Decimal
    avg_fill_price: Decimal | None
    total_fee: Decimal
    slippage: Decimal | None
    source_manifest_ids: tuple[int, ...]
    source_files: tuple[str, ...]
    source_event_start: str | None
    source_event_end: str | None
    rest_audit_at: datetime | None
    model_version: str
    config_hash: str
    fidelity: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


def paper_execution_result_from_payload(
    payload: Mapping[str, Any],
) -> PaperExecutionResult:
    """Rehydrate an immutable execution result from its durable JSON form."""

    def decimal_value(value: Any, default: str = "0") -> Decimal:
        return Decimal(str(default if value is None else value))

    def optional_decimal(value: Any) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    def datetime_value(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    def optional_datetime(value: Any) -> datetime | None:
        return None if value is None else datetime_value(value)

    raw_intent = dict(payload.get("intent") or {})
    if not raw_intent:
        raise ValueError("durable execution result is missing intent")
    intent = OrderIntent(
        strategy_id=str(raw_intent["strategy_id"]),
        market_id=str(raw_intent["market_id"]),
        condition_id=str(raw_intent["condition_id"]),
        asset_id=str(raw_intent["asset_id"]),
        side=str(raw_intent["side"]).upper(),
        order_type=str(raw_intent["order_type"]).upper(),
        limit_price=decimal_value(raw_intent["limit_price"]),
        size=decimal_value(raw_intent["size"]),
        post_only=bool(raw_intent.get("post_only", False)),
        decision_ts=datetime_value(raw_intent["decision_ts"]),
        client_order_id=str(raw_intent["client_order_id"]),
        amount_unit=str(raw_intent.get("amount_unit") or "SHARES").upper(),
        expires_at=optional_datetime(raw_intent.get("expires_at")),
        tick_size=optional_decimal(raw_intent.get("tick_size")),
        min_order_size=optional_decimal(raw_intent.get("min_order_size")),
        fee_rate=optional_decimal(raw_intent.get("fee_rate")),
        fee_exponent=optional_decimal(raw_intent.get("fee_exponent")),
        fee_taker_only=bool(raw_intent.get("fee_taker_only", True)),
        venue_regime_id=raw_intent.get("venue_regime_id"),
        venue_regime_source_hash=raw_intent.get("venue_regime_source_hash"),
        venue_taker_delay_ms=int(raw_intent.get("venue_taker_delay_ms") or 0),
        venue_delay_source=raw_intent.get("venue_delay_source"),
        venue_itode=bool(raw_intent.get("venue_itode", False)),
        venue_seconds_delay=int(raw_intent.get("venue_seconds_delay") or 0),
        fee_schedule_id=raw_intent.get("fee_schedule_id"),
        fee_schedule_source=raw_intent.get("fee_schedule_source"),
        economics_regime_id=raw_intent.get("economics_regime_id"),
        builder_code=raw_intent.get("builder_code"),
        builder_taker_fee_bps=int(raw_intent.get("builder_taker_fee_bps") or 0),
        builder_maker_fee_bps=int(raw_intent.get("builder_maker_fee_bps") or 0),
    )
    fills = tuple(
        PaperTakerFill(
            price=decimal_value(raw["price"]),
            size=decimal_value(raw["size"]),
            fee=decimal_value(raw.get("fee")),
            level_index=int(raw.get("level_index") or 0),
            settlement_match_type=str(raw.get("settlement_match_type") or "UNKNOWN"),
            settlement_evidence_id=raw.get("settlement_evidence_id"),
            settlement_evidence_source=str(
                raw.get("settlement_evidence_source") or "PAPER_L2_NO_COUNTERPARTY"
            ),
            settlement_conservation_status=str(
                raw.get("settlement_conservation_status") or "NOT_PROVABLE"
            ),
            settlement_conservation_hash=raw.get("settlement_conservation_hash"),
            fee_charge_id=raw.get("fee_charge_id"),
            fee_schedule_id=raw.get("fee_schedule_id"),
            platform_fee_rate=decimal_value(raw.get("platform_fee_rate")),
            platform_fee_exponent=decimal_value(
                raw.get("platform_fee_exponent"), "1"
            ),
            platform_fee=decimal_value(raw.get("platform_fee")),
            builder_fee=decimal_value(raw.get("builder_fee")),
            builder_fee_rate_bps=int(raw.get("builder_fee_rate_bps") or 0),
            rounding_policy=raw.get("rounding_policy"),
            economics_regime_id=raw.get("economics_regime_id"),
            fee_source=raw.get("fee_source"),
        )
        for raw in (dict(item) for item in payload.get("fills") or ())
    )
    return PaperExecutionResult(
        audit_key=str(payload["audit_key"]),
        status=str(payload["status"]).upper(),
        reason=str(payload.get("reason") or "durable_result_recovery"),
        intent=intent,
        arrival_ts=datetime_value(payload["arrival_ts"]),
        decision_checkpoint_id=payload.get("decision_checkpoint_id"),
        arrival_checkpoint_id=payload.get("arrival_checkpoint_id"),
        book_generation=(
            int(payload["book_generation"])
            if payload.get("book_generation") is not None
            else None
        ),
        coverage_grade=payload.get("coverage_grade"),
        book_age_ms=(
            int(payload["book_age_ms"])
            if payload.get("book_age_ms") is not None
            else None
        ),
        fills=fills,
        requested_amount=decimal_value(payload.get("requested_amount")),
        amount_unit=str(payload.get("amount_unit") or intent.amount_unit).upper(),
        filled_size=decimal_value(payload.get("filled_size")),
        remaining_size=decimal_value(payload.get("remaining_size")),
        filled_notional=decimal_value(payload.get("filled_notional")),
        remaining_amount=decimal_value(payload.get("remaining_amount")),
        avg_fill_price=optional_decimal(payload.get("avg_fill_price")),
        total_fee=decimal_value(payload.get("total_fee")),
        slippage=optional_decimal(payload.get("slippage")),
        source_manifest_ids=tuple(
            int(item) for item in payload.get("source_manifest_ids") or ()
        ),
        source_files=tuple(str(item) for item in payload.get("source_files") or ()),
        source_event_start=payload.get("source_event_start"),
        source_event_end=payload.get("source_event_end"),
        rest_audit_at=optional_datetime(payload.get("rest_audit_at")),
        model_version=str(payload.get("model_version") or "unknown"),
        config_hash=str(payload.get("config_hash") or "unknown"),
        fidelity=dict(payload.get("fidelity") or {}),
    )


class PaperAuditSink(Protocol):
    def append(self, result: PaperExecutionResult) -> None: ...


class InMemoryPaperLedger:
    def __init__(self) -> None:
        self.rows: list[PaperExecutionResult] = []

    def append(self, result: PaperExecutionResult) -> None:
        self.rows.append(result)


class TakerOnlyPaperExecutionEngine:
    """Deterministic shadow engine; it has no account, position, or PnL side effects."""

    def __init__(
        self,
        config: TakerExecutionConfig | None = None,
        *,
        audit_sink: PaperAuditSink | None = None,
        liquidity_overlay: CounterfactualLiquidityOverlay | None = None,
    ) -> None:
        self.config = config or TakerExecutionConfig()
        self.audit_sink = audit_sink or InMemoryPaperLedger()
        self._results: dict[str, PaperExecutionResult] = {}
        self.liquidity_overlay = liquidity_overlay or CounterfactualLiquidityOverlay()
        self._fallback_intent_sequence = 0

    def execute(
        self,
        intent: OrderIntent,
        *,
        decision_checkpoint: ArrivalBookCheckpoint | None,
        arrival_checkpoint: ArrivalBookCheckpoint | None,
        portfolio: PaperPortfolioSnapshot | None = None,
        arrival_ts_override: datetime | None = None,
        intent_sequence: int | None = None,
        fidelity_metadata: Mapping[str, Any] | None = None,
        depth_haircut_override: Decimal | None = None,
    ) -> PaperExecutionResult:
        audit_key = _audit_key(intent)
        if audit_key in self._results:
            return self._results[audit_key]
        arrival_ts = (
            arrival_ts_override
            if arrival_ts_override is not None
            else self.modeled_arrival_ts(intent, decision_checkpoint)
        )
        if arrival_ts < intent.decision_ts:
            raise ValueError("arrival_ts_override precedes decision_ts")
        rejection = self._preflight(
            intent, decision_checkpoint, arrival_checkpoint, arrival_ts, portfolio
        )
        if rejection is not None:
            status, reason = rejection
            return self._finish(
                audit_key,
                intent,
                status=status,
                reason=reason,
                arrival_ts=arrival_ts,
                decision_checkpoint=decision_checkpoint,
                arrival_checkpoint=arrival_checkpoint,
                fills=(),
                fidelity_metadata=fidelity_metadata,
            )
        assert arrival_checkpoint is not None

        depth_haircut = (
            min(Decimal("1"), max(Decimal("0"), depth_haircut_override))
            if depth_haircut_override is not None
            else Decimal("1")
            if arrival_checkpoint.coverage_grade in {"A_PLUS", "A"}
            else self.config.grade_b_depth_haircut
        )
        sequence = (
            int(intent_sequence)
            if intent_sequence is not None
            else self._next_fallback_intent_sequence()
        )
        levels = self._remaining_levels(arrival_checkpoint, intent.side)
        crossing = bool(levels) and _limit_allows(
            intent.side, levels[0].price, intent.limit_price
        )
        if intent.post_only and crossing:
            return self._finish(
                audit_key,
                intent,
                status="REJECTED",
                reason="post_only_crosses_arrival_book",
                arrival_ts=arrival_ts,
                decision_checkpoint=decision_checkpoint,
                arrival_checkpoint=arrival_checkpoint,
                fills=(),
                fidelity_metadata=fidelity_metadata,
            )

        candidates: list[tuple[PaperBookLevel, Decimal, int]] = []
        quote_request = intent.side == "BUY" and intent.amount_unit == "QUOTE"
        remaining = intent.size
        cash_remaining = (
            portfolio.cash_balance
            if portfolio is not None and intent.side == "BUY"
            else None
        )
        position_remaining = (
            portfolio.position_size
            if portfolio is not None and intent.side == "SELL"
            else None
        )
        risk_limit: str | None = None
        for index, level in enumerate(levels):
            if not _limit_allows(intent.side, level.price, intent.limit_price):
                break
            available_size = level.size * depth_haircut
            take = (
                min(available_size, remaining / level.price)
                if quote_request
                else min(remaining, available_size)
            )
            if cash_remaining is not None:
                unit_cost = level.price + _fee_per_share(
                    intent, level.price, self.config
                )
                affordable = (
                    cash_remaining / unit_cost if unit_cost > 0 else Decimal("0")
                )
                if affordable < take:
                    risk_limit = "paper_cash"
                take = min(take, affordable)
            if position_remaining is not None:
                if position_remaining < take:
                    risk_limit = "paper_position"
                take = min(take, position_remaining)
            if take <= 0:
                continue
            candidates.append((level, take, index))
            remaining -= take * level.price if quote_request else take
            if cash_remaining is not None:
                cash_remaining -= take * (
                    level.price + _fee_per_share(intent, level.price, self.config)
                )
            if position_remaining is not None:
                position_remaining -= take
            if remaining <= 0:
                break

        if intent.order_type == "FOK" and remaining > 0:
            return self._finish(
                audit_key,
                intent,
                status="REJECTED",
                reason=(
                    f"fok_insufficient_{risk_limit}"
                    if risk_limit is not None
                    else "fok_insufficient_arrival_depth"
                ),
                arrival_ts=arrival_ts,
                decision_checkpoint=decision_checkpoint,
                arrival_checkpoint=arrival_checkpoint,
                fills=(),
                fidelity_metadata=fidelity_metadata,
            )

        fills: list[PaperTakerFill] = []
        terminal_shortfall = Decimal("0")
        for level, size, index in candidates:
            allocated = self.liquidity_overlay.consume(
                intent_sequence=sequence,
                asset_id=arrival_checkpoint.asset_id,
                book_generation=arrival_checkpoint.generation,
                side=_book_side(intent.side),
                price_tick=level.price,
                displayed_size=self._displayed_size(
                    arrival_checkpoint,
                    intent.side,
                    level.price,
                ),
                requested_size=size,
                event_id=arrival_checkpoint.checkpoint_id,
                strategy_id=intent.strategy_id,
                account_id=intent.strategy_id,
                arrival_ts_ns=_datetime_ns(arrival_ts),
                deterministic_order_id=intent.client_order_id,
                strategy_priority=0,
            )
            unallocated = max(Decimal("0"), size - allocated)
            terminal_shortfall += (
                unallocated * level.price if quote_request else unallocated
            )
            if allocated <= 0:
                continue
            charge = calculate_fill_fee(
                intent,
                level.price,
                allocated,
                self.config,
                fill_id=f"{audit_key}:{index}",
                liquidity_role=LiquidityRole.TAKER,
            )
            fills.append(_paper_fill(level.price, allocated, index, charge))

        filled = sum((item.size for item in fills), Decimal("0"))
        remaining_after = max(Decimal("0"), remaining) + terminal_shortfall
        if intent.order_type == "FOK" and remaining_after > 0:
            release = getattr(self.liquidity_overlay, "release_order", None)
            if fills and release is None:
                raise RuntimeError(
                    "liquidity overlay cannot release an incomplete FOK allocation"
                )
            if release is not None:
                release(
                    intent.client_order_id,
                    reason="fok_terminal_allocation_shortfall",
                )
            return self._finish(
                audit_key,
                intent,
                status="REJECTED",
                reason="fok_insufficient_global_liquidity",
                arrival_ts=arrival_ts,
                decision_checkpoint=decision_checkpoint,
                arrival_checkpoint=arrival_checkpoint,
                fills=(),
                fidelity_metadata=fidelity_metadata,
            )
        if remaining_after == 0 and filled > 0:
            status: PaperStatus = "FILLED"
            reason = "arrival_book_walk_complete"
        elif intent.order_type in {"GTC", "GTD"}:
            status = "PARTIAL" if filled > 0 else "WORKING"
            reason = (
                "crossing_filled_remainder_resting"
                if filled > 0
                else "non_crossing_resting"
            )
        elif filled > 0:
            status = "PARTIAL"
            reason = (
                f"fak_{risk_limit}_limited"
                if risk_limit is not None
                else "fak_remainder_cancelled"
            )
        else:
            status = "CANCELLED"
            reason = "no_marketable_arrival_depth"
        return self._finish(
            audit_key,
            intent,
            status=status,
            reason=reason,
            arrival_ts=arrival_ts,
            decision_checkpoint=decision_checkpoint,
            arrival_checkpoint=arrival_checkpoint,
            fills=tuple(fills),
            fidelity_metadata=fidelity_metadata,
        )

    def modeled_arrival_ts(
        self,
        intent: OrderIntent,
        decision_checkpoint: ArrivalBookCheckpoint | None,
    ) -> datetime:
        arrival_ts = self.config.latency.arrival_ts(intent.decision_ts)
        if _is_marketable(intent, decision_checkpoint):
            arrival_ts += timedelta(milliseconds=max(0, intent.venue_taker_delay_ms))
        return arrival_ts

    def reject(
        self,
        intent: OrderIntent,
        *,
        reason: str,
        decision_checkpoint: ArrivalBookCheckpoint | None,
        arrival_checkpoint: ArrivalBookCheckpoint | None,
        status: PaperStatus = "REJECTED",
        fidelity_metadata: Mapping[str, Any] | None = None,
    ) -> PaperExecutionResult:
        """Create an audited terminal result for an upstream execution gate."""

        audit_key = _audit_key(intent)
        if audit_key in self._results:
            return self._results[audit_key]
        return self._finish(
            audit_key,
            intent,
            status=status,
            reason=reason,
            arrival_ts=self.config.latency.arrival_ts(intent.decision_ts),
            decision_checkpoint=decision_checkpoint,
            arrival_checkpoint=arrival_checkpoint,
            fills=(),
            fidelity_metadata=fidelity_metadata,
        )

    def _preflight(
        self,
        intent: OrderIntent,
        decision_checkpoint: ArrivalBookCheckpoint | None,
        checkpoint: ArrivalBookCheckpoint | None,
        arrival_ts: datetime,
        portfolio: PaperPortfolioSnapshot | None,
    ) -> tuple[PaperStatus, str] | None:
        if (
            not intent.strategy_id
            or not intent.client_order_id
            or not intent.asset_id
            or intent.side not in {"BUY", "SELL"}
            or intent.order_type not in {"GTC", "GTD", "FOK", "FAK"}
            or intent.amount_unit not in {"SHARES", "QUOTE"}
            or (intent.side == "SELL" and intent.amount_unit != "SHARES")
            or (intent.order_type in {"GTC", "GTD"} and intent.amount_unit != "SHARES")
            or intent.size <= 0
            or not Decimal("0") < intent.limit_price < Decimal("1")
        ):
            return "REJECTED", "invalid_order_intent"
        if intent.order_type == "GTD":
            expiration_error = validate_gtd_expiration(
                intent.decision_ts,
                intent.expires_at,
            )
            if expiration_error is not None:
                return "REJECTED", expiration_error
            assert intent.expires_at is not None
            if arrival_ts >= gtd_effective_expires_at(intent.expires_at):
                return "REJECTED", "gtd_expired_before_arrival"
        if intent.min_order_size is not None and intent.min_order_size > 0:
            order_shares = (
                intent.size / intent.limit_price
                if intent.side == "BUY" and intent.amount_unit == "QUOTE"
                else intent.size
            )
            if order_shares < intent.min_order_size:
                return "REJECTED", "size_below_market_min_order_size"
        if intent.tick_size is not None and intent.tick_size > 0:
            ticks = intent.limit_price / intent.tick_size
            if ticks != ticks.to_integral_value():
                return "REJECTED", "limit_price_not_aligned_to_tick_size"
        if portfolio is not None:
            if portfolio.cash_balance < 0 or portfolio.position_size < 0:
                return "REJECTED", "invalid_paper_portfolio_state"
            if intent.side == "BUY" and portfolio.cash_balance <= 0:
                return "REJECTED", "insufficient_paper_cash"
            if intent.side == "SELL" and portfolio.position_size <= 0:
                return "REJECTED", "insufficient_paper_position"
        if decision_checkpoint is None:
            return "DATA_NOT_READY", "decision_checkpoint_missing"
        if (
            decision_checkpoint.asset_id != intent.asset_id
            or decision_checkpoint.market_id != intent.market_id
            or decision_checkpoint.condition_id != intent.condition_id
        ):
            return "DATA_NOT_READY", "decision_checkpoint_identity_mismatch"
        if decision_checkpoint.observed_at > intent.decision_ts:
            return "DATA_NOT_READY", "decision_checkpoint_lookahead"
        if decision_checkpoint.market_state != "LIVE":
            return (
                "MARKET_NOT_TRADABLE",
                f"decision_market_state_{decision_checkpoint.market_state.lower()}",
            )
        if decision_checkpoint.has_gap:
            return "BOOK_GAP", "decision_checkpoint_has_gap"
        if decision_checkpoint.coverage_grade in {"C", "D"}:
            return (
                "DATA_NOT_READY",
                f"decision_coverage_grade_{decision_checkpoint.coverage_grade.lower()}",
            )
        if decision_checkpoint.book_status.upper() not in {
            "READY",
            "READY_HIGH",
            "READY_MEDIUM",
        }:
            return (
                "DATA_NOT_READY",
                f"decision_book_status_{decision_checkpoint.book_status.lower()}",
            )
        if decision_checkpoint.age_ms(intent.decision_ts) > max(
            1, int(self.config.max_book_age_ms)
        ):
            return "BOOK_STALE", "decision_book_too_old"
        if intent.side == "BUY" and not decision_checkpoint.asks:
            return "DATA_NOT_READY", "decision_book_missing_asks"
        if intent.side == "SELL" and not decision_checkpoint.bids:
            return "DATA_NOT_READY", "decision_book_missing_bids"
        if _book_is_crossed(decision_checkpoint):
            return "DATA_NOT_READY", "decision_book_crossed"
        if checkpoint is None:
            return "DATA_NOT_READY", "arrival_checkpoint_missing"
        if (
            checkpoint.asset_id != intent.asset_id
            or checkpoint.market_id != intent.market_id
            or checkpoint.condition_id != intent.condition_id
        ):
            return "DATA_NOT_READY", "arrival_checkpoint_identity_mismatch"
        if checkpoint.observed_at > arrival_ts:
            return "DATA_NOT_READY", "arrival_checkpoint_lookahead"
        if checkpoint.market_state not in {"LIVE"}:
            return (
                "MARKET_NOT_TRADABLE",
                f"market_state_{checkpoint.market_state.lower()}",
            )
        if checkpoint.has_gap:
            return "BOOK_GAP", "arrival_checkpoint_has_gap"
        if checkpoint.book_status.upper() not in {
            "READY",
            "READY_HIGH",
            "READY_MEDIUM",
        }:
            return "DATA_NOT_READY", f"book_status_{checkpoint.book_status.lower()}"
        if checkpoint.coverage_grade in {"C", "D"}:
            return (
                "DATA_NOT_READY",
                f"coverage_grade_{checkpoint.coverage_grade.lower()}",
            )
        if checkpoint.age_ms(arrival_ts) > max(1, int(self.config.max_book_age_ms)):
            return "BOOK_STALE", "arrival_book_too_old"
        if intent.side == "BUY" and not checkpoint.asks:
            return "DATA_NOT_READY", "arrival_book_missing_asks"
        if intent.side == "SELL" and not checkpoint.bids:
            return "DATA_NOT_READY", "arrival_book_missing_bids"
        if _book_is_crossed(checkpoint):
            return "DATA_NOT_READY", "arrival_book_crossed"
        return None

    def _remaining_levels(
        self, checkpoint: ArrivalBookCheckpoint, side: Side
    ) -> tuple[PaperBookLevel, ...]:
        raw = (
            sorted(checkpoint.asks, key=lambda item: item.price)
            if side == "BUY"
            else sorted(checkpoint.bids, key=lambda item: item.price, reverse=True)
        )
        book_side = _book_side(side)
        return tuple(
            PaperBookLevel(
                level.price,
                self.liquidity_overlay.available(
                    asset_id=checkpoint.asset_id,
                    book_generation=checkpoint.generation,
                    side=book_side,
                    price_tick=level.price,
                    displayed_size=level.size,
                    event_id=checkpoint.checkpoint_id,
                ),
            )
            for level in raw
        )

    @staticmethod
    def _displayed_size(
        checkpoint: ArrivalBookCheckpoint,
        side: Side,
        price: Decimal,
    ) -> Decimal:
        levels = checkpoint.asks if side == "BUY" else checkpoint.bids
        return next(
            (level.size for level in levels if level.price == price), Decimal("0")
        )

    def _next_fallback_intent_sequence(self) -> int:
        self._fallback_intent_sequence += 1
        return self._fallback_intent_sequence

    def _finish(
        self,
        audit_key: str,
        intent: OrderIntent,
        *,
        status: PaperStatus,
        reason: str,
        arrival_ts: datetime,
        decision_checkpoint: ArrivalBookCheckpoint | None,
        arrival_checkpoint: ArrivalBookCheckpoint | None,
        fills: tuple[PaperTakerFill, ...],
        fidelity_metadata: Mapping[str, Any] | None,
    ) -> PaperExecutionResult:
        filled = sum((item.size for item in fills), Decimal("0"))
        notional = sum((item.price * item.size for item in fills), Decimal("0"))
        remaining_amount = max(
            Decimal("0"),
            intent.size - (notional if intent.amount_unit == "QUOTE" else filled),
        )
        avg = notional / filled if filled > 0 else None
        reference = _reference_price(arrival_checkpoint, intent.side)
        slippage = (
            None
            if avg is None or reference is None
            else (avg - reference if intent.side == "BUY" else reference - avg)
        )
        result = PaperExecutionResult(
            audit_key=audit_key,
            status=status,
            reason=reason,
            intent=intent,
            arrival_ts=arrival_ts,
            decision_checkpoint_id=decision_checkpoint.checkpoint_id
            if decision_checkpoint
            else None,
            arrival_checkpoint_id=arrival_checkpoint.checkpoint_id
            if arrival_checkpoint
            else None,
            book_generation=arrival_checkpoint.generation
            if arrival_checkpoint
            else None,
            coverage_grade=arrival_checkpoint.coverage_grade
            if arrival_checkpoint
            else None,
            book_age_ms=arrival_checkpoint.age_ms(arrival_ts)
            if arrival_checkpoint
            else None,
            fills=fills,
            requested_amount=intent.size,
            amount_unit=intent.amount_unit,
            filled_size=filled,
            remaining_size=(
                max(Decimal("0"), intent.size - filled)
                if intent.amount_unit == "SHARES"
                else Decimal("0")
            ),
            filled_notional=notional,
            remaining_amount=remaining_amount,
            avg_fill_price=avg,
            total_fee=sum((item.fee for item in fills), Decimal("0")),
            slippage=slippage,
            source_manifest_ids=arrival_checkpoint.source_manifest_ids
            if arrival_checkpoint
            else (),
            source_files=arrival_checkpoint.source_files if arrival_checkpoint else (),
            source_event_start=arrival_checkpoint.source_event_start
            if arrival_checkpoint
            else None,
            source_event_end=arrival_checkpoint.source_event_end
            if arrival_checkpoint
            else None,
            rest_audit_at=arrival_checkpoint.rest_audit_at
            if arrival_checkpoint
            else None,
            model_version=self.config.model_version,
            config_hash=self.config.config_hash,
            fidelity=(
                dict(fidelity_metadata)
                if fidelity_metadata is not None
                else _default_fidelity(arrival_checkpoint, self.config)
            ),
        )
        self._results[audit_key] = result
        self.audit_sink.append(result)
        return result


def _datetime_ns(value: datetime) -> int:
    seconds = int(value.timestamp())
    return seconds * 1_000_000_000 + value.microsecond * 1_000


def _audit_key(intent: OrderIntent) -> str:
    raw = f"{intent.strategy_id}|{intent.client_order_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fee_per_share(
    intent: OrderIntent,
    price: Decimal,
    config: TakerExecutionConfig,
) -> Decimal:
    schedule = _fee_schedule(intent, config)
    base = max(Decimal(0), price * (Decimal(1) - price))
    platform = schedule.platform_fee_rate * (
        base**schedule.platform_fee_exponent
    )
    builder = (
        price
        * Decimal(schedule.builder_taker_fee_bps)
        / Decimal(10_000)
    )
    return platform + builder


def estimate_order_reservation(
    intent: OrderIntent,
    config: TakerExecutionConfig,
    *,
    remaining_size: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    """Return conservative cash/shares required for an unfilled paper order."""

    remaining = (
        intent.size if remaining_size is None else max(Decimal("0"), remaining_size)
    )
    if intent.side == "SELL":
        return Decimal("0"), remaining
    schedule = _fee_schedule(intent, config)
    liquidity_role = (
        LiquidityRole.MAKER if intent.post_only else LiquidityRole.TAKER
    )
    if intent.amount_unit == "QUOTE":
        notional = remaining
        charge = maximum_order_fees(
            schedule=schedule,
            liquidity_role=liquidity_role,
            limit_price=intent.limit_price,
            quote_notional=remaining,
        )
    else:
        shares = remaining
        notional = remaining * intent.limit_price
        charge = maximum_order_fees(
            schedule=schedule,
            liquidity_role=liquidity_role,
            limit_price=intent.limit_price,
            shares=shares,
        )
    return notional + charge.total_fee, Decimal("0")


def _fill_fee(
    intent: OrderIntent,
    price: Decimal,
    size: Decimal,
    config: TakerExecutionConfig,
) -> Decimal:
    return calculate_fill_fee(
        intent,
        price,
        size,
        config,
        fill_id="LEGACY_CALLER",
        liquidity_role=LiquidityRole.TAKER,
    ).total_fee


def maker_fill_fee(
    intent: OrderIntent,
    price: Decimal,
    size: Decimal,
    config: TakerExecutionConfig,
) -> Decimal:
    """Return the venue fee for a passive fill under the bound market terms."""

    return calculate_fill_fee(
        intent,
        price,
        size,
        config,
        fill_id="LEGACY_MAKER_CALLER",
        liquidity_role=LiquidityRole.MAKER,
    ).total_fee


def calculate_fill_fee(
    intent: OrderIntent,
    price: Decimal,
    size: Decimal,
    config: TakerExecutionConfig,
    *,
    fill_id: str,
    liquidity_role: LiquidityRole | str,
) -> FeeCharge:
    return FeeEngine.calculate(
        fill_id=fill_id,
        schedule=_fee_schedule(intent, config),
        liquidity_role=liquidity_role,
        price=price,
        shares=max(Decimal(0), size),
    )


def _fee_schedule(
    intent: OrderIntent,
    config: TakerExecutionConfig,
) -> FeeSchedule:
    if intent.fee_rate is None:
        rate = max(Decimal(0), config.fee_bps) / Decimal(10_000)
        exponent = Decimal(1)
        source = "CONFIG_FALLBACK_DYNAMIC_CURVE"
    else:
        rate = max(Decimal(0), intent.fee_rate)
        exponent = max(
            Decimal(0),
            intent.fee_exponent
            if intent.fee_exponent is not None
            else Decimal(1),
        )
        source = intent.fee_schedule_source or "BOUND_MARKET_TERMS"
    schedule_id = intent.fee_schedule_id or fee_schedule_id(
        asset_id=intent.asset_id,
        condition_id=intent.condition_id,
        effective_from=intent.decision_ts,
        platform_fee_rate=rate,
        platform_fee_exponent=exponent,
        platform_taker_only=intent.fee_taker_only,
        source=source,
    )
    return FeeSchedule(
        schedule_id=schedule_id,
        asset_id=intent.asset_id,
        condition_id=intent.condition_id,
        effective_from=intent.decision_ts,
        effective_until=None,
        platform_fee_rate=rate,
        platform_fee_exponent=exponent,
        platform_taker_only=intent.fee_taker_only,
        builder_code=intent.builder_code,
        builder_taker_fee_bps=intent.builder_taker_fee_bps,
        builder_maker_fee_bps=intent.builder_maker_fee_bps,
        economics_regime_id=intent.economics_regime_id,
        source=source,
    )


def _paper_fill(
    price: Decimal,
    size: Decimal,
    level_index: int,
    charge: FeeCharge,
) -> PaperTakerFill:
    return PaperTakerFill(
        price=price,
        size=size,
        fee=charge.total_fee,
        level_index=level_index,
        fee_charge_id=charge.fee_charge_id,
        fee_schedule_id=charge.fee_schedule_id,
        platform_fee_rate=charge.platform_fee_rate,
        platform_fee_exponent=charge.platform_fee_exponent,
        platform_fee=charge.platform_fee,
        builder_fee=charge.builder_fee,
        builder_fee_rate_bps=charge.builder_fee_rate_bps,
        rounding_policy=charge.rounding_policy,
        economics_regime_id=charge.economics_regime_id,
        fee_source=charge.source,
    )


def _book_side(order_side: Side) -> str:
    return "ASK" if order_side == "BUY" else "BID"


def _limit_allows(side: Side, book_price: Decimal, limit_price: Decimal) -> bool:
    return book_price <= limit_price if side == "BUY" else book_price >= limit_price


def _reference_price(
    checkpoint: ArrivalBookCheckpoint | None, side: Side
) -> Decimal | None:
    if checkpoint is None:
        return None
    levels = checkpoint.asks if side == "BUY" else checkpoint.bids
    if not levels:
        return None
    return (
        min(item.price for item in levels)
        if side == "BUY"
        else max(item.price for item in levels)
    )


def _book_is_crossed(checkpoint: ArrivalBookCheckpoint) -> bool:
    if not checkpoint.bids or not checkpoint.asks:
        return False
    return max(level.price for level in checkpoint.bids) >= min(
        level.price for level in checkpoint.asks
    )


def _is_marketable(
    intent: OrderIntent,
    checkpoint: ArrivalBookCheckpoint | None,
) -> bool:
    if checkpoint is None or intent.post_only:
        return False
    levels = checkpoint.asks if intent.side == "BUY" else checkpoint.bids
    if not levels:
        return False
    best = (
        min(level.price for level in levels)
        if intent.side == "BUY"
        else max(level.price for level in levels)
    )
    return _limit_allows(intent.side, best, intent.limit_price)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        observed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return observed.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _default_fidelity(
    checkpoint: ArrivalBookCheckpoint | None,
    config: TakerExecutionConfig,
) -> dict[str, Any]:
    """Ensure direct engine callers still emit an explicit conservative label."""

    from quant.simulator.fidelity import FidelityInputs, assess_fidelity

    data_safe = bool(
        checkpoint
        and checkpoint.coverage_grade in {"A_PLUS", "A"}
        and checkpoint.book_status.upper() in {"READY", "READY_HIGH", "READY_MEDIUM"}
        and not checkpoint.has_gap
    )
    return assess_fidelity(
        FidelityInputs(
            data_quality_grade=checkpoint.coverage_grade if checkpoint else None,
            data_safe=data_safe,
            venue_emulated=False,
            capacity_safe=False,
            taker_calibrated_in_domain=False,
            maker_calibrated_in_domain=False,
            impact_aware_scenario=False,
            capacity_status="NOT_EVALUATED",
            venue_regime_id="UNBOUND",
            fill_model_version=config.model_version,
            latency_model_version="paper_latency_model_v1",
            finality_model_version="UNBOUND",
            valuation_model_version="UNBOUND",
            calibration_domain_status="UNCALIBRATED",
        )
    ).as_dict()
