"""Polymarket CLOB V2 response and restricted-mode contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class VenueErrorDecision:
    code: str
    accepted: bool
    outcome_known: bool
    retry_read_only: bool
    retry_submit: bool
    fail_closed: bool
    retry_after_seconds: float | None = None


def normalize_order_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize V2 order responses without requiring inline transaction hashes."""

    payload = dict(response)
    order_id = _first(payload, "orderID", "order_id", "id")
    trade_ids = _string_list(
        _first(payload, "tradeIDs", "trade_ids", "tradeIds", "transactions", default=[])
    )
    transaction_hashes = _string_list(
        _first(
            payload,
            "transactionHashes",
            "transaction_hashes",
            "transactionHashes",
            default=[],
        )
    )
    success = payload.get("success")
    accepted = bool(order_id and success is not False)
    finality_state = (
        "TRADE_ID_ASSIGNED"
        if trade_ids and not transaction_hashes
        else "MATCHED"
        if transaction_hashes
        else "VENUE_ACCEPTED"
        if accepted
        else "REJECTED"
    )
    return {
        **payload,
        "order_id": str(order_id or ""),
        "trade_ids": trade_ids,
        "transaction_hashes": transaction_hashes,
        "accepted": accepted,
        "finality_state": finality_state,
        "requires_trade_reconciliation": bool(trade_ids and not transaction_hashes),
    }


def normalize_batch_order_response(
    response: Iterable[Mapping[str, Any]] | Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: Any = response
    if isinstance(response, Mapping):
        rows = (
            response.get("orders")
            or response.get("data")
            or response.get("results")
            or []
        )
    if not isinstance(rows, (list, tuple)):
        raise ValueError("batch order response must contain per-order rows")
    return [normalize_order_response(row) for row in rows if isinstance(row, Mapping)]


def classify_venue_error(
    status_code: int | None,
    payload: Mapping[str, Any] | str | None = None,
    *,
    retry_after: Any = None,
) -> VenueErrorDecision:
    text = _error_text(payload).lower()
    delay = _float_or_none(retry_after)
    if status_code == 400:
        code = (
            "INVALID_TICK"
            if "tick" in text
            else "BELOW_MIN_SIZE"
            if "minimum" in text or "min size" in text
            else "FOK_NOT_FULL"
            if "fully filled" in text or "fok" in text
            else "FAK_NO_MATCH"
            if "fak" in text or "no match" in text
            else "DUPLICATE"
            if "duplicate" in text
            else "BAD_REQUEST"
        )
        return VenueErrorDecision(code, False, True, False, False, True)
    if status_code == 425:
        return VenueErrorDecision(
            "ENGINE_RESTART", False, True, True, False, True, delay
        )
    if status_code == 429:
        return VenueErrorDecision("RATE_LIMIT", False, True, True, False, True, delay)
    if status_code == 503:
        code = "POST_ONLY_MODE" if "post" in text else "CANCEL_ONLY_MODE"
        return VenueErrorDecision(code, False, True, True, False, True, delay)
    if status_code is not None and 400 <= status_code < 500:
        return VenueErrorDecision(
            "HTTP_REJECTED", False, True, False, False, True, delay
        )
    return VenueErrorDecision(
        "SUBMIT_OUTCOME_UNKNOWN", False, False, True, False, True, delay
    )


def _first(payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return default


def _string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        value = value.values()
    if not isinstance(value, Iterable):
        return [str(value)]
    return [str(item) for item in value if item not in (None, "")]


def _error_text(payload: Mapping[str, Any] | str | None) -> str:
    if isinstance(payload, Mapping):
        return str(payload.get("error") or payload.get("message") or payload)
    return str(payload or "")


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
