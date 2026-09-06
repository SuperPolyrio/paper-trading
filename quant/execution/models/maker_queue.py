"""Conservative L2 maker queue models with probabilistic output."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from math import exp


class QueueModel(str, Enum):
    STRICT_TRADE_EVIDENCE = "STRICT_TRADE_EVIDENCE"
    RISK_AVERSE_QUEUE = "RISK_AVERSE_QUEUE"
    PROBABILISTIC_QUEUE = "PROBABILISTIC_QUEUE"


@dataclass(frozen=True)
class MakerQueueState:
    paper_order_id: str
    asset_id: str
    side: str
    price_tick: Decimal
    queue_model_version: str
    displayed_size_at_accept: Decimal
    own_orders_ahead: Decimal
    estimated_external_queue_ahead: Decimal
    order_size: Decimal
    cumulative_trade_volume_at_price: Decimal = Decimal("0")
    cumulative_cancel_ahead_estimate: Decimal = Decimal("0")
    last_event_id: str = ""
    book_generation: int | None = None
    queue_epoch: int = 0
    last_event_ts_ns: int | None = None

    @property
    def effective_queue_ahead(self) -> Decimal:
        return max(
            Decimal("0"),
            self.own_orders_ahead
            + self.estimated_external_queue_ahead
            - self.cumulative_trade_volume_at_price
            - self.cumulative_cancel_ahead_estimate,
        )


@dataclass(frozen=True)
class MakerFillPrediction:
    p_no_fill: Decimal
    p_partial: Decimal
    p_full: Decimal
    expected_filled_size: Decimal
    filled_size_p10: Decimal
    filled_size_p50: Decimal
    filled_size_p90: Decimal
    expected_time_to_first_fill_seconds: Decimal | None
    expected_time_to_full_fill_seconds: Decimal | None
    confidence: Decimal
    domain_status: str
    fill_probability: Decimal
    expected_time_to_fill_seconds: Decimal | None
    time_to_fill_p90_seconds: Decimal | None
    queue_ahead_estimate: Decimal
    cancel_ahead_probability: Decimal
    model_confidence: Decimal
    calibration_domain: str
    aggressor_arrival_probability: Decimal = Decimal("1")


@dataclass(frozen=True)
class MakerQueueAdvance:
    state: MakerQueueState
    incremental_fill_size: Decimal
    cumulative_filled_size: Decimal


class MakerQueueEngine:
    def __init__(
        self,
        model: QueueModel,
        *,
        cancel_ahead_probability: Decimal = Decimal("0.25"),
    ) -> None:
        self.model = model
        self.cancel_ahead_probability = min(
            Decimal("1"), max(Decimal("0"), cancel_ahead_probability)
        )

    def on_trade(
        self,
        state: MakerQueueState,
        *,
        aggressor_side: str | None,
        volume: Decimal,
        event_id: str,
        event_ts_ns: int | None = None,
    ) -> MakerQueueState:
        if event_id == state.last_event_id:
            return state
        if (
            event_ts_ns is not None
            and state.last_event_ts_ns is not None
            and event_ts_ns < state.last_event_ts_ns
        ):
            return state
        correct_aggressor = aggressor_side is not None and (
            (state.side.upper() == "BUY" and aggressor_side.upper() == "SELL")
            or (state.side.upper() == "SELL" and aggressor_side.upper() == "BUY")
        )
        if not correct_aggressor:
            return replace(
                state,
                last_event_id=event_id,
                last_event_ts_ns=event_ts_ns or state.last_event_ts_ns,
            )
        return replace(
            state,
            cumulative_trade_volume_at_price=(
                state.cumulative_trade_volume_at_price + max(Decimal("0"), volume)
            ),
            last_event_id=event_id,
            last_event_ts_ns=event_ts_ns or state.last_event_ts_ns,
        )

    def rebase(
        self,
        state: MakerQueueState,
        *,
        book_generation: int,
        displayed_external_queue: Decimal,
        event_id: str,
        event_ts_ns: int,
    ) -> MakerQueueState:
        """Start a conservative queue epoch after a new full book baseline."""

        if state.book_generation == int(book_generation):
            return state
        return replace(
            state,
            displayed_size_at_accept=max(Decimal("0"), displayed_external_queue),
            estimated_external_queue_ahead=max(
                Decimal("0"), displayed_external_queue
            ),
            cumulative_trade_volume_at_price=Decimal("0"),
            cumulative_cancel_ahead_estimate=Decimal("0"),
            last_event_id=str(event_id),
            book_generation=int(book_generation),
            queue_epoch=max(0, int(state.queue_epoch)) + 1,
            last_event_ts_ns=int(event_ts_ns),
        )

    def advance_trade(
        self,
        state: MakerQueueState,
        *,
        aggressor_side: str | None,
        volume: Decimal,
        event_id: str,
        event_ts_ns: int | None = None,
        cumulative_filled_size: Decimal = Decimal("0"),
    ) -> MakerQueueAdvance:
        """Advance one FIFO queue from trade evidence and return only the new fill."""

        already_filled = min(
            state.order_size,
            max(Decimal("0"), cumulative_filled_size),
        )
        next_state = self.on_trade(
            state,
            aggressor_side=aggressor_side,
            volume=volume,
            event_id=event_id,
            event_ts_ns=event_ts_ns,
        )
        if next_state == state:
            return MakerQueueAdvance(next_state, Decimal("0"), already_filled)
        queue_at_accept = max(
            Decimal("0"),
            state.own_orders_ahead
            + state.estimated_external_queue_ahead
            - state.cumulative_cancel_ahead_estimate,
        )
        eligible = min(
            state.order_size,
            max(
                Decimal("0"),
                next_state.cumulative_trade_volume_at_price - queue_at_accept,
            ),
        )
        return MakerQueueAdvance(
            next_state,
            max(Decimal("0"), eligible - already_filled),
            max(already_filled, eligible),
        )

    def on_book_decrease(
        self,
        state: MakerQueueState,
        *,
        decrease: Decimal,
        event_id: str,
        event_ts_ns: int | None = None,
    ) -> MakerQueueState:
        if event_id == state.last_event_id:
            return state
        if (
            event_ts_ns is not None
            and state.last_event_ts_ns is not None
            and event_ts_ns < state.last_event_ts_ns
        ):
            return state
        cancel_ahead = Decimal("0")
        if self.model == QueueModel.PROBABILISTIC_QUEUE:
            position_ratio = state.effective_queue_ahead / max(
                Decimal("0.00000001"), state.displayed_size_at_accept
            )
            cancel_ahead = (
                max(Decimal("0"), decrease)
                * self.cancel_ahead_probability
                * min(Decimal("1"), position_ratio)
            )
        return replace(
            state,
            cumulative_cancel_ahead_estimate=(
                state.cumulative_cancel_ahead_estimate + cancel_ahead
            ),
            last_event_id=event_id,
            last_event_ts_ns=event_ts_ns or state.last_event_ts_ns,
        )

    def predict(
        self,
        state: MakerQueueState,
        *,
        forecast_trade_volume: Decimal,
        horizon_seconds: Decimal,
        aggressor_arrival_probability: Decimal | None = None,
    ) -> MakerFillPrediction:
        ahead = state.effective_queue_ahead
        projected = max(Decimal("0"), forecast_trade_volume)
        executable = max(Decimal("0"), projected - ahead)
        fill_ratio = min(
            Decimal("1"), executable / max(state.order_size, Decimal("0.00000001"))
        )
        if self.model == QueueModel.STRICT_TRADE_EVIDENCE:
            p_full = Decimal("1") if fill_ratio == 1 else Decimal("0")
            p_partial = (
                Decimal("1")
                if Decimal("0") < fill_ratio < Decimal("1")
                else Decimal("0")
            )
            confidence = Decimal("0.9")
        elif self.model == QueueModel.RISK_AVERSE_QUEUE:
            p_full = fill_ratio * Decimal("0.75")
            p_partial = min(Decimal("1") - p_full, fill_ratio * Decimal("0.25"))
            confidence = Decimal("0.65")
        else:
            p_full = fill_ratio * Decimal("0.6")
            p_partial = min(Decimal("1") - p_full, fill_ratio * Decimal("0.35"))
            confidence = Decimal("0.4")
        arrival_probability = (
            Decimal("1")
            if aggressor_arrival_probability is None
            else _probability(aggressor_arrival_probability)
        )
        if self.model != QueueModel.STRICT_TRADE_EVIDENCE:
            p_full *= arrival_probability
            p_partial *= arrival_probability
        p_no_fill = max(Decimal("0"), Decimal("1") - p_full - p_partial)
        expected = state.order_size * (p_full + p_partial * Decimal("0.5"))
        first = (
            horizon_seconds * (ahead / max(projected, Decimal("0.00000001")))
            if executable > 0
            else None
        )
        full = horizon_seconds if p_full > 0 else None
        return MakerFillPrediction(
            p_no_fill=p_no_fill,
            p_partial=p_partial,
            p_full=p_full,
            expected_filled_size=expected,
            filled_size_p10=Decimal("0"),
            filled_size_p50=state.order_size if p_full >= Decimal("0.5") else expected,
            filled_size_p90=min(state.order_size, expected * Decimal("1.5")),
            expected_time_to_first_fill_seconds=first,
            expected_time_to_full_fill_seconds=full,
            confidence=confidence,
            domain_status="RESEARCH_UNCALIBRATED",
            fill_probability=p_full + p_partial,
            expected_time_to_fill_seconds=first,
            time_to_fill_p90_seconds=full,
            queue_ahead_estimate=ahead,
            cancel_ahead_probability=(
                self.cancel_ahead_probability
                if self.model == QueueModel.PROBABILISTIC_QUEUE
                else Decimal("0")
            ),
            model_confidence=confidence,
            calibration_domain="OFFLINE_L2_TRADE_EVIDENCE_REQUIRED",
            aggressor_arrival_probability=arrival_probability,
        )


def poisson_arrival_probability(
    *,
    observed_count: Decimal | int,
    lookback_seconds: Decimal | int,
    horizon_seconds: Decimal | int,
    rate_multiplier: Decimal = Decimal("1"),
) -> Decimal:
    """Estimate at least one compatible aggressor arrival under a Poisson rate."""

    count = max(Decimal("0"), Decimal(str(observed_count)))
    lookback = max(Decimal("0.000001"), Decimal(str(lookback_seconds)))
    horizon = max(Decimal("0"), Decimal(str(horizon_seconds)))
    multiplier = max(Decimal("0"), Decimal(str(rate_multiplier)))
    expected_arrivals = count * multiplier * horizon / lookback
    if expected_arrivals <= 0:
        return Decimal("0")
    return _probability(Decimal(str(1 - exp(-float(expected_arrivals)))))


def _probability(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value))
