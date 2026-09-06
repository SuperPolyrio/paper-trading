"""Typed official Combo/RFQ contracts and six-decimal arithmetic."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

E6 = 1_000_000
_E6_DECIMAL = Decimal(E6)


class ComboDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SizeUnit(str, Enum):
    NOTIONAL = "notional"
    SHARES = "shares"


class ComboRfqState(str, Enum):
    REQUESTED = "REQUESTED"
    QUOTE_COMPETITION = "QUOTE_COMPETITION"
    QUOTE_AVAILABLE = "QUOTE_AVAILABLE"
    ACCEPTING = "ACCEPTING"
    AWAITING_MAKER_CONFIRMATION = "AWAITING_MAKER_CONFIRMATION"
    MATCHED = "MATCHED"
    MINED = "MINED"
    RETRYING = "RETRYING"
    RECONCILING = "RECONCILING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"

    @property
    def terminal(self) -> bool:
        return self in {
            ComboRfqState.CONFIRMED,
            ComboRfqState.FAILED,
            ComboRfqState.EXPIRED,
            ComboRfqState.CANCELED,
        }


class ExecutionStatus(str, Enum):
    MATCHED = "MATCHED"
    MINED = "MINED"
    RETRYING = "RETRYING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in {ExecutionStatus.CONFIRMED, ExecutionStatus.FAILED}


def decimal_to_e6(value: Decimal | str | int, *, allow_zero: bool = False) -> int:
    """Convert an exact decimal to six-decimal base units without rounding."""

    try:
        selected = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal value") from exc
    if not selected.is_finite():
        raise ValueError("fixed-point value must be finite")
    if selected < 0 or (selected == 0 and not allow_zero):
        raise ValueError("fixed-point value must be positive")
    scaled = selected * _E6_DECIMAL
    if scaled != scaled.to_integral_value():
        raise ValueError("fixed-point value has more than six decimal places")
    return int(scaled)


def e6_to_decimal(value_e6: int | str) -> Decimal:
    try:
        selected = int(value_e6)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid e6 value") from exc
    return Decimal(selected) / _E6_DECIMAL


def unix_ms_to_datetime(value: int | str | None) -> datetime | None:
    if value in {None, "", 0, "0"}:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def datetime_to_unix_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return int(value.timestamp() * 1000)


@dataclass(frozen=True)
class RequestedSize:
    unit: SizeUnit
    value_e6: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_e6", int(self.value_e6))
        if self.value_e6 <= 0:
            raise ValueError("requested size must be positive")

    @classmethod
    def from_decimal(cls, unit: SizeUnit, value: Decimal | str | int) -> RequestedSize:
        return cls(unit=unit, value_e6=decimal_to_e6(value))

    def as_payload(self) -> dict[str, str]:
        return {"unit": self.unit.value, "value_e6": str(self.value_e6)}


@dataclass(frozen=True)
class ComboMarket:
    market_id: str
    condition_id: str
    position_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    outcome_prices: tuple[Decimal, ...]
    slug: str
    title: str
    volume: Decimal = Decimal(0)
    tags: tuple[str, ...] = ()
    image: str | None = None
    raw_payload_hash: str = ""

    def __post_init__(self) -> None:
        if not self.market_id or not self.condition_id:
            raise ValueError("combo market identity is required")
        if len(self.position_ids) != len(self.outcomes):
            raise ValueError("position_ids and outcomes must align by index")
        if len(self.outcome_prices) != len(self.outcomes):
            raise ValueError("outcome_prices and outcomes must align by index")
        if len(self.position_ids) < 2 or len(set(self.position_ids)) != len(
            self.position_ids
        ):
            raise ValueError("combo market requires distinct outcome position IDs")

    @property
    def yes_position_id(self) -> str:
        for index, outcome in enumerate(self.outcomes):
            if outcome.strip().upper() == "YES":
                return self.position_ids[index]
        raise ValueError("combo market has no YES position")

    @property
    def no_position_id(self) -> str:
        for index, outcome in enumerate(self.outcomes):
            if outcome.strip().upper() == "NO":
                return self.position_ids[index]
        raise ValueError("combo market has no NO position")

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> ComboMarket:
        positions = tuple(str(item) for item in payload.get("position_ids") or ())
        outcomes = tuple(str(item) for item in payload.get("outcomes") or ())
        prices = tuple(Decimal(str(item)) for item in payload.get("outcome_prices") or ())
        return cls(
            market_id=str(payload.get("id") or ""),
            condition_id=str(payload.get("condition_id") or ""),
            position_ids=positions,
            outcomes=outcomes,
            outcome_prices=prices,
            slug=str(payload.get("slug") or ""),
            title=str(payload.get("title") or ""),
            volume=Decimal(str(payload.get("volume") or 0)),
            tags=tuple(str(item) for item in payload.get("tags") or ()),
            image=str(payload["image"]) if payload.get("image") else None,
            raw_payload_hash=payload_hash(payload),
        )


@dataclass(frozen=True)
class ComboRequest:
    rfq_id: str
    leg_position_ids: tuple[str, ...]
    yes_position_id: str
    no_position_id: str
    direction: ComboDirection
    requested_size: RequestedSize
    created_at: datetime
    submission_deadline: datetime | None = None

    def __post_init__(self) -> None:
        if not self.rfq_id:
            raise ValueError("rfq_id is required")
        if not 2 <= len(self.leg_position_ids) <= 50:
            raise ValueError("Combo RFQ requires between 2 and 50 legs")
        if len(set(self.leg_position_ids)) != len(self.leg_position_ids):
            raise ValueError("Combo RFQ leg position IDs must be unique")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        expected_unit = (
            SizeUnit.NOTIONAL
            if self.direction is ComboDirection.BUY
            else SizeUnit.SHARES
        )
        if self.requested_size.unit is not expected_unit:
            raise ValueError(
                f"{self.direction.value} RFQ must use {expected_unit.value} sizing"
            )
        if self.submission_deadline is not None:
            if self.submission_deadline.tzinfo is None:
                raise ValueError("submission_deadline must be timezone-aware")
            if self.submission_deadline <= self.created_at:
                raise ValueError("submission_deadline must follow created_at")

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> ComboRequest:
        requested = payload.get("requested_size") or {}
        created = unix_ms_to_datetime(payload.get("created_at"))
        if created is None:
            created = datetime.now(timezone.utc)
        return cls(
            rfq_id=str(payload.get("rfq_id") or payload.get("id") or ""),
            leg_position_ids=tuple(
                str(item) for item in payload.get("leg_position_ids") or ()
            ),
            yes_position_id=str(payload.get("yes_position_id") or ""),
            no_position_id=str(payload.get("no_position_id") or ""),
            direction=ComboDirection(str(payload.get("direction") or "").upper()),
            requested_size=RequestedSize(
                unit=SizeUnit(str(requested.get("unit") or "").lower()),
                value_e6=int(requested.get("value_e6") or 0),
            ),
            created_at=created,
            submission_deadline=unix_ms_to_datetime(
                payload.get("submission_deadline")
            ),
        )

    def as_builder_payload(self) -> dict[str, Any]:
        return {
            "leg_position_ids": list(self.leg_position_ids),
            "direction": self.direction.value,
            "requested_size": self.requested_size.as_payload(),
        }


@dataclass(frozen=True)
class ComboQuote:
    quote_id: str
    rfq_id: str
    price_e6: int
    size_e6: int
    expires_at: datetime
    total_required_e6: int | None = None
    net_receive_e6: int | None = None
    signed_order: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "price_e6", int(self.price_e6))
        object.__setattr__(self, "size_e6", int(self.size_e6))
        if not self.quote_id or not self.rfq_id:
            raise ValueError("quote_id and rfq_id are required")
        if not 0 < self.price_e6 < E6:
            raise ValueError("quote price must be between 0 and 1")
        if self.size_e6 <= 0:
            raise ValueError("quote size must be positive")
        if self.expires_at.tzinfo is None:
            raise ValueError("quote expiry must be timezone-aware")

    @classmethod
    def from_api(cls, payload: Mapping[str, Any], *, rfq_id: str) -> ComboQuote:
        expires_at = unix_ms_to_datetime(payload.get("expires_at"))
        if expires_at is None:
            raise ValueError("official quote is missing expires_at")
        return cls(
            quote_id=str(payload.get("quote_id") or ""),
            rfq_id=rfq_id,
            price_e6=int(payload.get("price_e6") or 0),
            size_e6=int(payload.get("size_e6") or 0),
            expires_at=expires_at,
            total_required_e6=(
                int(payload["total_required_e6"])
                if payload.get("total_required_e6") is not None
                else None
            ),
            net_receive_e6=(
                int(payload["net_receive_e6"])
                if payload.get("net_receive_e6") is not None
                else None
            ),
            signed_order=dict(payload.get("signed_order") or {}),
        )


@dataclass(frozen=True)
class OfficialRfqSnapshot:
    rfq_id: str
    state: ComboRfqState
    quote: ComboQuote | None
    tx_hash: str | None
    error_code: str | None
    error_message: str | None
    payload: Mapping[str, Any]
    payload_hash: str

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> OfficialRfqSnapshot:
        rfq_id = str(
            payload.get("rfq_id")
            or (payload.get("request") or {}).get("rfq_id")
            or ""
        )
        status = normalize_state(payload.get("status"))
        quote_payload = payload.get("quote")
        quote = (
            ComboQuote.from_api(quote_payload, rfq_id=rfq_id)
            if isinstance(quote_payload, Mapping)
            else None
        )
        error = payload.get("error")
        error_code: str | None = None
        error_message: str | None = None
        if isinstance(error, Mapping):
            error_code = str(error.get("code") or "") or None
            error_message = str(error.get("message") or error.get("error") or "") or None
        elif error:
            error_message = str(error)
        return cls(
            rfq_id=rfq_id,
            state=status,
            quote=quote,
            tx_hash=str(payload.get("tx_hash") or "") or None,
            error_code=error_code,
            error_message=error_message,
            payload=dict(payload),
            payload_hash=payload_hash(payload),
        )


def normalize_state(value: Any) -> ComboRfqState:
    selected = str(value or "REQUESTED").strip().upper()
    aliases = {
        "AWAITING_REQUESTER_ACCEPTANCE": ComboRfqState.QUOTE_AVAILABLE,
        "EXECUTING": ComboRfqState.MATCHED,
        "FILLED": ComboRfqState.CONFIRMED,
        "CANCELLED": ComboRfqState.CANCELED,
    }
    if selected in aliases:
        return aliases[selected]
    try:
        return ComboRfqState(selected)
    except ValueError:
        return ComboRfqState.RECONCILING


def payload_hash(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    encoded = json.dumps(
        _jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return value
