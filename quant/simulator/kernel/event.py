"""Immutable, serializable event contract for deterministic simulation."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .deterministic_id import canonical_json, deterministic_id
from .event_priority import priority_for


@dataclass(frozen=True)
class SimEvent:
    """One causally ordered simulator event.

    ``event_ts_ns`` is the event's venue/market timestamp. ``receive_ts_ns``
    records observation time but never participates in causal ordering.
    """

    event_id: str
    event_type: str
    event_ts_ns: int
    receive_ts_ns: int | None
    priority: int
    source_sequence: int
    deterministic_tiebreaker: str
    aggregate_key: str
    payload: Mapping[str, Any]
    source_event_id: str | None
    model_version: str

    def __post_init__(self) -> None:
        event_type = str(self.event_type).strip().upper()
        aggregate_key = str(self.aggregate_key).strip()
        tiebreaker = str(self.deterministic_tiebreaker).strip()
        model_version = str(self.model_version).strip()
        event_id = str(self.event_id).strip()
        if not event_id or not event_type or not aggregate_key or not tiebreaker or not model_version:
            raise ValueError("event_id, type, aggregate_key, tiebreaker and model_version are required")
        if int(self.event_ts_ns) < 0:
            raise ValueError("event_ts_ns must be non-negative")
        if self.receive_ts_ns is not None and int(self.receive_ts_ns) < 0:
            raise ValueError("receive_ts_ns must be non-negative")
        if int(self.source_sequence) < 0:
            raise ValueError("source_sequence must be non-negative")
        payload = _freeze_payload(self.payload)
        canonical_json(payload)
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "event_ts_ns", int(self.event_ts_ns))
        object.__setattr__(self, "receive_ts_ns", None if self.receive_ts_ns is None else int(self.receive_ts_ns))
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "source_sequence", int(self.source_sequence))
        object.__setattr__(self, "deterministic_tiebreaker", tiebreaker)
        object.__setattr__(self, "aggregate_key", aggregate_key)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "source_event_id", None if self.source_event_id is None else str(self.source_event_id))
        object.__setattr__(self, "model_version", model_version)

    @classmethod
    def build(
        cls,
        *,
        event_type: str,
        event_ts_ns: int,
        source_sequence: int,
        aggregate_key: str,
        payload: Mapping[str, Any] | None = None,
        receive_ts_ns: int | None = None,
        priority: int | None = None,
        source_event_id: str | None = None,
        model_version: str = "simulator-kernel-v1",
        deterministic_tiebreaker: str | None = None,
        event_id: str | None = None,
    ) -> "SimEvent":
        normalized_type = str(event_type).strip().upper()
        normalized_payload = dict(payload or {})
        fingerprint = {
            "event_type": normalized_type,
            "event_ts_ns": int(event_ts_ns),
            "source_sequence": int(source_sequence),
            "aggregate_key": str(aggregate_key),
            "payload": normalized_payload,
            "source_event_id": source_event_id,
            "model_version": str(model_version),
        }
        tiebreaker = deterministic_tiebreaker or deterministic_id("tie", fingerprint)
        identifier = event_id or deterministic_id("event", {**fingerprint, "tiebreaker": tiebreaker})
        return cls(
            event_id=identifier,
            event_type=normalized_type,
            event_ts_ns=int(event_ts_ns),
            receive_ts_ns=receive_ts_ns,
            priority=priority_for(normalized_type) if priority is None else int(priority),
            source_sequence=int(source_sequence),
            deterministic_tiebreaker=tiebreaker,
            aggregate_key=str(aggregate_key),
            payload=normalized_payload,
            source_event_id=source_event_id,
            model_version=str(model_version),
        )

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        return (
            self.event_ts_ns,
            self.priority,
            self.source_sequence,
            self.deterministic_tiebreaker,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_ts_ns": self.event_ts_ns,
            "receive_ts_ns": self.receive_ts_ns,
            "priority": self.priority,
            "source_sequence": self.source_sequence,
            "deterministic_tiebreaker": self.deterministic_tiebreaker,
            "aggregate_key": self.aggregate_key,
            "payload": _thaw_payload(self.payload),
            "source_event_id": self.source_event_id,
            "model_version": self.model_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SimEvent":
        return cls(
            event_id=str(value["event_id"]),
            event_type=str(value["event_type"]),
            event_ts_ns=int(value["event_ts_ns"]),
            receive_ts_ns=None if value.get("receive_ts_ns") is None else int(value["receive_ts_ns"]),
            priority=int(value["priority"]),
            source_sequence=int(value["source_sequence"]),
            deterministic_tiebreaker=str(value["deterministic_tiebreaker"]),
            aggregate_key=str(value["aggregate_key"]),
            payload=value.get("payload") if isinstance(value.get("payload"), Mapping) else {},
            source_event_id=None if value.get("source_event_id") is None else str(value["source_event_id"]),
            model_version=str(value["model_version"]),
        )


def _freeze_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _thaw_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_payload(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_payload(item) for item in value]
    if isinstance(value, frozenset):
        return [_thaw_payload(item) for item in sorted(value, key=canonical_json)]
    return value
