"""Pure command contract for the simulated venue gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from quant.simulator.kernel.deterministic_id import deterministic_id


class CommandType(str, Enum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


class CommandState(str, Enum):
    CREATED = "CREATED"
    LOCAL_VALIDATING = "LOCAL_VALIDATING"
    LOCAL_DENIED = "LOCAL_DENIED"
    QUEUED_FOR_GATEWAY = "QUEUED_FOR_GATEWAY"
    THROTTLED = "THROTTLED"
    SIGNING = "SIGNING"
    SENT = "SENT"
    IN_FLIGHT = "IN_FLIGHT"
    DELAYED_UNCANCELABLE = "DELAYED_UNCANCELABLE"
    ACKED_LIVE = "ACKED_LIVE"
    ACKED_MATCHED = "ACKED_MATCHED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    SUBMIT_OUTCOME_UNKNOWN = "SUBMIT_OUTCOME_UNKNOWN"
    RECONCILING = "RECONCILING"
    TERMINAL = "TERMINAL"


class CommandDisposition(str, Enum):
    ACCEPTED_IMMEDIATELY = "ACCEPTED_IMMEDIATELY"
    THROTTLED_UNTIL = "THROTTLED_UNTIL"
    LOCAL_RATE_LIMIT_DENIED = "LOCAL_RATE_LIMIT_DENIED"
    LOCAL_DENIED = "LOCAL_DENIED"
    VENUE_DENIED = "VENUE_DENIED"
    DELAYED_UNCANCELABLE = "DELAYED_UNCANCELABLE"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True)
class GatewayCommand:
    command_id: str
    command_type: CommandType
    account_id: str
    signer_id: str
    ip_id: str
    endpoint: str
    created_ts_ns: int
    order_id: str | None = None
    replacement_order_id: str | None = None
    post_only: bool = False
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if int(self.created_ts_ns) < 0:
            raise ValueError("created_ts_ns must be non-negative")
        if not all(str(value).strip() for value in (self.command_id, self.account_id, self.signer_id, self.ip_id, self.endpoint)):
            raise ValueError("command_id, account_id, signer_id, ip_id and endpoint are required")
        if self.command_type in {CommandType.CANCEL, CommandType.REPLACE} and not self.order_id:
            raise ValueError(f"{self.command_type.value} requires order_id")
        object.__setattr__(self, "created_ts_ns", int(self.created_ts_ns))
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def build(
        cls,
        *,
        command_type: CommandType | str,
        account_id: str,
        signer_id: str,
        endpoint: str,
        ip_id: str = "default-ip",
        created_ts_ns: int,
        order_id: str | None = None,
        replacement_order_id: str | None = None,
        post_only: bool = False,
        payload: Mapping[str, Any] | None = None,
        command_id: str | None = None,
    ) -> "GatewayCommand":
        kind = command_type if isinstance(command_type, CommandType) else CommandType(str(command_type).upper())
        values = {
            "command_type": kind.value,
            "account_id": str(account_id),
            "signer_id": str(signer_id),
            "ip_id": str(ip_id),
            "endpoint": str(endpoint),
            "created_ts_ns": int(created_ts_ns),
            "order_id": order_id,
            "replacement_order_id": replacement_order_id,
            "post_only": bool(post_only),
            "payload": dict(payload or {}),
        }
        return cls(
            command_id=command_id or deterministic_id("venue-command", values),
            command_type=kind,
            account_id=str(account_id),
            signer_id=str(signer_id),
            ip_id=str(ip_id),
            endpoint=str(endpoint),
            created_ts_ns=int(created_ts_ns),
            order_id=order_id,
            replacement_order_id=replacement_order_id,
            post_only=bool(post_only),
            payload=dict(payload or {}),
        )

    @property
    def increases_risk(self) -> bool:
        return self.command_type in {CommandType.SUBMIT, CommandType.REPLACE}


@dataclass(frozen=True)
class GatewayDecision:
    command_id: str
    disposition: CommandDisposition
    state: CommandState
    reason: str
    effective_ts_ns: int | None = None
