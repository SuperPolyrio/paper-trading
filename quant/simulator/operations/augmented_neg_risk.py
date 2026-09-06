"""Versioned augmented negative-risk outcomes and atomic conversion intents."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from .domain import PositionOperationIntent, PositionOperationType


class AugmentedOutcomeKind(str, Enum):
    NAMED = "NAMED"
    PLACEHOLDER = "PLACEHOLDER"
    OTHER = "OTHER"


@dataclass(frozen=True)
class AugmentedOutcome:
    outcome_id: str
    label: str
    condition_id: str
    yes_asset_id: str
    no_asset_id: str
    kind: AugmentedOutcomeKind
    active: bool = True
    tradeable: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(
            not str(value).strip()
            for value in (
                self.outcome_id,
                self.label,
                self.condition_id,
                self.yes_asset_id,
                self.no_asset_id,
            )
        ):
            raise ValueError("augmented outcome identity is incomplete")
        if self.yes_asset_id == self.no_asset_id:
            raise ValueError("YES and NO assets must be distinct")
        if self.kind in {
            AugmentedOutcomeKind.PLACEHOLDER,
            AugmentedOutcomeKind.OTHER,
        } and self.tradeable:
            raise ValueError("only named augmented outcomes can be tradeable")


@dataclass(frozen=True)
class AugmentedNegRiskEvent:
    event_key: str
    version: int
    effective_ts: datetime
    outcomes: tuple[AugmentedOutcome, ...]
    source: str
    source_event_id: str
    raw_payload_hash: str
    rule_version: str = "augmented-neg-risk-v1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_key or not self.source or not self.source_event_id:
            raise ValueError("augmented event identity is incomplete")
        if self.version <= 0:
            raise ValueError("augmented event version must be positive")
        if self.effective_ts.tzinfo is None:
            raise ValueError("augmented event timestamp must be timezone-aware")
        if len(self.outcomes) < 2:
            raise ValueError("augmented event requires at least two outcomes")
        if sum(item.kind is AugmentedOutcomeKind.OTHER for item in self.outcomes) != 1:
            raise ValueError("augmented event requires exactly one explicit Other")
        for field_name in (
            "outcome_id",
            "condition_id",
            "yes_asset_id",
            "no_asset_id",
        ):
            values = [str(getattr(item, field_name)) for item in self.outcomes]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate augmented outcome {field_name}")

    @property
    def version_id(self) -> str:
        return f"{self.event_key}:v{self.version}"

    def outcome(self, outcome_id: str) -> AugmentedOutcome:
        for item in self.outcomes:
            if item.outcome_id == outcome_id:
                return item
        raise LookupError(f"augmented outcome not found: {outcome_id}")

    def tradeable_assets(self) -> frozenset[str]:
        return frozenset(
            asset
            for outcome in self.outcomes
            if outcome.active and outcome.tradeable
            for asset in (outcome.yes_asset_id, outcome.no_asset_id)
        )


@dataclass(frozen=True)
class AugmentedConversionMatrix:
    matrix_id: str
    event_version_id: str
    source_outcome_id: str
    token_deltas_per_unit: Mapping[str, Decimal]
    conservation_by_winner: Mapping[str, Decimal]
    rule_version: str = "augmented-neg-risk-v1"


@dataclass(frozen=True)
class AugmentedConversionReconciliation:
    reconciliation_id: str
    operation_id: str
    transaction_hash: str | None
    status: str
    expected_token_deltas: Mapping[str, Decimal]
    observed_token_deltas: Mapping[str, Decimal]
    amount_delta: Decimal
    reconciled_at: datetime
    source_event_id: str
    raw_payload_hash: str


AUGMENTED_NEG_RISK_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_augmented_neg_risk_events (
        event_version_id TEXT PRIMARY KEY,
        event_key TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        effective_ts TIMESTAMPTZ NOT NULL,
        source TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        raw_payload_hash TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (event_key,version),
        UNIQUE (source,source_event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_augmented_neg_risk_outcomes (
        event_version_id TEXT NOT NULL,
        outcome_id TEXT NOT NULL,
        label TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        yes_asset_id TEXT NOT NULL,
        no_asset_id TEXT NOT NULL,
        outcome_kind TEXT NOT NULL,
        active BOOLEAN NOT NULL,
        tradeable BOOLEAN NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        PRIMARY KEY (event_version_id,outcome_id),
        UNIQUE (event_version_id,condition_id),
        UNIQUE (event_version_id,yes_asset_id),
        UNIQUE (event_version_id,no_asset_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_augmented_conversion_matrices (
        matrix_id TEXT PRIMARY KEY,
        event_version_id TEXT NOT NULL,
        source_outcome_id TEXT NOT NULL,
        token_deltas_per_unit JSONB NOT NULL,
        conservation_by_winner JSONB NOT NULL,
        rule_version TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (event_version_id,source_outcome_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_augmented_conversion_reconciliations (
        reconciliation_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        transaction_hash TEXT,
        status TEXT NOT NULL,
        expected_token_deltas JSONB NOT NULL,
        observed_token_deltas JSONB NOT NULL,
        amount_delta NUMERIC NOT NULL,
        reconciled_at TIMESTAMPTZ NOT NULL,
        source_event_id TEXT NOT NULL,
        raw_payload_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (source_event_id)
    )
    """,
)


def clarify_placeholder(
    event: AugmentedNegRiskEvent,
    *,
    placeholder_outcome_id: str,
    label: str,
    effective_ts: datetime,
    source_event_id: str,
    raw_payload_hash: str,
) -> AugmentedNegRiskEvent:
    current = event.outcome(placeholder_outcome_id)
    if current.kind is not AugmentedOutcomeKind.PLACEHOLDER:
        raise ValueError("only placeholder outcomes can be clarified")
    outcomes = tuple(
        replace(item, label=label, kind=AugmentedOutcomeKind.NAMED, tradeable=True)
        if item.outcome_id == placeholder_outcome_id
        else item
        for item in event.outcomes
    )
    return AugmentedNegRiskEvent(
        event_key=event.event_key,
        version=event.version + 1,
        effective_ts=effective_ts,
        outcomes=outcomes,
        source=event.source,
        source_event_id=source_event_id,
        raw_payload_hash=raw_payload_hash,
        rule_version=event.rule_version,
        metadata={
            **dict(event.metadata),
            "clarified_outcome_id": placeholder_outcome_id,
            "previous_version_id": event.version_id,
        },
    )


def is_augmented_neg_risk_event(payload: Mapping[str, Any]) -> bool:
    trading = payload.get("trading")
    trading_data = trading if isinstance(trading, Mapping) else payload
    enable = trading_data.get("enableNegRisk")
    if enable is None:
        enable = trading_data.get("enable_neg_risk")
    augmented = trading_data.get("negRiskAugmented")
    if augmented is None:
        augmented = trading_data.get("neg_risk_augmented")
    return _bool(enable) and _bool(augmented)


def normalize_augmented_event_payload(
    payload: Mapping[str, Any],
    *,
    version: int,
    effective_ts: datetime,
    source: str = "GAMMA_EVENT",
    source_event_id: str | None = None,
) -> AugmentedNegRiskEvent:
    """Normalize an official Gamma event into immutable augmented outcomes."""

    if not is_augmented_neg_risk_event(payload):
        raise ValueError("event is not marked as augmented negative risk")
    event_key = _text(payload.get("id") or payload.get("slug"))
    if not event_key:
        raise ValueError("augmented Gamma event id is missing")
    raw_markets = payload.get("markets")
    if not isinstance(raw_markets, list):
        raise ValueError("augmented Gamma event markets are missing")
    outcomes: list[AugmentedOutcome] = []
    for raw in raw_markets:
        if not isinstance(raw, Mapping):
            continue
        condition_id = _text(raw.get("conditionId") or raw.get("condition_id"))
        market_id = _text(raw.get("id") or condition_id)
        label = _text(
            raw.get("groupItemTitle")
            or raw.get("group_item_title")
            or raw.get("shortTitle")
            or raw.get("question")
            or raw.get("title")
        )
        token_ids = _list(raw.get("clobTokenIds") or raw.get("clob_token_ids"))
        token_names = [str(item).strip().upper() for item in _list(raw.get("outcomes"))]
        if not all((condition_id, market_id, label)) or len(token_ids) != 2:
            raise ValueError("augmented market identity or binary token pair is incomplete")
        if token_names and token_names != ["YES", "NO"]:
            raise ValueError("augmented market token order must be YES then NO")
        slug = str(raw.get("slug") or "").strip().lower()
        lower_label = label.lower()
        is_other = _bool(raw.get("negRiskOther") or raw.get("neg_risk_other")) or (
            lower_label == "other"
        )
        is_placeholder = _bool(
            raw.get("isPlaceholder") or raw.get("is_placeholder")
        ) or slug.startswith("trade-indexer-placeholder-") or lower_label.startswith(
            "trade indexer placeholder"
        )
        kind = (
            AugmentedOutcomeKind.OTHER
            if is_other
            else AugmentedOutcomeKind.PLACEHOLDER
            if is_placeholder
            else AugmentedOutcomeKind.NAMED
        )
        outcomes.append(
            AugmentedOutcome(
                outcome_id=market_id,
                label=label,
                condition_id=condition_id,
                yes_asset_id=str(token_ids[0]),
                no_asset_id=str(token_ids[1]),
                kind=kind,
                active=_bool_default(raw.get("active"), True),
                tradeable=(
                    kind is AugmentedOutcomeKind.NAMED
                    and not _bool_default(raw.get("closed"), False)
                    and _bool_default(raw.get("acceptingOrders"), True)
                ),
                metadata={
                    "market_slug": raw.get("slug"),
                    "neg_risk_other": is_other,
                },
            )
        )
    raw_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    return AugmentedNegRiskEvent(
        event_key=event_key,
        version=version,
        effective_ts=effective_ts,
        outcomes=tuple(outcomes),
        source=source,
        source_event_id=source_event_id
        or _stable_id(
            "gamma-augmented-event",
            {"event_key": event_key, "raw_payload_hash": raw_hash},
        ),
        raw_payload_hash=raw_hash,
        metadata={"event_slug": payload.get("slug")},
    )
def build_conversion_matrix(
    event: AugmentedNegRiskEvent, source_outcome_id: str
) -> AugmentedConversionMatrix:
    source = event.outcome(source_outcome_id)
    if not source.active:
        raise ValueError("cannot convert an inactive augmented outcome")
    deltas: dict[str, Decimal] = {source.no_asset_id: Decimal("-1")}
    for outcome in event.outcomes:
        if outcome.active and outcome.outcome_id != source_outcome_id:
            deltas[outcome.yes_asset_id] = Decimal("1")
    conservation = _conservation(event, source_outcome_id, deltas)
    if any(value != 0 for value in conservation.values()):
        raise ValueError("augmented conversion violates collateral/token conservation")
    matrix_id = _stable_id(
        "augmented-conversion-matrix",
        {"event_version_id": event.version_id, "source": source_outcome_id},
    )
    return AugmentedConversionMatrix(
        matrix_id=matrix_id,
        event_version_id=event.version_id,
        source_outcome_id=source_outcome_id,
        token_deltas_per_unit=deltas,
        conservation_by_winner=conservation,
        rule_version=event.rule_version,
    )


def build_augmented_conversion_intent(
    event: AugmentedNegRiskEvent,
    *,
    source_outcome_id: str,
    amount: Decimal,
    account_id: str,
    strategy_id: str,
    decision_ts: datetime,
    operation_id: str | None = None,
) -> PositionOperationIntent:
    matrix = build_conversion_matrix(event, source_outcome_id)
    quantity = Decimal(amount)
    if quantity <= 0:
        raise ValueError("augmented conversion amount must be positive")
    token_deltas = {
        asset_id: per_unit * quantity
        for asset_id, per_unit in matrix.token_deltas_per_unit.items()
    }
    event_id = operation_id or _stable_id(
        "augmented-conversion-operation",
        {
            "account_id": account_id,
            "amount": str(quantity),
            "decision_ts": decision_ts.isoformat(),
            "event_version_id": event.version_id,
            "source_outcome_id": source_outcome_id,
            "strategy_id": strategy_id,
        },
    )
    return PositionOperationIntent(
        event_id=event_id,
        operation_type=PositionOperationType.NEG_RISK_CONVERT,
        account_id=account_id,
        strategy_id=strategy_id,
        condition_id=event.outcome(source_outcome_id).condition_id,
        market_id=event.version_id,
        amount=quantity,
        decision_ts=decision_ts,
        collateral_delta=Decimal(0),
        token_deltas=token_deltas,
    )


def reconcile_augmented_conversion(
    *,
    operation_id: str,
    expected_token_deltas: Mapping[str, Decimal],
    observed_token_deltas: Mapping[str, Decimal] | None,
    source_event_id: str,
    raw_payload_hash: str,
    reconciled_at: datetime,
    transaction_hash: str | None = None,
    tolerance: Decimal = Decimal("0.000001"),
) -> AugmentedConversionReconciliation:
    expected = {str(key): Decimal(value) for key, value in expected_token_deltas.items()}
    observed = (
        {str(key): Decimal(value) for key, value in observed_token_deltas.items()}
        if observed_token_deltas is not None
        else {}
    )
    if observed_token_deltas is None:
        status = "NO_OFFICIAL_EVIDENCE"
        amount_delta = Decimal(0)
    else:
        assets = set(expected) | set(observed)
        amount_delta = sum(
            (abs(expected.get(asset, Decimal(0)) - observed.get(asset, Decimal(0))) for asset in assets),
            Decimal(0),
        )
        status = "PASS" if amount_delta <= tolerance else "MISMATCH"
    reconciliation_id = _stable_id(
        "augmented-conversion-reconciliation",
        {"operation_id": operation_id, "source_event_id": source_event_id},
    )
    return AugmentedConversionReconciliation(
        reconciliation_id=reconciliation_id,
        operation_id=operation_id,
        transaction_hash=transaction_hash,
        status=status,
        expected_token_deltas=expected,
        observed_token_deltas=observed,
        amount_delta=amount_delta,
        reconciled_at=reconciled_at,
        source_event_id=source_event_id,
        raw_payload_hash=raw_payload_hash,
    )


class PostgresAugmentedNegRiskStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            for statement in AUGMENTED_NEG_RISK_SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def register_event(self, event: AugmentedNegRiskEvent) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_augmented_neg_risk_events (
                    event_version_id,event_key,version,effective_ts,source,
                    source_event_id,raw_payload_hash,rule_version,metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (event_version_id) DO NOTHING RETURNING event_version_id
                """,
                (
                    event.version_id,
                    event.event_key,
                    event.version,
                    event.effective_ts,
                    event.source,
                    event.source_event_id,
                    event.raw_payload_hash,
                    event.rule_version,
                    _json(event.metadata),
                ),
            )
            inserted = cur.fetchone() is not None
            if not inserted:
                cur.execute(
                    """
                    SELECT event_key,version,effective_ts,source,source_event_id,
                           raw_payload_hash,rule_version
                    FROM quant.simulator_augmented_neg_risk_events
                    WHERE event_version_id=%s
                    """,
                    (event.version_id,),
                )
                row = cur.fetchone()
                if row is None or (
                    str(row["event_key"]) != event.event_key
                    or int(row["version"]) != event.version
                    or row["effective_ts"] != event.effective_ts
                    or str(row["source"]) != event.source
                    or str(row["source_event_id"]) != event.source_event_id
                    or str(row["raw_payload_hash"]) != event.raw_payload_hash
                    or str(row["rule_version"]) != event.rule_version
                ):
                    raise ValueError("augmented event version collision")
                conn.commit()
                return False
            for outcome in event.outcomes:
                cur.execute(
                    """
                    INSERT INTO quant.simulator_augmented_neg_risk_outcomes (
                        event_version_id,outcome_id,label,condition_id,yes_asset_id,
                        no_asset_id,outcome_kind,active,tradeable,metadata
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        event.version_id,
                        outcome.outcome_id,
                        outcome.label,
                        outcome.condition_id,
                        outcome.yes_asset_id,
                        outcome.no_asset_id,
                        outcome.kind.value,
                        outcome.active,
                        outcome.tradeable,
                        _json(outcome.metadata),
                    ),
                )
                matrix = build_conversion_matrix(event, outcome.outcome_id)
                cur.execute(
                    """
                    INSERT INTO quant.simulator_augmented_conversion_matrices (
                        matrix_id,event_version_id,source_outcome_id,
                        token_deltas_per_unit,conservation_by_winner,rule_version
                    ) VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                    """,
                    (
                        matrix.matrix_id,
                        matrix.event_version_id,
                        matrix.source_outcome_id,
                        _decimal_json(matrix.token_deltas_per_unit),
                        _decimal_json(matrix.conservation_by_winner),
                        matrix.rule_version,
                    ),
                )
            conn.commit()
        return True

    def record_reconciliation(
        self, item: AugmentedConversionReconciliation
    ) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_augmented_conversion_reconciliations (
                    reconciliation_id,operation_id,transaction_hash,status,
                    expected_token_deltas,observed_token_deltas,amount_delta,
                    reconciled_at,source_event_id,raw_payload_hash
                ) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
                ON CONFLICT (source_event_id) DO NOTHING RETURNING reconciliation_id
                """,
                (
                    item.reconciliation_id,
                    item.operation_id,
                    item.transaction_hash,
                    item.status,
                    _decimal_json(item.expected_token_deltas),
                    _decimal_json(item.observed_token_deltas),
                    item.amount_delta,
                    item.reconciled_at,
                    item.source_event_id,
                    item.raw_payload_hash,
                ),
            )
            inserted = cur.fetchone() is not None
            if not inserted:
                cur.execute(
                    """
                    SELECT *
                    FROM quant.simulator_augmented_conversion_reconciliations
                    WHERE source_event_id=%s
                    """,
                    (item.source_event_id,),
                )
                existing = cur.fetchone()
                if existing is None or (
                    str(existing["reconciliation_id"]) != item.reconciliation_id
                    or str(existing["operation_id"]) != item.operation_id
                    or str(existing["transaction_hash"] or "")
                    != str(item.transaction_hash or "")
                    or str(existing["status"]) != item.status
                    or _decimal_mapping(existing["expected_token_deltas"])
                    != dict(item.expected_token_deltas)
                    or _decimal_mapping(existing["observed_token_deltas"])
                    != dict(item.observed_token_deltas)
                    or Decimal(existing["amount_delta"]) != item.amount_delta
                    or str(existing["raw_payload_hash"]) != item.raw_payload_hash
                ):
                    raise ValueError(
                        "augmented conversion reconciliation idempotency collision"
                    )
            conn.commit()
        return inserted


def _conservation(
    event: AugmentedNegRiskEvent,
    source_outcome_id: str,
    deltas: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    source = event.outcome(source_outcome_id)
    result: dict[str, Decimal] = {}
    for winner in event.outcomes:
        input_payoff = Decimal(1) if winner.outcome_id != source.outcome_id else Decimal(0)
        output_payoff = sum(
            (
                amount
                for asset_id, amount in deltas.items()
                if amount > 0 and asset_id == winner.yes_asset_id
            ),
            Decimal(0),
        )
        result[winner.outcome_id] = output_payoff - input_payoff
    return result


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def _decimal_json(value: Mapping[str, Decimal]) -> str:
    return json.dumps(
        {str(key): str(item) for key, item in sorted(value.items())},
        sort_keys=True,
    )


def _decimal_mapping(value: Mapping[str, Any]) -> dict[str, Decimal]:
    return {str(key): Decimal(item) for key, item in value.items()}


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, default=str)


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return parsed if isinstance(parsed, list) else [value]
    return []


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bool_default(value: Any, default: bool) -> bool:
    return default if value is None else _bool(value)


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
