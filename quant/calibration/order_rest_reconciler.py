"""Merge User WS and authenticated REST into execution and ledger truth."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

TRADE_STATUSES = {"MATCHED", "MINED", "CONFIRMED", "RETRYING", "FAILED"}
FINAL_TRADE_STATUSES = {"CONFIRMED", "FAILED"}


def reconcile_order_lifecycle(
    *,
    order_id: str,
    user_ws_events: Iterable[Mapping[str, Any]],
    rest_order: Mapping[str, Any] | None,
    rest_trades: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    ws_rows = _dedupe(user_ws_events, source="user_ws")
    trade_rows = _dedupe(rest_trades, source="rest")
    correlated_ws = [row for row in ws_rows if correlates_order(row, order_id)]
    correlated_trades = [
        row for row in trade_rows if correlates_order(row, order_id)
    ]
    lifecycle_rows = [*correlated_ws, *correlated_trades]
    trade_groups = _trade_groups(lifecycle_rows)
    statuses = sorted({_status(row) for row in lifecycle_rows if _status(row)})
    final_status, final_errors = _final_trade_status(trade_groups)

    matched_rows = [
        _representative(rows, order_id=order_id) for rows in trade_groups.values()
    ]
    role_observations = [
        _liquidity_role(row, order_id) for row in matched_rows
    ]
    explicit_roles = {role for role in role_observations if role != "UNKNOWN"}
    if not explicit_roles:
        liquidity_role_truth = "UNKNOWN"
    elif explicit_roles == {"MAKER"}:
        liquidity_role_truth = "MAKER"
    elif explicit_roles == {"TAKER"}:
        liquidity_role_truth = "TAKER"
    else:
        liquidity_role_truth = "CONFLICT"
    matched_at_values = [
        observed_at
        for rows in trade_groups.values()
        if (observed_at := _first_trade_time(rows)) is not None
    ]
    actual_size = sum(
        (_order_trade_size(row, order_id) for row in matched_rows), Decimal("0")
    )
    actual_quote = sum(
        (_order_trade_notional(row, order_id) for row in matched_rows),
        Decimal("0"),
    )
    explicit_fees = [_trade_fee(row) for row in matched_rows]
    actual_fee = sum((fee for fee in explicit_fees if fee is not None), Decimal("0"))
    actual_fee_present = bool(explicit_fees) and all(fee is not None for fee in explicit_fees)
    actual_price = actual_quote / actual_size if actual_size > 0 else None
    order_status = _status(rest_order or {})
    execution_truth = "MATCHED" if matched_rows else order_status or "UNKNOWN"
    ledger_truth = final_status if final_status in FINAL_TRADE_STATUSES else "PENDING"
    errors: list[str] = []
    if not rest_order:
        errors.append("rest_order_missing")
    if not correlated_ws and not correlated_trades:
        errors.append("no_correlated_lifecycle_evidence")
    errors.extend(final_errors)
    return {
        "schema_version": "order_rest_reconciliation_v1",
        "order_id": str(order_id),
        "execution_truth": execution_truth,
        "ledger_truth": ledger_truth,
        "final_trade_status": final_status,
        "actual_matched_size": format(actual_size, "f"),
        "actual_quote_amount": format(actual_quote, "f"),
        "actual_avg_price": format(actual_price, "f") if actual_price is not None else None,
        "actual_fee": format(actual_fee, "f") if actual_fee_present else None,
        "liquidity_role_truth": liquidity_role_truth,
        "maker_role_observations": role_observations.count("MAKER"),
        "taker_role_observations": role_observations.count("TAKER"),
        "unknown_role_observations": role_observations.count("UNKNOWN"),
        "first_match_at": (
            _iso_timestamp(min(matched_at_values)) if matched_at_values else None
        ),
        "last_match_at": (
            _iso_timestamp(max(matched_at_values)) if matched_at_values else None
        ),
        "trade_observations": [
            {
                "trade_key": _trade_key(row),
                "size": format(_order_trade_size(row, order_id), "f"),
                "price": format(_order_trade_effective_price(row, order_id), "f"),
                "fee_rate_bps": _value(row, "fee_rate_bps"),
                "status": _status(row),
                "source": row.get("_reconciliation_source"),
                "liquidity_role": _liquidity_role(row, order_id),
            }
            for row in matched_rows
        ],
        "matched_trade_count": len(trade_groups),
        "observed_trade_statuses": statuses,
        "user_ws_event_count": len(correlated_ws),
        "rest_trade_count": len(correlated_trades),
        "rest_order_present": bool(rest_order),
        "rest_order_status": order_status or None,
        "rest_order_reconciled": bool(rest_order),
        "rest_trade_reconciled": bool(correlated_trades),
        "final_trade_status_present": final_status in FINAL_TRADE_STATUSES,
        "errors": sorted(set(errors)),
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
    }


def correlates_order(row: Mapping[str, Any], order_id: str) -> bool:
    """Return whether a venue event belongs to one exact order."""

    expected = str(order_id).lower()
    if not expected:
        return False
    direct = {
        str(row.get(key) or "").lower()
        for key in ("order_id", "orderID", "id", "taker_order_id", "order_hash")
        if row.get(key) not in (None, "")
    }
    maker_orders = row.get("maker_orders") if isinstance(row.get("maker_orders"), list) else []
    direct.update(
        str(item.get("order_id") or "").lower()
        for item in maker_orders
        if isinstance(item, Mapping)
    )
    return expected in direct


def _liquidity_role(row: Mapping[str, Any], order_id: str) -> str:
    expected = str(order_id).strip().lower()
    if not expected:
        return "UNKNOWN"
    maker_orders = (
        row.get("maker_orders") if isinstance(row.get("maker_orders"), list) else []
    )
    maker_ids = {
        str(item.get("order_id") or "").strip().lower()
        for item in maker_orders
        if isinstance(item, Mapping)
    }
    is_maker = expected in maker_ids
    is_taker = expected == str(row.get("taker_order_id") or "").strip().lower()
    if is_maker and is_taker:
        return "CONFLICT"
    if is_maker:
        return "MAKER"
    if is_taker:
        return "TAKER"
    return "UNKNOWN"


def _dedupe(rows: Iterable[Mapping[str, Any]], *, source: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in rows:
        row = dict(raw)
        key = (_trade_key(row), _status(row), _event_time(row))
        if key in seen:
            continue
        seen.add(key)
        result.append({**row, "_reconciliation_source": source})
    return result


def _trade_groups(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        if not _is_trade_row(raw):
            continue
        row = dict(raw)
        groups.setdefault(_trade_key(row), []).append(row)
    return groups


def _is_trade_row(row: Mapping[str, Any]) -> bool:
    event_type = str(row.get("event_type") or row.get("type") or "").upper()
    if event_type == "ORDER":
        return False
    if event_type == "TRADE":
        return _status(row) in TRADE_STATUSES
    has_trade_identity = any(
        row.get(key) not in (None, "")
        for key in ("trade_id", "tradeID", "transaction_hash", "tx_hash")
    )
    has_trade_values = any(
        row.get(key) not in (None, "")
        for key in ("matched_amount", "filled_size", "size", "fill_price", "price")
    )
    return _status(row) in TRADE_STATUSES and (has_trade_identity or has_trade_values)


def _final_trade_status(
    groups: Mapping[str, list[Mapping[str, Any]]],
) -> tuple[str | None, list[str]]:
    if not groups:
        return None, []
    outcomes: list[str] = []
    errors: list[str] = []
    for rows in groups.values():
        statuses = {_status(row) for row in rows}
        if "CONFIRMED" in statuses and "FAILED" in statuses:
            errors.append("conflicting_final_trade_status")
        latest = _status(max(rows, key=_status_sort_key))
        outcomes.append(latest)
    if any(status not in FINAL_TRADE_STATUSES for status in outcomes):
        return "PENDING", errors
    unique = set(outcomes)
    if len(unique) == 1:
        return outcomes[0], errors
    errors.append("mixed_final_trade_status")
    return "MIXED_FINAL", errors


def _representative(
    rows: list[Mapping[str, Any]], *, order_id: str
) -> Mapping[str, Any]:
    return max(
        rows,
        key=lambda row: (
            _order_trade_size(row, order_id) > 0,
            _order_trade_effective_price(row, order_id) > 0,
            _status_sort_key(row),
        ),
    )


def _trade_key(row: Mapping[str, Any]) -> str:
    for key in ("trade_id", "tradeID"):
        value = str(row.get(key) or "").strip().lower()
        if value:
            return f"trade:{value}"
    event_type = str(row.get("event_type") or row.get("type") or "").upper()
    event_id = str(row.get("id") or "").strip().lower()
    if event_id and (event_type == "TRADE" or _status(row) in TRADE_STATUSES):
        return f"trade:{event_id}"
    for key in ("transaction_hash", "tx_hash"):
        value = str(row.get(key) or "").strip().lower()
        if value:
            return f"transaction:{value}"
    return "fallback:" + "|".join(
        str(_value(row, key))
        for key in (
            "order_id",
            "orderID",
            "taker_order_id",
            "match_time",
            "price",
            "size",
        )
    )


def _event_time(row: Mapping[str, Any]) -> str:
    return str(row.get("last_update") or row.get("timestamp") or row.get("match_time") or "")


def _first_trade_time(rows: Iterable[Mapping[str, Any]]) -> float | None:
    values = [
        _time_sort_value(_event_time(row))
        for row in rows
        if _trade_size(row) > 0 and _event_time(row)
    ]
    positive = [value for value in values if value > 0]
    return min(positive) if positive else None


def _iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _status_sort_key(row: Mapping[str, Any]) -> tuple[float, int]:
    rank = {"MATCHED": 1, "MINED": 2, "RETRYING": 3, "CONFIRMED": 4, "FAILED": 4}
    return (_time_sort_value(_event_time(row)), rank.get(_status(row), 0))


def _time_sort_value(value: str) -> float:
    if not value:
        return 0.0
    try:
        numeric = float(value)
        return numeric / 1000 if numeric > 10_000_000_000 else numeric
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


def _trade_size(row: Mapping[str, Any]) -> Decimal:
    value = _value(row, "matched_amount", "filled_size", "size")
    return _base_units(value) if row.get("_reconciliation_source") == "rest" else _decimal(value)


def _trade_price(row: Mapping[str, Any]) -> Decimal:
    return _decimal(_value(row, "avg_fill_price", "fill_price", "price"))


def _trade_notional(row: Mapping[str, Any]) -> Decimal:
    size = _trade_size(row)
    maker_orders = row.get("maker_orders") if isinstance(row.get("maker_orders"), list) else []
    if not maker_orders or size <= 0:
        return size * _trade_price(row)

    taker_asset = str(row.get("asset_id") or "")
    taker_outcome = str(row.get("outcome") or "").strip().lower()
    maker_size = Decimal("0")
    maker_notional = Decimal("0")
    for item in maker_orders:
        if not isinstance(item, Mapping):
            return size * _trade_price(row)
        matched = _decimal(item.get("matched_amount"))
        price = _decimal(item.get("price"))
        if matched <= 0 or price <= 0 or price >= 1:
            return size * _trade_price(row)
        maker_asset = str(item.get("asset_id") or "")
        maker_outcome = str(item.get("outcome") or "").strip().lower()
        same_outcome = (
            bool(taker_asset and maker_asset and taker_asset == maker_asset)
            or bool(taker_outcome and maker_outcome and taker_outcome == maker_outcome)
        )
        taker_token_price = price if same_outcome else Decimal("1") - price
        maker_size += matched
        maker_notional += matched * taker_token_price

    tolerance = max(Decimal("0.000001"), size * Decimal("0.000001"))
    if abs(maker_size - size) > tolerance:
        return size * _trade_price(row)
    return maker_notional


def _trade_effective_price(row: Mapping[str, Any]) -> Decimal:
    size = _trade_size(row)
    return _trade_notional(row) / size if size > 0 else _trade_price(row)


def _matching_maker_orders(
    row: Mapping[str, Any], order_id: str
) -> list[Mapping[str, Any]]:
    expected = str(order_id).strip().lower()
    maker_orders = (
        row.get("maker_orders") if isinstance(row.get("maker_orders"), list) else []
    )
    return [
        item
        for item in maker_orders
        if isinstance(item, Mapping)
        and str(item.get("order_id") or "").strip().lower() == expected
    ]


def _order_trade_size(row: Mapping[str, Any], order_id: str) -> Decimal:
    maker_legs = _matching_maker_orders(row, order_id)
    if maker_legs:
        return sum(
            (_decimal(item.get("matched_amount")) for item in maker_legs),
            Decimal("0"),
        )
    return _trade_size(row)


def _order_trade_notional(row: Mapping[str, Any], order_id: str) -> Decimal:
    maker_legs = _matching_maker_orders(row, order_id)
    if not maker_legs:
        return _trade_notional(row)
    return sum(
        (
            _decimal(item.get("matched_amount")) * _decimal(item.get("price"))
            for item in maker_legs
        ),
        Decimal("0"),
    )


def _order_trade_effective_price(row: Mapping[str, Any], order_id: str) -> Decimal:
    size = _order_trade_size(row, order_id)
    if size <= 0:
        return _trade_price(row)
    return _order_trade_notional(row, order_id) / size


def _trade_fee(row: Mapping[str, Any]) -> Decimal | None:
    for key in ("fee_usdc", "feeUsdc", "fee", "fee_amount"):
        if row.get(key) not in (None, ""):
            value = row[key]
            return _base_units(value) if row.get("_reconciliation_source") == "rest" else _decimal(value)
    return None


def _status(row: Mapping[str, Any]) -> str:
    value = str(row.get("status") or row.get("type") or "").upper()
    for prefix in ("TRADE_STATUS_", "ORDER_STATUS_"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row[key]
    return 0


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _base_units(value: Any) -> Decimal:
    text = str(value or "0").strip()
    numeric = _decimal(text)
    return numeric / Decimal("1000000") if "." not in text and "e" not in text.lower() else numeric
