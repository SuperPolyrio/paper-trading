"""Simulator operational metrics and scoped automatic degradation.

This is a deterministic control-plane model, not a replacement for the LOB
collector.  Workers pass their observed counters and fault signals into it;
the result makes the scope explicit so a token-local book gap cannot degrade
unrelated tokens, while account-level accounting faults still request a global
kill switch.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from math import ceil


class DegradationScope(str, Enum):
    TOKEN = "TOKEN"
    MODEL = "MODEL"
    ACCOUNT = "ACCOUNT"


class TokenDataState(str, Enum):
    REDUNDANT = "REDUNDANT"
    SINGLE_FEED_SAFE = "SINGLE_FEED_SAFE"
    DATA_UNSAFE = "DATA_UNSAFE"


class ModelState(str, Enum):
    MODEL_VALID = "MODEL_VALID"
    STREAM_RECONCILING = "STREAM_RECONCILING"
    MODEL_STALE = "MODEL_STALE"
    MODEL_DISABLED = "MODEL_DISABLED"


class CapacityState(str, Enum):
    CAPACITY_SAFE = "CAPACITY_SAFE"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"


class LedgerState(str, Enum):
    CONFIRMED_LEDGER_OK = "CONFIRMED_LEDGER_OK"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class SimulatorMetricsInput:
    event_queue_ages_ms: tuple[float, ...] = ()
    inflight_command_ages_ms: tuple[float, ...] = ()
    rate_limit_throttle_count: int = 0
    heartbeat_auto_cancel_count: int = 0
    venue_mode_durations_seconds: Mapping[str, float] | None = None
    liquidity_overlay_contention: int = 0
    capacity_exceeded_order_count: int = 0
    self_trade_prevented_count: int = 0
    fill_void_count: int = 0
    ledger_reversal_count: int = 0
    provisional_confirmed_nav_gap: float = 0
    illiquid_unmarked_value: float = 0
    benchmark_regression_count: int = 0
    venue_contract_drift_count: int = 0


@dataclass(frozen=True)
class DegradationTransition:
    scope: DegradationScope
    identifier: str
    previous_state: str
    state: str
    signal: str
    requires_global_kill_switch: bool


def build_simulator_metrics(source: SimulatorMetricsInput) -> dict[str, object]:
    """Produce stable SLO field names from worker-local counters."""

    event_ages = tuple(max(0.0, float(value)) for value in source.event_queue_ages_ms)
    inflight_ages = tuple(
        max(0.0, float(value)) for value in source.inflight_command_ages_ms
    )
    return {
        "sim_event_queue_age_p50_ms": _percentile(event_ages, 0.50),
        "sim_event_queue_age_p95_ms": _percentile(event_ages, 0.95),
        "sim_event_queue_age_p99_ms": _percentile(event_ages, 0.99),
        "inflight_command_count": len(inflight_ages),
        "inflight_command_age_p99_ms": _percentile(inflight_ages, 0.99),
        "rate_limit_throttle_count": max(0, int(source.rate_limit_throttle_count)),
        "heartbeat_auto_cancel_count": max(0, int(source.heartbeat_auto_cancel_count)),
        "venue_mode_duration_seconds": dict(source.venue_mode_durations_seconds or {}),
        "liquidity_overlay_contention": max(
            0, int(source.liquidity_overlay_contention)
        ),
        "capacity_exceeded_order_count": max(
            0, int(source.capacity_exceeded_order_count)
        ),
        "self_trade_prevented_count": max(0, int(source.self_trade_prevented_count)),
        "fill_void_count": max(0, int(source.fill_void_count)),
        "ledger_reversal_count": max(0, int(source.ledger_reversal_count)),
        "provisional_confirmed_nav_gap": float(source.provisional_confirmed_nav_gap),
        "illiquid_unmarked_value": float(source.illiquid_unmarked_value),
        "benchmark_regression_count": max(0, int(source.benchmark_regression_count)),
        "venue_contract_drift_count": max(0, int(source.venue_contract_drift_count)),
    }


class DegradationController:
    """Fail closed by scope, with an auditable monotonic fault journal."""

    def __init__(self) -> None:
        self._states: dict[tuple[DegradationScope, str], str] = {}
        self._transitions: list[DegradationTransition] = []

    @property
    def transitions(self) -> tuple[DegradationTransition, ...]:
        return tuple(self._transitions)

    def state_for(self, scope: DegradationScope, identifier: str) -> str:
        key = (scope, str(identifier))
        if key in self._states:
            return self._states[key]
        return _initial_state(scope)

    def apply(
        self, *, scope: DegradationScope, identifier: str, signal: str
    ) -> DegradationTransition:
        """Apply one validated signal and retain an immutable state transition."""

        identifier = str(identifier)
        signal = str(signal).upper()
        previous = self.state_for(scope, identifier)
        next_state, global_kill = _transition(scope, previous, signal)
        self._states[(scope, identifier)] = next_state
        transition = DegradationTransition(
            scope=scope,
            identifier=identifier,
            previous_state=previous,
            state=next_state,
            signal=signal,
            requires_global_kill_switch=global_kill,
        )
        self._transitions.append(transition)
        return transition

    def restore(self, transition: DegradationTransition) -> None:
        """Restore the latest durable state without creating a new transition."""

        allowed = {
            DegradationScope.TOKEN: {state.value for state in TokenDataState},
            DegradationScope.MODEL: {state.value for state in ModelState},
            DegradationScope.ACCOUNT: {state.value for state in CapacityState}
            | {state.value for state in LedgerState},
        }[transition.scope]
        if transition.state not in allowed:
            raise ValueError(
                "invalid persisted degradation state: "
                f"{transition.scope.value}:{transition.state}"
            )
        self._states[(transition.scope, transition.identifier)] = transition.state

    def snapshot(self) -> dict[str, object]:
        counts = Counter(state for state in self._states.values())
        return {
            "states": [
                {
                    "scope": scope.value,
                    "identifier": identifier,
                    "state": state,
                }
                for (scope, identifier), state in sorted(
                    self._states.items(),
                    key=lambda item: (item[0][0].value, item[0][1]),
                )
            ],
            "state_counts": dict(sorted(counts.items())),
            "global_kill_switch_recommended": any(
                item.requires_global_kill_switch for item in self._transitions
            ),
            "transition_count": len(self._transitions),
            "transitions": [
                asdict(item) | {"scope": item.scope.value} for item in self._transitions
            ],
        }

    def admission_reasons(
        self,
        *,
        token_id: str,
        model_id: str,
        account_id: str,
    ) -> tuple[str, ...]:
        """Return only states that must block this specific paper command."""

        scoped = (
            (
                DegradationScope.TOKEN,
                str(token_id),
                {
                    TokenDataState.REDUNDANT.value,
                    TokenDataState.SINGLE_FEED_SAFE.value,
                },
            ),
            (
                DegradationScope.MODEL,
                str(model_id),
                {ModelState.MODEL_VALID.value},
            ),
            (
                DegradationScope.ACCOUNT,
                str(account_id),
                {
                    LedgerState.CONFIRMED_LEDGER_OK.value,
                    CapacityState.CAPACITY_SAFE.value,
                },
            ),
        )
        return tuple(
            f"{scope.value.lower()}:{identifier}:{state}"
            for scope, identifier, allowed in scoped
            if (state := self.state_for(scope, identifier)) not in allowed
        )


def _initial_state(scope: DegradationScope) -> str:
    return {
        DegradationScope.TOKEN: TokenDataState.REDUNDANT.value,
        DegradationScope.MODEL: ModelState.MODEL_VALID.value,
        DegradationScope.ACCOUNT: LedgerState.CONFIRMED_LEDGER_OK.value,
    }[scope]


def _transition(
    scope: DegradationScope, previous: str, signal: str
) -> tuple[str, bool]:
    if scope == DegradationScope.TOKEN:
        transitions = {
            "PRIMARY_FEED_LOST": TokenDataState.SINGLE_FEED_SAFE.value,
            "SECONDARY_FEED_LOST": TokenDataState.SINGLE_FEED_SAFE.value,
            "BOOK_GAP": TokenDataState.DATA_UNSAFE.value,
            "ALL_FEEDS_LOST": TokenDataState.DATA_UNSAFE.value,
            "BOOK_REBUILT_SINGLE_FEED": TokenDataState.SINGLE_FEED_SAFE.value,
            "REDUNDANCY_RESTORED": TokenDataState.REDUNDANT.value,
        }
        return transitions.get(signal, previous), False
    if scope == DegradationScope.MODEL:
        transitions = {
            "STREAM_DISCONNECTED": ModelState.STREAM_RECONCILING.value,
            "STREAM_RECONCILED": ModelState.MODEL_VALID.value,
            "DRIFT_DETECTED": ModelState.MODEL_STALE.value,
            "CALIBRATION_INVALID": ModelState.MODEL_DISABLED.value,
            "RECALIBRATED": ModelState.MODEL_VALID.value,
        }
        return transitions.get(signal, previous), False
    transitions = {
        "CAPACITY_EXCEEDED": CapacityState.CAPACITY_EXCEEDED.value,
        "CAPACITY_RECOVERED": CapacityState.CAPACITY_SAFE.value,
        "LEDGER_MISMATCH": LedgerState.RECONCILIATION_REQUIRED.value,
        "UNKNOWN_LIVE_ORDER": LedgerState.RECONCILIATION_REQUIRED.value,
        "HEARTBEAT_FAILURE": LedgerState.RECONCILIATION_REQUIRED.value,
        "LEDGER_RECONCILED": LedgerState.CONFIRMED_LEDGER_OK.value,
    }
    next_state = transitions.get(signal, previous)
    return next_state, signal in {
        "LEDGER_MISMATCH",
        "UNKNOWN_LIVE_ORDER",
        "HEARTBEAT_FAILURE",
    }


def _percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    # Nearest-rank keeps a small-sample p99 conservative instead of silently
    # reporting the second-largest observation as the tail.
    index = max(0, min(len(ordered) - 1, ceil(len(ordered) * quantile) - 1))
    return ordered[index]
