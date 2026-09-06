"""Synthetic conditional orders which generate normal paper child intents."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .tenant_platform import (
    PaperPermission,
    PostgresTenantPlatformStore,
    TenantPrincipal,
)

TERMINAL_STATES = frozenset({"TRIGGERED", "CANCELLED", "EXPIRED", "FAILED"})
TRUSTED_DATA_QUALITY = frozenset({"A", "B", "BOOK_FRESH", "READY_TWO_SIDED"})
PARENT_SUCCESS_STATES = frozenset({"FILLED", "CONFIRMED", "COMPLETED"})
PARENT_FAILURE_STATES = frozenset(
    {"CANCELLED", "CANCELED", "EXPIRED", "FAILED", "REJECTED", "VOIDED"}
)


class ConditionalOrderError(RuntimeError):
    pass


class ConditionalOrderValidationError(ConditionalOrderError):
    pass


class ConditionalOrderNotFound(ConditionalOrderError):
    pass


class ConditionalOrderConflict(ConditionalOrderError):
    pass


@dataclass(frozen=True)
class TriggerObservation:
    asset_id: str
    event_ts: datetime
    source: str
    data_quality: str
    reference_price: str = "MID"
    price: Decimal | None = None
    has_gap: bool = False
    stale: bool = False
    signal_name: str | None = None
    signal_value: str | None = None

    @property
    def trustworthy(self) -> bool:
        return (
            not self.has_gap
            and not self.stale
            and str(self.data_quality).upper() in TRUSTED_DATA_QUALITY
        )


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        selected = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ConditionalOrderValidationError(f"{field} must be numeric") from exc
    if not selected.is_finite():
        raise ConditionalOrderValidationError(f"{field} must be finite")
    return selected


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def validate_child_order(payload: Mapping[str, Any]) -> dict[str, Any]:
    child = dict(payload)
    for field in ("asset_id", "side", "time_in_force", "limit_price", "size"):
        if child.get(field) in (None, ""):
            raise ConditionalOrderValidationError(f"child_order.{field} is required")
    side = str(child["side"]).upper()
    tif = str(child["time_in_force"]).upper()
    if side not in {"BUY", "SELL"} or tif not in {"FOK", "FAK", "GTC", "GTD"}:
        raise ConditionalOrderValidationError(
            "child order side or time_in_force is invalid"
        )
    price = _decimal(child["limit_price"], field="child_order.limit_price")
    size = _decimal(child["size"], field="child_order.size")
    if price <= 0 or price >= 1 or size <= 0:
        raise ConditionalOrderValidationError(
            "child order price/size is outside its domain"
        )
    amount_unit = str(child.get("amount_unit") or "SHARES").upper()
    if amount_unit not in {"SHARES", "QUOTE"}:
        raise ConditionalOrderValidationError("child order amount_unit is invalid")
    if (side == "SELL" or tif in {"GTC", "GTD"}) and amount_unit != "SHARES":
        raise ConditionalOrderValidationError(
            "SELL and resting child orders require SHARES"
        )
    return _json_value(
        {
            **child,
            "asset_id": str(child["asset_id"]),
            "side": side,
            "time_in_force": tif,
            "limit_price": price,
            "size": size,
            "amount_unit": amount_unit,
            "post_only": bool(child.get("post_only", False)),
        }
    )


def evaluate_trigger(
    order: Mapping[str, Any], observation: TriggerObservation
) -> tuple[bool, Decimal | None, Decimal | None, str]:
    """Return trigger decision and updated watermark using only causal evidence."""

    if not observation.trustworthy:
        return False, None, None, "UNTRUSTED_OR_GAPPED_OBSERVATION"
    kind = str(order["trigger_kind"]).upper()
    if kind == "TIME":
        trigger_at = order.get("trigger_at")
        if isinstance(trigger_at, str):
            trigger_at = datetime.fromisoformat(trigger_at.replace("Z", "+00:00"))
        return (
            observation.event_ts >= trigger_at,
            observation.price,
            None,
            "TIME_REACHED",
        )
    if kind == "SIGNAL":
        matched = observation.signal_name == str(
            order.get("signal_name") or ""
        ) and str(observation.signal_value or "").upper() in {"1", "TRUE", "TRIGGER"}
        return matched, observation.price, None, "SIGNAL_MATCHED"
    if kind == "PARENT_TERMINAL":
        return False, observation.price, None, "PARENT_EVENT_REQUIRED"
    if observation.price is None:
        return False, None, None, "PRICE_MISSING"
    price = Decimal(observation.price)
    order_type = str(order["order_type"]).upper()
    if order_type == "TRAILING_STOP":
        side = str(dict(order["child_order"])["side"]).upper()
        prior = order.get("watermark")
        watermark = price if prior in (None, "") else Decimal(str(prior))
        watermark = max(watermark, price) if side == "SELL" else min(watermark, price)
        if order.get("trailing_percent") not in (None, ""):
            percent = Decimal(str(order["trailing_percent"]))
            threshold = watermark * (
                Decimal(1) - percent if side == "SELL" else Decimal(1) + percent
            )
        else:
            offset = Decimal(str(order["trailing_offset"]))
            threshold = watermark - offset if side == "SELL" else watermark + offset
        triggered = price <= threshold if side == "SELL" else price >= threshold
        return triggered, price, watermark, "TRAILING_THRESHOLD_REACHED"
    trigger_value = Decimal(str(order["trigger_value"]))
    operator = str(order["trigger_operator"]).upper()
    triggered = (
        price >= trigger_value
        if operator == "GTE"
        else price <= trigger_value
        if operator == "LTE"
        else price == trigger_value
    )
    return triggered, price, None, f"PRICE_{operator}_{trigger_value}"


ChildSubmitter = Callable[[Mapping[str, Any], Mapping[str, Any]], int]


class PostgresConditionalOrderService:
    def __init__(self, tenant_store: PostgresTenantPlatformStore) -> None:
        self.tenant_store = tenant_store

    def arm_order(
        self,
        principal: TenantPrincipal,
        *,
        account_id: UUID,
        strategy_id: UUID,
        order_type: str,
        trigger: Mapping[str, Any],
        child_order: Mapping[str, Any],
        idempotency_key: str,
        group_id: UUID | None = None,
        group_policy: str = "NONE",
        parent_conditional_order_id: UUID | None = None,
        parent_intent_id: int | None = None,
        expires_at: datetime | None = None,
        initially_paused: bool = False,
    ) -> dict[str, Any]:
        selected_type = str(order_type).upper()
        selected_policy = str(group_policy).upper()
        allowed_types = {
            "STOP",
            "STOP_LIMIT",
            "TAKE_PROFIT",
            "TRAILING_STOP",
            "TIME_TRIGGERED",
            "SIGNAL_TRIGGERED",
            "OCO",
            "OTO",
            "BRACKET",
        }
        if selected_type not in allowed_types or selected_policy not in {
            "NONE",
            "OCO",
            "OTO",
            "BRACKET",
        }:
            raise ConditionalOrderValidationError(
                "conditional order type/group is invalid"
            )
        child = validate_child_order(child_order)
        kind = str(trigger.get("kind") or "PRICE").upper()
        reference_price = str(trigger.get("reference_price") or "MID").upper()
        if reference_price not in {"LAST", "BEST_BID", "BEST_ASK", "MID"}:
            raise ConditionalOrderValidationError("trigger reference_price is invalid")
        if kind not in {"PRICE", "TIME", "SIGNAL", "PARENT_TERMINAL"}:
            raise ConditionalOrderValidationError("trigger kind is invalid")
        operator = str(trigger.get("operator") or "").upper() or None
        trigger_value = trigger.get("value")
        trigger_at = trigger.get("at")
        signal_name = str(trigger.get("signal_name") or "").strip() or None
        if kind == "PRICE":
            if operator not in {"GTE", "LTE", "EQ"} or trigger_value in (None, ""):
                raise ConditionalOrderValidationError(
                    "price trigger requires operator and value"
                )
            trigger_value = _decimal(trigger_value, field="trigger.value")
            if trigger_value <= 0 or trigger_value >= 1:
                raise ConditionalOrderValidationError(
                    "trigger value must be between 0 and 1"
                )
        elif kind == "TIME":
            if trigger_at in (None, ""):
                raise ConditionalOrderValidationError(
                    "time trigger requires trigger.at"
                )
            if isinstance(trigger_at, str):
                trigger_at = datetime.fromisoformat(trigger_at.replace("Z", "+00:00"))
            if trigger_at.tzinfo is None:
                raise ConditionalOrderValidationError(
                    "trigger.at must include a timezone"
                )
        elif kind == "SIGNAL" and not signal_name:
            raise ConditionalOrderValidationError("signal trigger requires signal_name")
        elif kind == "PARENT_TERMINAL" and parent_intent_id is None:
            raise ConditionalOrderValidationError(
                "parent trigger requires parent_intent_id"
            )
        waiting_for_parent = bool(
            selected_policy == "BRACKET"
            and parent_intent_id is not None
            and kind != "PARENT_TERMINAL"
        )
        initial_status = (
            "PENDING_DATA" if initially_paused or waiting_for_parent else "ARMED"
        )
        initial_source = (
            "GROUP_PENDING"
            if initially_paused
            else "WAITING_PARENT"
            if waiting_for_parent
            else None
        )
        trailing_offset = trigger.get("trailing_offset")
        trailing_percent = trigger.get("trailing_percent")
        if selected_type == "TRAILING_STOP":
            if (trailing_offset in (None, "")) == (trailing_percent in (None, "")):
                raise ConditionalOrderValidationError(
                    "trailing stop requires exactly one offset or percent"
                )
            if trailing_offset not in (None, ""):
                trailing_offset = _decimal(trailing_offset, field="trailing_offset")
                if trailing_offset <= 0:
                    raise ConditionalOrderValidationError(
                        "trailing offset must be positive"
                    )
            if trailing_percent not in (None, ""):
                trailing_percent = _decimal(trailing_percent, field="trailing_percent")
                if trailing_percent <= 0 or trailing_percent >= 1:
                    raise ConditionalOrderValidationError(
                        "trailing percent must be in (0,1)"
                    )
        order_id = uuid4()
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT 1 FROM quant.paper_account_registry account
                JOIN quant.paper_strategies strategy
                  ON strategy.tenant_id=account.tenant_id
                 AND strategy.account_id=account.account_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND strategy.strategy_id=%s
                  AND account.status='ACTIVE' AND strategy.status='ACTIVE'
                """,
                (principal.tenant_id, account_id, strategy_id),
            )
            if cur.fetchone() is None:
                raise ConditionalOrderNotFound("active account/strategy was not found")
            cur.execute(
                """
                INSERT INTO quant.paper_conditional_orders (
                    conditional_order_id,tenant_id,account_id,strategy_id,asset_id,
                    order_type,trigger_kind,trigger_operator,trigger_value,trigger_at,
                    signal_name,reference_price,trailing_offset,trailing_percent,
                    child_order,group_id,group_policy,parent_conditional_order_id,
                    parent_intent_id,status,trigger_source,expires_at,
                    idempotency_key,created_by
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (tenant_id,idempotency_key) DO NOTHING
                RETURNING conditional_order_id
                """,
                (
                    order_id,
                    principal.tenant_id,
                    account_id,
                    strategy_id,
                    child["asset_id"],
                    selected_type,
                    kind,
                    operator,
                    trigger_value,
                    trigger_at,
                    signal_name,
                    reference_price,
                    trailing_offset,
                    trailing_percent,
                    json.dumps(child, sort_keys=True),
                    group_id,
                    selected_policy,
                    parent_conditional_order_id,
                    parent_intent_id,
                    initial_status,
                    initial_source,
                    expires_at,
                    str(idempotency_key),
                    principal.subject_user_id,
                ),
            )
            inserted = cur.fetchone()
            if inserted is None:
                cur.execute(
                    """SELECT conditional_order_id FROM quant.paper_conditional_orders
                       WHERE tenant_id=%s AND idempotency_key=%s""",
                    (principal.tenant_id, str(idempotency_key)),
                )
                order_id = UUID(str(cur.fetchone()["conditional_order_id"]))
            else:
                self._append_event(
                    cur,
                    principal,
                    order_id,
                    "CREATED",
                    None,
                    "ARMED",
                    "conditional_order_armed",
                    {},
                    f"created:{order_id}",
                )
                self.tenant_store._append_audit(
                    cur,
                    principal,
                    event_type="CONDITIONAL_ORDER_CREATED",
                    resource_type="CONDITIONAL_ORDER",
                    resource_id=str(order_id),
                    reason="synthetic_order_armed",
                    payload={
                        "order_type": selected_type,
                        "asset_id": child["asset_id"],
                    },
                )
            conn.commit()
        return self.get_order(principal, order_id)

    def arm_group(
        self,
        principal: TenantPrincipal,
        *,
        account_id: UUID,
        strategy_id: UUID,
        group_policy: str,
        orders: list[Mapping[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create a recoverable conditional group without exposing partial groups."""

        selected_policy = str(group_policy).upper()
        minimum_legs = 1 if selected_policy == "OTO" else 2
        if selected_policy not in {"OCO", "OTO", "BRACKET"}:
            raise ConditionalOrderValidationError("conditional group policy is invalid")
        if not minimum_legs <= len(orders) <= 8:
            raise ConditionalOrderValidationError(
                f"{selected_policy} requires {minimum_legs}..8 order legs"
            )
        group_id = uuid5(
            NAMESPACE_URL,
            f"paper-conditional-group:{principal.tenant_id}:{idempotency_key}",
        )
        created = []
        for index, raw in enumerate(orders):
            leg = dict(raw)
            created.append(
                self.arm_order(
                    principal,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    order_type=str(leg["order_type"]),
                    trigger=dict(leg["trigger"]),
                    child_order=dict(leg["child_order"]),
                    idempotency_key=f"{idempotency_key}:leg:{index}",
                    group_id=group_id,
                    group_policy=selected_policy,
                    parent_conditional_order_id=(
                        UUID(str(leg["parent_conditional_order_id"]))
                        if leg.get("parent_conditional_order_id")
                        else None
                    ),
                    parent_intent_id=(
                        int(leg["parent_intent_id"])
                        if leg.get("parent_intent_id") is not None
                        else None
                    ),
                    expires_at=leg.get("expires_at"),
                    initially_paused=True,
                )
            )
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """SELECT conditional_order_id,status,parent_intent_id,group_policy,
                          trigger_kind,trigger_source
                   FROM quant.paper_conditional_orders
                   WHERE tenant_id=%s AND group_id=%s
                   ORDER BY created_at,conditional_order_id FOR UPDATE""",
                (principal.tenant_id, group_id),
            )
            group_rows = [dict(row) for row in cur.fetchall()]
            pending = [
                row for row in group_rows if row.get("trigger_source") == "GROUP_PENDING"
            ]
            if len(group_rows) != len(orders):
                raise ConditionalOrderConflict(
                    "conditional group is incomplete; retry with the original request"
                )
            for row in pending:
                waits_for_parent = bool(
                    selected_policy == "BRACKET"
                    and row.get("parent_intent_id") is not None
                    and str(row["trigger_kind"]) != "PARENT_TERMINAL"
                )
                next_status = "PENDING_DATA" if waits_for_parent else "ARMED"
                next_source = (
                    "WAITING_PARENT" if waits_for_parent else "GROUP_ACTIVATED"
                )
                order_id = UUID(str(row["conditional_order_id"]))
                cur.execute(
                    """UPDATE quant.paper_conditional_orders
                       SET status=%s,trigger_source=%s,
                           state_version=state_version+1,
                           updated_at=clock_timestamp()
                       WHERE tenant_id=%s AND conditional_order_id=%s
                         AND trigger_source='GROUP_PENDING'""",
                    (next_status, next_source, principal.tenant_id, order_id),
                )
                self._append_event(
                    cur,
                    principal,
                    order_id,
                    "GROUP_ACTIVATED",
                    str(row["status"]),
                    next_status,
                    "conditional_group_complete",
                    {"group_id": group_id, "group_policy": selected_policy},
                    f"group-activate:{order_id}:{group_id}",
                )
            conn.commit()
        return {
            "group_id": str(group_id),
            "group_policy": selected_policy,
            "orders": [
                self.get_order(
                    principal, UUID(str(order["conditional_order_id"]))
                )
                for order in created
            ],
        }

    def get_order(self, principal: TenantPrincipal, order_id: UUID) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """SELECT * FROM quant.paper_conditional_orders
                   WHERE tenant_id=%s AND conditional_order_id=%s""",
                (principal.tenant_id, order_id),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise ConditionalOrderNotFound("conditional order was not found")
        return _json_value(dict(row))

    def list_orders(
        self, principal: TenantPrincipal, *, account_id: UUID, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """SELECT * FROM quant.paper_conditional_orders
                   WHERE tenant_id=%s AND account_id=%s
                   ORDER BY created_at DESC,conditional_order_id DESC LIMIT %s""",
                (principal.tenant_id, account_id, int(limit)),
            )
            rows = [_json_value(dict(row)) for row in cur.fetchall()]
            conn.commit()
        return rows

    def cancel_order(
        self, principal: TenantPrincipal, order_id: UUID, *, reason: str
    ) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """SELECT status FROM quant.paper_conditional_orders
                   WHERE tenant_id=%s AND conditional_order_id=%s FOR UPDATE""",
                (principal.tenant_id, order_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ConditionalOrderNotFound("conditional order was not found")
            if str(row["status"]) in TERMINAL_STATES:
                raise ConditionalOrderConflict("conditional order is already terminal")
            cur.execute(
                """UPDATE quant.paper_conditional_orders
                   SET status='CANCELLED',failure_reason=%s,state_version=state_version+1,
                       updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND conditional_order_id=%s""",
                (str(reason).strip() or "user_cancel", principal.tenant_id, order_id),
            )
            self._append_event(
                cur,
                principal,
                order_id,
                "CANCELLED",
                str(row["status"]),
                "CANCELLED",
                str(reason).strip() or "user_cancel",
                {},
                f"cancel:{order_id}",
            )
            conn.commit()
        return self.get_order(principal, order_id)

    def process_observation(
        self,
        principal: TenantPrincipal,
        observation: TriggerObservation,
        *,
        submit_child: ChildSubmitter,
    ) -> list[dict[str, Any]]:
        candidates = self._claim_triggers(principal, observation)
        results: list[dict[str, Any]] = []
        for candidate in candidates:
            evidence = {
                "trigger_source": observation.source,
                "trigger_ts": observation.event_ts,
                "trigger_price": observation.price,
                "data_quality": observation.data_quality,
                "has_gap": observation.has_gap,
            }
            try:
                child_intent_id = int(submit_child(candidate, _json_value(evidence)))
            except Exception as exc:  # noqa: BLE001
                self._defer_submission(
                    principal, UUID(candidate["conditional_order_id"]), exc
                )
                continue
            results.append(
                self._complete_trigger(
                    principal,
                    UUID(candidate["conditional_order_id"]),
                    child_intent_id=child_intent_id,
                    evidence=evidence,
                )
            )
        return results

    def recover_triggering(
        self,
        principal: TenantPrincipal,
        *,
        submit_child: ChildSubmitter,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Idempotently finish trigger claims left by a stopped worker."""

        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """SELECT * FROM quant.paper_conditional_orders
                   WHERE tenant_id=%s AND status='TRIGGERING'
                   ORDER BY trigger_ts,conditional_order_id LIMIT %s""",
                (principal.tenant_id, int(limit)),
            )
            candidates = [_json_value(dict(row)) for row in cur.fetchall()]
            conn.commit()
        results = []
        for candidate in candidates:
            order_id = UUID(str(candidate["conditional_order_id"]))
            evidence = {
                "trigger_source": candidate.get("trigger_source"),
                "trigger_ts": candidate.get("trigger_ts"),
                "trigger_price": candidate.get("trigger_price"),
                "data_quality": candidate.get("trigger_data_quality"),
                "recovered": True,
            }
            try:
                child_intent_id = int(submit_child(candidate, evidence))
            except Exception as exc:  # noqa: BLE001
                self._defer_submission(principal, order_id, exc)
                continue
            results.append(
                self._complete_trigger(
                    principal,
                    order_id,
                    child_intent_id=child_intent_id,
                    evidence=evidence,
                )
            )
        return results

    def _claim_triggers(
        self, principal: TenantPrincipal, observation: TriggerObservation
    ) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        closed_groups: set[UUID] = set()
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT * FROM quant.paper_conditional_orders
                WHERE tenant_id=%s AND asset_id=%s AND reference_price=%s
                  AND status IN ('ARMED','PENDING_DATA')
                  AND COALESCE(trigger_source,'')<>'GROUP_PENDING'
                  AND (parent_intent_id IS NULL OR trigger_source='PARENT_ACTIVATED')
                ORDER BY created_at,conditional_order_id FOR UPDATE
                """,
                (
                    principal.tenant_id,
                    str(observation.asset_id),
                    str(observation.reference_price).upper(),
                ),
            )
            for raw in cur.fetchall():
                row = dict(raw)
                order_id = UUID(str(row["conditional_order_id"]))
                group_id = (
                    UUID(str(row["group_id"])) if row.get("group_id") else None
                )
                group_is_oco = bool(
                    group_id and row.get("group_policy") in {"OCO", "BRACKET"}
                )
                if group_is_oco and group_id in closed_groups:
                    continue
                prior_status = str(row["status"])
                last_ts = row.get("last_observation_ts")
                if last_ts is not None and observation.event_ts <= last_ts:
                    continue
                if row.get("expires_at") and observation.event_ts >= row["expires_at"]:
                    cur.execute(
                        """UPDATE quant.paper_conditional_orders SET status='EXPIRED',
                           state_version=state_version+1,updated_at=clock_timestamp()
                           WHERE tenant_id=%s AND conditional_order_id=%s""",
                        (principal.tenant_id, order_id),
                    )
                    self._append_event(
                        cur,
                        principal,
                        order_id,
                        "EXPIRED",
                        row["status"],
                        "EXPIRED",
                        "expiry_reached",
                        {},
                        f"expire:{order_id}",
                    )
                    continue
                triggered, trigger_price, watermark, reason = evaluate_trigger(
                    row, observation
                )
                next_status = (
                    "TRIGGERING"
                    if triggered
                    else "ARMED"
                    if observation.trustworthy
                    else "PENDING_DATA"
                )
                cur.execute(
                    """
                    UPDATE quant.paper_conditional_orders
                    SET status=%s,last_observation_ts=%s,last_observation_price=%s,
                        last_data_quality=%s,watermark=COALESCE(%s,watermark),
                        trigger_source=CASE WHEN %s='TRIGGERING' THEN %s ELSE trigger_source END,
                        trigger_ts=CASE WHEN %s='TRIGGERING' THEN %s ELSE trigger_ts END,
                        trigger_price=CASE WHEN %s='TRIGGERING' THEN %s ELSE trigger_price END,
                        trigger_data_quality=CASE WHEN %s='TRIGGERING' THEN %s ELSE trigger_data_quality END,
                        state_version=state_version+1,updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND conditional_order_id=%s
                    """,
                    (
                        next_status,
                        observation.event_ts,
                        observation.price,
                        observation.data_quality,
                        watermark,
                        next_status,
                        observation.source,
                        next_status,
                        observation.event_ts,
                        next_status,
                        trigger_price,
                        next_status,
                        observation.data_quality,
                        principal.tenant_id,
                        order_id,
                    ),
                )
                if triggered:
                    if group_is_oco:
                        cur.execute(
                            """SELECT conditional_order_id,status
                               FROM quant.paper_conditional_orders
                               WHERE tenant_id=%s AND group_id=%s
                                 AND conditional_order_id<>%s
                                 AND status IN ('ARMED','PENDING_DATA')
                               ORDER BY created_at,conditional_order_id FOR UPDATE""",
                            (principal.tenant_id, group_id, order_id),
                        )
                        peers = [dict(peer) for peer in cur.fetchall()]
                        cur.execute(
                            """UPDATE quant.paper_conditional_orders
                               SET status='CANCELLED',failure_reason='oco_peer_claimed',
                                   state_version=state_version+1,
                                   updated_at=clock_timestamp()
                               WHERE tenant_id=%s AND group_id=%s
                                 AND conditional_order_id<>%s
                                 AND status IN ('ARMED','PENDING_DATA')""",
                            (principal.tenant_id, group_id, order_id),
                        )
                        for peer in peers:
                            peer_id = UUID(str(peer["conditional_order_id"]))
                            self._append_event(
                                cur,
                                principal,
                                peer_id,
                                "OCO_PEER_CANCELLED",
                                str(peer["status"]),
                                "CANCELLED",
                                "oco_peer_claimed",
                                {"triggered_peer": str(order_id)},
                                f"oco-claim:{peer_id}:{order_id}",
                            )
                        closed_groups.add(group_id)
                    row.update(
                        status="TRIGGERING",
                        watermark=watermark,
                        trigger_source=observation.source,
                        trigger_ts=observation.event_ts,
                        trigger_price=trigger_price,
                        trigger_data_quality=observation.data_quality,
                    )
                    claimed.append(_json_value(row))
                    self._append_event(
                        cur,
                        principal,
                        order_id,
                        "TRIGGER_CLAIMED",
                        prior_status,
                        "TRIGGERING",
                        reason,
                        _json_value({"price": trigger_price}),
                        f"trigger:{order_id}:{observation.event_ts.isoformat()}",
                    )
                elif prior_status != next_status:
                    self._append_event(
                        cur,
                        principal,
                        order_id,
                        "DATA_STATE_CHANGED",
                        prior_status,
                        next_status,
                        reason,
                        _json_value(
                            {
                                "price": observation.price,
                                "data_quality": observation.data_quality,
                            }
                        ),
                        f"data-state:{order_id}:{observation.event_ts.isoformat()}",
                    )
            conn.commit()
        return claimed

    def _complete_trigger(
        self,
        principal: TenantPrincipal,
        order_id: UUID,
        *,
        child_intent_id: int,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """SELECT group_id,group_policy,status FROM quant.paper_conditional_orders
                   WHERE tenant_id=%s AND conditional_order_id=%s FOR UPDATE""",
                (principal.tenant_id, order_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ConditionalOrderNotFound("conditional order was not found")
            if row["status"] == "TRIGGERED":
                conn.commit()
                already_triggered = True
            else:
                already_triggered = False
            if already_triggered:
                pass
            elif row["status"] != "TRIGGERING":
                raise ConditionalOrderConflict(
                    "conditional order trigger was not claimed"
                )
            else:
                cur.execute(
                    """UPDATE quant.paper_conditional_orders
                   SET status='TRIGGERED',generated_child_intent_id=%s,
                       state_version=state_version+1,updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND conditional_order_id=%s""",
                    (child_intent_id, principal.tenant_id, order_id),
                )
                self._append_event(
                    cur,
                    principal,
                    order_id,
                    "CHILD_INTENT_GENERATED",
                    "TRIGGERING",
                    "TRIGGERED",
                    "normal_paper_child_intent_submitted",
                    {**_json_value(evidence), "child_intent_id": child_intent_id},
                    f"child:{order_id}:{child_intent_id}",
                )
            if (
                not already_triggered
                and row.get("group_id")
                and row.get("group_policy") in {"OCO", "BRACKET"}
            ):
                cur.execute(
                    """SELECT conditional_order_id,status
                       FROM quant.paper_conditional_orders
                       WHERE tenant_id=%s AND group_id=%s AND conditional_order_id<>%s
                         AND status IN ('ARMED','PENDING_DATA') FOR UPDATE""",
                    (principal.tenant_id, row["group_id"], order_id),
                )
                peers = [dict(peer) for peer in cur.fetchall()]
                cur.execute(
                    """
                    UPDATE quant.paper_conditional_orders
                    SET status='CANCELLED',failure_reason='oco_peer_triggered',
                        state_version=state_version+1,updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND group_id=%s AND conditional_order_id<>%s
                      AND status IN ('ARMED','PENDING_DATA')
                    """,
                    (principal.tenant_id, row["group_id"], order_id),
                )
                for peer in peers:
                    peer_id = UUID(str(peer["conditional_order_id"]))
                    self._append_event(
                        cur,
                        principal,
                        peer_id,
                        "OCO_PEER_CANCELLED",
                        peer["status"],
                        "CANCELLED",
                        "oco_peer_triggered",
                        {"triggered_peer": str(order_id)},
                        f"oco:{peer_id}:{order_id}",
                    )
            conn.commit()
        return self.get_order(principal, order_id)

    def process_parent_terminal(
        self,
        principal: TenantPrincipal,
        *,
        parent_intent_id: int,
        parent_status: str,
        event_ts: datetime,
        submit_child: ChildSubmitter,
    ) -> list[dict[str, Any]]:
        """Advance OTO/bracket children from an authoritative parent terminal state."""

        selected_parent_status = str(parent_status).upper()
        if selected_parent_status in PARENT_FAILURE_STATES:
            self._cancel_for_failed_parent(
                principal,
                parent_intent_id=parent_intent_id,
                parent_status=selected_parent_status,
                event_ts=event_ts,
            )
            return []
        if selected_parent_status not in PARENT_SUCCESS_STATES:
            return []
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """SELECT conditional_order_id,status FROM quant.paper_conditional_orders
                   WHERE tenant_id=%s AND parent_intent_id=%s
                     AND group_policy='BRACKET'
                     AND trigger_kind<>'PARENT_TERMINAL'
                     AND trigger_source='WAITING_PARENT'
                     AND status='PENDING_DATA'
                   ORDER BY created_at,conditional_order_id FOR UPDATE""",
                (principal.tenant_id, int(parent_intent_id)),
            )
            bracket_children = [dict(row) for row in cur.fetchall()]
            if bracket_children:
                cur.execute(
                    """UPDATE quant.paper_conditional_orders
                       SET status='ARMED',trigger_source='PARENT_ACTIVATED',
                           state_version=state_version+1,updated_at=clock_timestamp()
                       WHERE tenant_id=%s AND parent_intent_id=%s
                         AND group_policy='BRACKET'
                         AND trigger_kind<>'PARENT_TERMINAL'
                         AND trigger_source='WAITING_PARENT'
                         AND status='PENDING_DATA'""",
                    (principal.tenant_id, int(parent_intent_id)),
                )
                for row in bracket_children:
                    order_id = UUID(str(row["conditional_order_id"]))
                    self._append_event(
                        cur,
                        principal,
                        order_id,
                        "PARENT_ACTIVATED",
                        str(row["status"]),
                        "ARMED",
                        f"parent_{selected_parent_status.lower()}",
                        {"parent_intent_id": parent_intent_id},
                        f"parent-activate:{order_id}:{parent_intent_id}",
                    )
            cur.execute(
                """SELECT * FROM quant.paper_conditional_orders
                   WHERE tenant_id=%s AND parent_intent_id=%s
                     AND trigger_kind='PARENT_TERMINAL'
                     AND status IN ('ARMED','PENDING_DATA')
                   ORDER BY created_at,conditional_order_id FOR UPDATE""",
                (principal.tenant_id, int(parent_intent_id)),
            )
            candidates = [dict(row) for row in cur.fetchall()]
            for row in candidates:
                order_id = UUID(str(row["conditional_order_id"]))
                cur.execute(
                    """UPDATE quant.paper_conditional_orders
                       SET status='TRIGGERING',trigger_source='PARENT_ORDER_EVENT',
                           trigger_ts=%s,trigger_data_quality='AUTHORITATIVE_ORDER_STATE',
                           state_version=state_version+1,updated_at=clock_timestamp()
                       WHERE tenant_id=%s AND conditional_order_id=%s""",
                    (event_ts, principal.tenant_id, order_id),
                )
                self._append_event(
                    cur,
                    principal,
                    order_id,
                    "PARENT_TERMINAL_TRIGGER",
                    row["status"],
                    "TRIGGERING",
                    f"parent_{selected_parent_status.lower()}",
                    {"parent_intent_id": parent_intent_id},
                    f"parent-trigger:{order_id}:{parent_intent_id}",
                )
            conn.commit()
        results = []
        for candidate in candidates:
            order_id = UUID(str(candidate["conditional_order_id"]))
            evidence = {
                "trigger_source": "PARENT_ORDER_EVENT",
                "trigger_ts": event_ts,
                "data_quality": "AUTHORITATIVE_ORDER_STATE",
                "parent_intent_id": parent_intent_id,
            }
            try:
                child_intent_id = int(
                    submit_child(_json_value(candidate), _json_value(evidence))
                )
            except Exception as exc:  # noqa: BLE001
                self._defer_submission(principal, order_id, exc)
                continue
            results.append(
                self._complete_trigger(
                    principal,
                    order_id,
                    child_intent_id=child_intent_id,
                    evidence=evidence,
                )
            )
        return results

    def _cancel_for_failed_parent(
        self,
        principal: TenantPrincipal,
        *,
        parent_intent_id: int,
        parent_status: str,
        event_ts: datetime,
    ) -> None:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """SELECT conditional_order_id,status
                   FROM quant.paper_conditional_orders
                   WHERE tenant_id=%s AND parent_intent_id=%s
                     AND status IN ('ARMED','PENDING_DATA')
                   ORDER BY created_at,conditional_order_id FOR UPDATE""",
                (principal.tenant_id, int(parent_intent_id)),
            )
            children = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """UPDATE quant.paper_conditional_orders
                   SET status='CANCELLED',
                       failure_reason='parent_terminal_without_fill',
                       state_version=state_version+1,updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND parent_intent_id=%s
                     AND status IN ('ARMED','PENDING_DATA')""",
                (principal.tenant_id, int(parent_intent_id)),
            )
            for row in children:
                order_id = UUID(str(row["conditional_order_id"]))
                self._append_event(
                    cur,
                    principal,
                    order_id,
                    "PARENT_TERMINAL_CANCELLED",
                    str(row["status"]),
                    "CANCELLED",
                    f"parent_{parent_status.lower()}",
                    {
                        "parent_intent_id": parent_intent_id,
                        "parent_status": parent_status,
                        "event_ts": event_ts,
                    },
                    f"parent-cancel:{order_id}:{parent_intent_id}",
                )
            conn.commit()

    def _defer_submission(
        self, principal: TenantPrincipal, order_id: UUID, exc: Exception
    ) -> None:
        reason = f"child_submission_deferred:{type(exc).__name__}"
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_TRADE
        ) as (conn, cur, _):
            cur.execute(
                """UPDATE quant.paper_conditional_orders
                   SET failure_reason=%s,updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND conditional_order_id=%s
                     AND status='TRIGGERING'""",
                (reason, principal.tenant_id, order_id),
            )
            self._append_event(
                cur,
                principal,
                order_id,
                "CHILD_SUBMISSION_DEFERRED",
                "TRIGGERING",
                "TRIGGERING",
                reason,
                {},
                f"defer:{order_id}",
            )
            conn.commit()

    @staticmethod
    def _append_event(
        cur: Any,
        principal: TenantPrincipal,
        order_id: UUID,
        event_type: str,
        from_state: str | None,
        to_state: str,
        reason: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> None:
        cur.execute(
            """
            INSERT INTO quant.paper_conditional_order_events (
                tenant_id,conditional_order_id,event_type,from_state,to_state,
                reason,payload,idempotency_key
            ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (tenant_id,idempotency_key) DO NOTHING
            """,
            (
                principal.tenant_id,
                order_id,
                event_type,
                from_state,
                to_state,
                reason,
                json.dumps(_json_value(payload), sort_keys=True),
                idempotency_key,
            ),
        )
