"""Normalize a paper prediction to the exact pre-submit signed order amounts."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping

from quant.simulator.economics.fee_rounding import round_fee

BASE_UNITS = Decimal("1000000")


def signed_order_amounts(audit: Mapping[str, Any]) -> dict[str, Decimal]:
    side = str(audit.get("side") or "").upper()
    maker = Decimal(str(audit.get("maker_amount") or 0)) / BASE_UNITS
    taker = Decimal(str(audit.get("taker_amount") or 0)) / BASE_UNITS
    if maker <= 0 or taker <= 0:
        raise ValueError("signed order amounts must be positive")
    if side == "BUY":
        return {"quote": maker, "shares": taker}
    if side == "SELL":
        return {"quote": taker, "shares": maker}
    raise ValueError(f"unsupported signed order side: {side}")


def normalize_prediction_to_signed_order(
    prediction: Mapping[str, Any],
    audit: Mapping[str, Any],
    market: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay predicted fills using the exact amounts encoded in the order.

    For BUY orders, ``makerAmount`` is the quote budget and ``takerAmount`` sets
    the minimum shares at the signed worst price. Price improvement can therefore
    return more than ``takerAmount`` shares. SELL orders remain share-denominated.
    """

    normalized = dict(prediction)
    amounts = signed_order_amounts(audit)
    side = str(audit.get("side") or "").upper()
    order_type = str(audit.get("order_type") or "").upper()
    remaining = amounts["quote"] if side == "BUY" else amounts["shares"]
    worst_price = amounts["quote"] / amounts["shares"]
    fills: list[dict[str, Any]] = []
    for fill_index, source in enumerate(prediction.get("fills") or ()):
        if not isinstance(source, Mapping) or remaining <= 0:
            continue
        source_size = Decimal(str(source.get("size") or 0))
        price = Decimal(str(source.get("price") or 0))
        if source_size <= 0 or price <= 0:
            continue
        if side == "BUY" and price > worst_price:
            break
        size = (
            min(source_size, remaining / price)
            if side == "BUY"
            else min(
                source_size,
                remaining,
            )
        )
        fee_parts = _normalized_fill_fee(
            source=source,
            source_size=source_size,
            size=size,
            price=price,
            market=market,
        )
        fee_charge_id = source.get("fee_charge_id")
        if fee_charge_id:
            fee_charge_id = _signed_fee_charge_id(
                audit=audit,
                fill_index=fill_index,
                price=price,
                size=size,
                fee=fee_parts["fee"],
            )
        fills.append(
            {
                **dict(source),
                "size": format(size, "f"),
                "price": format(price, "f"),
                "fee": format(fee_parts["fee"], "f"),
                "platform_fee": format(fee_parts["platform_fee"], "f"),
                "builder_fee": format(fee_parts["builder_fee"], "f"),
                "fee_charge_id": fee_charge_id,
            }
        )
        remaining -= size * price if side == "BUY" else size
    remaining = max(Decimal("0"), remaining)
    tolerance = Decimal("0.000001")
    if order_type == "FOK" and remaining > tolerance:
        # FOK is atomic: tentative paper depth cannot become a partial fill.
        fills = []
        remaining = amounts["quote"] if side == "BUY" else amounts["shares"]
    filled_size = sum((Decimal(row["size"]) for row in fills), Decimal("0"))
    filled_notional = sum(
        (Decimal(row["size"]) * Decimal(row["price"]) for row in fills),
        Decimal("0"),
    )
    average_price = filled_notional / filled_size if filled_size > 0 else Decimal("0")
    total_fee = sum((Decimal(row["fee"]) for row in fills), Decimal("0"))
    original_amount = prediction.get("requested_amount")
    amount_unit = str(
        prediction.get("amount_unit") or audit.get("amount_unit") or ""
    ).upper()
    effective_amount = amounts["quote"] if amount_unit == "QUOTE" else amounts["shares"]
    if filled_size > 0 and remaining <= tolerance:
        status = "FILLED"
        reason = "arrival_book_walk_complete_signed_amount"
        remaining = Decimal("0")
    elif order_type == "FAK" and filled_size > 0:
        status = "PARTIAL"
        reason = "fak_remainder_cancelled_signed_amount"
    elif order_type == "FOK":
        status = "REJECTED"
        reason = "fok_insufficient_arrival_depth_signed_amount"
    else:
        status = "CANCELLED"
        reason = "no_marketable_arrival_depth_signed_amount"
    remaining_size = remaining if side == "SELL" else Decimal("0")
    remaining_amount = remaining
    slippage = _normalized_slippage(
        prediction=prediction,
        side=side,
        average_price=average_price,
        filled_size=filled_size,
    )
    normalized.update(
        fills=fills,
        status=status,
        reason=reason,
        requested_amount=format(effective_amount, "f"),
        approved_requested_amount=(
            str(original_amount)
            if original_amount is not None
            else str(audit.get("amount"))
        ),
        signed_quote_amount=format(amounts["quote"], "f"),
        signed_share_amount=format(amounts["shares"], "f"),
        signed_worst_price=format(worst_price, "f"),
        filled_size=format(filled_size, "f"),
        filled_notional=format(filled_notional, "f"),
        avg_fill_price=format(average_price, "f"),
        total_fee=format(total_fee, "f"),
        slippage=format(slippage, "f") if slippage is not None else None,
        remaining_size=format(remaining_size, "f"),
        remaining_amount=format(remaining_amount, "f"),
        signed_amount_normalized=True,
    )
    return normalized


def signed_execution_result_payload(
    staged_result: Mapping[str, Any],
    normalized_prediction: Mapping[str, Any],
    signed_order_audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a durable staged result to the exact, pre-submit signed amounts."""

    if not bool(normalized_prediction.get("signed_amount_normalized")):
        raise ValueError("prediction was not normalized to signed order amounts")
    staged = dict(staged_result)
    old_audit_key = str(staged.get("audit_key") or "").strip()
    if not old_audit_key:
        raise ValueError("staged execution result has no audit key")
    intent = dict(staged.get("intent") or {})
    if not intent:
        raise ValueError("staged execution result has no intent")
    for field in ("side", "order_type", "amount_unit"):
        staged_value = str(
            intent.get(field) if field != "order_type" else intent.get(field)
        ).upper()
        signed_value = str(signed_order_audit.get(field) or "").upper()
        if staged_value != signed_value:
            raise ValueError(
                f"signed order {field} differs from staged intent: "
                f"{signed_value}!={staged_value}"
            )
    if str(intent.get("asset_id") or "") != str(
        signed_order_audit.get("token_id") or ""
    ):
        raise ValueError("signed order token differs from staged intent")

    authoritative_fields = (
        "status",
        "reason",
        "fills",
        "requested_amount",
        "amount_unit",
        "filled_size",
        "remaining_size",
        "filled_notional",
        "remaining_amount",
        "avg_fill_price",
        "total_fee",
        "slippage",
    )
    for field in authoritative_fields:
        staged[field] = normalized_prediction.get(field)
    intent["size"] = normalized_prediction["requested_amount"]
    intent["limit_price"] = str(
        signed_order_audit.get("worst_price") or intent.get("limit_price")
    )
    staged["intent"] = intent
    fidelity = dict(staged.get("fidelity") or {})
    fidelity.update(
        {
            "staged_paper_audit_key": old_audit_key,
            "signed_amount_normalized": True,
            "signed_order_fingerprint": signed_order_audit.get(
                "signed_order_fingerprint"
            ),
            "signed_order_hash": signed_order_audit.get("order_hash"),
            "signed_quote_amount": normalized_prediction.get("signed_quote_amount"),
            "signed_share_amount": normalized_prediction.get("signed_share_amount"),
            "signed_worst_price": normalized_prediction.get("signed_worst_price"),
        }
    )
    staged["fidelity"] = fidelity
    digest_payload = {
        "schema_version": "signed-paper-execution-result-v1",
        "staged_paper_audit_key": old_audit_key,
        "signed_order_fingerprint": signed_order_audit.get("signed_order_fingerprint"),
        "signed_order_hash": signed_order_audit.get("order_hash"),
        "signed_maker_amount": signed_order_audit.get("maker_amount"),
        "signed_taker_amount": signed_order_audit.get("taker_amount"),
        "normalized_result": {
            field: staged.get(field) for field in authoritative_fields
        },
    }
    staged["audit_key"] = hashlib.sha256(
        json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    for fill_index, fill in enumerate(staged.get("fills") or ()):
        if not isinstance(fill, Mapping) or not fill.get("fee_charge_id"):
            continue
        fill = dict(fill)
        fill["fee_charge_id"] = f"fee-charge:{staged['audit_key']}:{fill_index}"
        staged["fills"][fill_index] = fill
    return staged


def _normalized_fill_fee(
    *,
    source: Mapping[str, Any],
    source_size: Decimal,
    size: Decimal,
    price: Decimal,
    market: Mapping[str, Any] | None,
) -> dict[str, Decimal]:
    details = market or {}
    rate_value = source.get("platform_fee_rate")
    if rate_value in (None, ""):
        rate_value = details.get("fee_rate")
    exponent_value = source.get("platform_fee_exponent")
    if exponent_value in (None, ""):
        exponent_value = details.get("fee_exponent")
    if rate_value not in (None, ""):
        rate = max(Decimal("0"), Decimal(str(rate_value)))
        exponent = max(Decimal("0"), Decimal(str(exponent_value or 0)))
        base = max(Decimal("0"), price * (Decimal("1") - price))
        try:
            raw_platform = size * rate * (base**exponent)
        except Exception:
            raw_platform = Decimal(
                str(float(size) * float(rate) * (float(base) ** float(exponent)))
            )
        platform_fee = round_fee(raw_platform)
    else:
        source_platform = Decimal(
            str(source.get("platform_fee") or source.get("fee") or 0)
        )
        platform_fee = round_fee(source_platform * size / source_size)
    builder_bps = int(source.get("builder_fee_rate_bps") or 0)
    if builder_bps:
        builder_fee = round_fee(
            size * price * Decimal(builder_bps) / Decimal(10_000)
        )
    else:
        source_builder = Decimal(str(source.get("builder_fee") or 0))
        builder_fee = round_fee(source_builder * size / source_size)
    return {
        "platform_fee": platform_fee,
        "builder_fee": builder_fee,
        "fee": platform_fee + builder_fee,
    }


def _signed_fee_charge_id(
    *,
    audit: Mapping[str, Any],
    fill_index: int,
    price: Decimal,
    size: Decimal,
    fee: Decimal,
) -> str:
    payload = "|".join(
        (
            str(audit.get("signed_order_fingerprint") or audit.get("order_hash") or ""),
            str(fill_index),
            format(price, "f"),
            format(size, "f"),
            format(fee, "f"),
        )
    )
    return f"fee-charge:{hashlib.sha256(payload.encode()).hexdigest()}"


def _normalized_slippage(
    *,
    prediction: Mapping[str, Any],
    side: str,
    average_price: Decimal,
    filled_size: Decimal,
) -> Decimal | None:
    if filled_size <= 0:
        return None
    old_average = prediction.get("avg_fill_price")
    old_slippage = prediction.get("slippage")
    if old_average in (None, "") or old_slippage in (None, ""):
        return None
    old_average_decimal = Decimal(str(old_average))
    old_slippage_decimal = Decimal(str(old_slippage))
    reference = (
        old_average_decimal - old_slippage_decimal
        if side == "BUY"
        else old_average_decimal + old_slippage_decimal
    )
    return average_price - reference if side == "BUY" else reference - average_price
