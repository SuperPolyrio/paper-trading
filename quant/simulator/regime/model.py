"""Point-in-time venue rule snapshot used by replay and paper execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class VenueRegimeSnapshot:
    regime_id: str
    venue: str
    valid_from: datetime
    valid_to: datetime | None
    api_version: str
    sdk_name: str
    sdk_version: str
    signature_type: str
    collateral_token: str
    exchange_contract: str
    market_type: str
    tick_size_rule: Mapping[str, Any]
    min_order_rule: Mapping[str, Any]
    fee_schedule: Mapping[str, Any]
    rebate_schedule: Mapping[str, Any]
    delay_class: str
    rate_limits: Mapping[str, Any]
    batch_max_size: int
    heartbeat_rule: Mapping[str, Any]
    matching_mode: str
    source_url: str

    def __post_init__(self) -> None:
        if not self.regime_id or not self.venue or not self.source_url:
            raise ValueError("regime id, venue and source URL are required")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("regime valid_to must be after valid_from")
        if self.batch_max_size <= 0:
            raise ValueError("batch maximum size must be positive")

    @property
    def source_hash(self) -> str:
        payload = _json_value(asdict(self))
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class VenueRegimeBinding:
    regime_id: str
    source_hash: str
    venue: str
    valid_from: datetime
    valid_to: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
