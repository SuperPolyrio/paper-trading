"""Strict normalization for Data API account responses."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import ClosedPosition, OfficialPosition


def normalize_position(
    row: Mapping[str, Any], *, account_address: str
) -> OfficialPosition:
    asset_id = _required_text(row, "asset")
    condition_id = _required_text(row, "conditionId")
    proxy_wallet = str(row.get("proxyWallet") or account_address).lower()
    return OfficialPosition(
        account_address=proxy_wallet,
        asset_id=asset_id,
        condition_id=condition_id,
        size=_required_decimal(row, "size"),
        avg_price=_required_decimal(row, "avgPrice"),
        initial_value=_required_decimal(row, "initialValue"),
        gross_initial_value=_optional_decimal(row, "grossInitialValue"),
        entry_fees_usdc=_optional_decimal(row, "entryFeesUsdc"),
        current_value=_required_decimal(row, "currentValue"),
        cash_pnl=_required_decimal(row, "cashPnl"),
        realized_pnl=_required_decimal(row, "realizedPnl"),
        current_price=_required_decimal(row, "curPrice"),
        total_bought=_required_decimal(row, "totalBought"),
        redeemable=bool(row.get("redeemable")),
        mergeable=bool(row.get("mergeable")),
        title=str(row.get("title") or ""),
        slug=str(row.get("slug") or ""),
        outcome=str(row.get("outcome") or ""),
        outcome_index=_optional_int(row.get("outcomeIndex")),
        raw=dict(row),
    )


def normalize_closed_position(
    row: Mapping[str, Any], *, account_address: str
) -> ClosedPosition:
    return ClosedPosition(
        account_address=str(row.get("proxyWallet") or account_address).lower(),
        asset_id=_required_text(row, "asset"),
        condition_id=_required_text(row, "conditionId"),
        avg_price=_required_decimal(row, "avgPrice"),
        total_bought=_required_decimal(row, "totalBought"),
        realized_pnl=_required_decimal(row, "realizedPnl"),
        current_price=_required_decimal(row, "curPrice"),
        closed_at=_optional_timestamp(row.get("timestamp")),
        title=str(row.get("title") or ""),
        slug=str(row.get("slug") or ""),
        outcome=str(row.get("outcome") or ""),
        outcome_index=_optional_int(row.get("outcomeIndex")),
        raw=dict(row),
    )


def parse_decimal(
    value: Any, *, field_name: str, optional: bool = False
) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if optional:
            return None
        raise ValueError(f"official account field is missing: {field_name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"official account field is not Decimal: {field_name}"
        ) from exc
    if not parsed.is_finite():
        raise ValueError(f"official account field is not finite: {field_name}")
    return parsed


def parse_timestamp(value: Any, *, field_name: str) -> datetime:
    if value is None or str(value).strip() == "":
        raise ValueError(f"official account timestamp is missing: {field_name}")
    text = str(value).strip()
    try:
        if text.replace(".", "", 1).isdigit():
            raw = float(text)
            if raw > 10_000_000_000:
                raw /= 1000
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise ValueError(
            f"official account timestamp is invalid: {field_name}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_decimal(row: Mapping[str, Any], name: str) -> Decimal:
    value = parse_decimal(row.get(name), field_name=name)
    assert value is not None
    return value


def _optional_decimal(row: Mapping[str, Any], name: str) -> Decimal | None:
    return parse_decimal(row.get(name), field_name=name, optional=True)


def _required_text(row: Mapping[str, Any], name: str) -> str:
    value = str(row.get(name) or "").strip()
    if not value:
        raise ValueError(f"official account field is missing: {name}")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def _optional_timestamp(value: Any) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    return parse_timestamp(value, field_name="timestamp")
