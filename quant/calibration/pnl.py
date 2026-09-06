"""PnL primitives shared by live calibration reconciliation and its ledger."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


class CalibrationPnlError(RuntimeError):
    pass


@dataclass(frozen=True)
class PositionPnlState:
    quantity: Decimal = Decimal("0")
    cost_basis: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")


@dataclass(frozen=True)
class PositionPnlMutation:
    before: PositionPnlState
    after: PositionPnlState
    side: str
    size: Decimal
    price: Decimal
    fee: Decimal
    notional: Decimal
    cash_delta: Decimal
    realized_pnl_delta: Decimal


def apply_fill(
    state: PositionPnlState,
    *,
    side: str,
    size: Any,
    price: Any,
    fee: Any = 0,
) -> PositionPnlMutation:
    normalized_side = str(side or "").upper()
    fill_size = _decimal(size)
    fill_price = _decimal(price)
    fill_fee = max(Decimal("0"), _decimal(fee))
    if normalized_side not in {"BUY", "SELL"}:
        raise CalibrationPnlError(f"unsupported side: {side}")
    if fill_size < 0 or fill_price < 0:
        raise CalibrationPnlError("fill size and price must be non-negative")

    notional = fill_size * fill_price
    quantity = state.quantity
    cost_basis = state.cost_basis
    realized = state.realized_pnl
    if normalized_side == "BUY":
        cash_delta = -(notional + fill_fee)
        quantity += fill_size
        cost_basis += notional + fill_fee
        realized_delta = Decimal("0")
    else:
        if fill_size > quantity:
            raise CalibrationPnlError("sell size exceeds tracked position")
        average_cost = cost_basis / quantity if quantity > 0 else Decimal("0")
        allocated_cost = average_cost * fill_size
        cash_delta = notional - fill_fee
        quantity -= fill_size
        cost_basis = max(Decimal("0"), cost_basis - allocated_cost)
        realized_delta = cash_delta - allocated_cost
        realized += realized_delta
    if quantity == 0:
        cost_basis = Decimal("0")

    return PositionPnlMutation(
        before=state,
        after=PositionPnlState(
            quantity=quantity,
            cost_basis=cost_basis,
            realized_pnl=realized,
        ),
        side=normalized_side,
        size=fill_size,
        price=fill_price,
        fee=fill_fee,
        notional=notional,
        cash_delta=cash_delta,
        realized_pnl_delta=realized_delta,
    )


def apply_resolution(
    state: PositionPnlState,
    *,
    winning: bool,
) -> tuple[PositionPnlState, Decimal, Decimal]:
    payout = state.quantity if winning else Decimal("0")
    realized_delta = payout - state.cost_basis
    return (
        PositionPnlState(
            quantity=Decimal("0"),
            cost_basis=Decimal("0"),
            realized_pnl=state.realized_pnl + realized_delta,
        ),
        payout,
        realized_delta,
    )


def build_paired_pnl(
    row: Mapping[str, Any],
    truth: Mapping[str, Any],
    *,
    real_state: PositionPnlState | None = None,
    paper_state: PositionPnlState | None = None,
    tolerance: Any = "0.00001",
) -> tuple[dict[str, Any], PositionPnlState, PositionPnlState]:
    """Build real-vs-paper PnL from one reconciled fill.

    Without persisted states this is an isolated trade view. SELL reconciliation
    requires ledger-backed states because realized PnL depends on prior cost.
    """

    side = str(row.get("side") or "").upper()
    prediction = row.get("prediction") if isinstance(row.get("prediction"), Mapping) else {}
    real_size = _decimal(truth.get("actual_matched_size"))
    real_notional = _decimal(truth.get("actual_quote_amount")).quantize(
        Decimal("0.000001")
    ).normalize()
    real_price = (
        real_notional / real_size
        if real_size > 0 and real_notional > 0
        else _decimal(truth.get("actual_avg_price"))
    )
    real_fee = _actual_fee(row, truth)
    paper_size = _decimal(prediction.get("filled_size"))
    paper_price = _decimal(prediction.get("avg_fill_price"))
    paper_fee = _decimal(prediction.get("total_fee"))
    isolated = real_state is None or paper_state is None
    real_before = real_state or PositionPnlState()
    paper_before = paper_state or PositionPnlState()
    allowed = abs(_decimal(tolerance))

    if real_size <= 0 and paper_size <= 0:
        empty = {
            "schema_version": "calibration_pnl_reconciliation_v1",
            "status": "NO_FILL",
            "pnl_reconciled": True,
            "scope": "TRADE_LEVEL" if isolated else "PORTFOLIO_LEDGER",
            "real": _empty_leg(real_before),
            "paper": _empty_leg(paper_before),
            "difference": _difference_payload(Decimal("0"), Decimal("0"), None, None),
            "mark": _mark_payload(row),
            "tolerance": _format(allowed),
        }
        return empty, real_before, paper_before

    if side == "SELL" and isolated:
        payload = {
            "schema_version": "calibration_pnl_reconciliation_v1",
            "status": "PENDING_COST_BASIS",
            "pnl_reconciled": False,
            "scope": "TRADE_LEVEL",
            "reason": "sell realized PnL requires the persisted position cost basis",
            "mark": _mark_payload(row),
            "tolerance": _format(allowed),
        }
        return payload, real_before, paper_before

    try:
        real_mutation = apply_fill(
            real_before,
            side=side,
            size=real_size,
            price=real_price,
            fee=real_fee,
        )
        paper_mutation = apply_fill(
            paper_before,
            side=side,
            size=paper_size,
            price=paper_price,
            fee=paper_fee,
        )
    except CalibrationPnlError as exc:
        payload = {
            "schema_version": "calibration_pnl_reconciliation_v1",
            "status": "PENDING_COST_BASIS",
            "pnl_reconciled": False,
            "scope": "TRADE_LEVEL" if isolated else "PORTFOLIO_LEDGER",
            "reason": str(exc),
            "mark": _mark_payload(row),
            "tolerance": _format(allowed),
        }
        return payload, real_before, paper_before

    mark = _mark_payload(row)
    real_leg = _leg_payload(real_mutation, mark)
    paper_leg = _leg_payload(paper_mutation, mark)
    realized_error = abs(
        real_mutation.realized_pnl_delta - paper_mutation.realized_pnl_delta
    )
    real_unrealized = _optional_decimal(real_leg.get("unrealized_pnl"))
    paper_unrealized = _optional_decimal(paper_leg.get("unrealized_pnl"))
    unrealized_error = (
        abs(real_unrealized - paper_unrealized)
        if real_unrealized is not None and paper_unrealized is not None
        else None
    )
    mark_ready = mark["status"] == "READY" or (
        real_mutation.after.quantity == 0 and paper_mutation.after.quantity == 0
    )
    reconciled = realized_error <= allowed and (
        unrealized_error is None or unrealized_error <= allowed
    )
    status = (
        "PASS"
        if reconciled and mark_ready
        else "PENDING_MARK"
        if not mark_ready
        else "MISMATCH"
    )
    payload = {
        "schema_version": "calibration_pnl_reconciliation_v1",
        "status": status,
        "pnl_reconciled": reconciled and mark_ready,
        "scope": "TRADE_LEVEL" if isolated else "PORTFOLIO_LEDGER",
        "real": real_leg,
        "paper": paper_leg,
        "difference": _difference_payload(
            real_mutation.realized_pnl_delta,
            paper_mutation.realized_pnl_delta,
            real_unrealized,
            paper_unrealized,
        ),
        "mark": mark,
        "tolerance": _format(allowed),
    }
    return payload, real_mutation.after, paper_mutation.after


def _leg_payload(
    mutation: PositionPnlMutation,
    mark: Mapping[str, Any],
) -> dict[str, Any]:
    liquidation_price = _optional_decimal(mark.get("liquidation_price"))
    mark_value = (
        mutation.after.quantity * liquidation_price
        if liquidation_price is not None
        else None
    )
    unrealized = (
        mark_value - mutation.after.cost_basis if mark_value is not None else None
    )
    total = (
        mutation.after.realized_pnl + unrealized if unrealized is not None else None
    )
    return {
        "side": mutation.side,
        "filled_size": _format(mutation.size),
        "average_price": _format(mutation.price),
        "notional": _format(mutation.notional),
        "fee": _format(mutation.fee),
        "cash_delta": _format(mutation.cash_delta),
        "position_before": _format(mutation.before.quantity),
        "position_after": _format(mutation.after.quantity),
        "cost_basis_before": _format(mutation.before.cost_basis),
        "cost_basis_after": _format(mutation.after.cost_basis),
        "average_cost_after": (
            _format(mutation.after.cost_basis / mutation.after.quantity)
            if mutation.after.quantity > 0
            else "0"
        ),
        "realized_pnl_delta": _format(mutation.realized_pnl_delta),
        "realized_pnl_total": _format(mutation.after.realized_pnl),
        "liquidation_value": _format_optional(mark_value),
        "unrealized_pnl": _format_optional(unrealized),
        "total_pnl": _format_optional(total),
    }


def _empty_leg(state: PositionPnlState) -> dict[str, Any]:
    return {
        "filled_size": "0",
        "notional": "0",
        "fee": "0",
        "position_before": _format(state.quantity),
        "position_after": _format(state.quantity),
        "cost_basis_before": _format(state.cost_basis),
        "cost_basis_after": _format(state.cost_basis),
        "realized_pnl_delta": "0",
        "realized_pnl_total": _format(state.realized_pnl),
        "unrealized_pnl": None,
        "total_pnl": None,
    }


def _mark_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    market = row.get("market_snapshot") if isinstance(row.get("market_snapshot"), Mapping) else {}
    best_bid = _optional_decimal(market.get("best_bid"))
    best_ask = _optional_decimal(market.get("best_ask"))
    midpoint = (
        (best_bid + best_ask) / Decimal("2")
        if best_bid is not None and best_ask is not None
        else None
    )
    prediction = row.get("prediction") if isinstance(row.get("prediction"), Mapping) else {}
    return {
        "status": "READY" if best_bid is not None else "MISSING",
        "method": "CONSERVATIVE_EXECUTABLE_BID",
        "liquidation_price": _format_optional(best_bid),
        "best_bid": _format_optional(best_bid),
        "best_ask": _format_optional(best_ask),
        "midpoint_reference": _format_optional(midpoint),
        "checkpoint_id": market.get("shadow_checkpoint_id")
        or prediction.get("arrival_checkpoint_id"),
        "observed_at": market.get("shadow_observed_at") or market.get("observed_at"),
        "note": "unrealized PnL excludes the fee of a hypothetical future exit",
    }


def _actual_fee(row: Mapping[str, Any], truth: Mapping[str, Any]) -> Decimal:
    reconciliation = (
        row.get("reconciliation")
        if isinstance(row.get("reconciliation"), Mapping)
        else {}
    )
    if reconciliation.get("actual_fee") not in (None, ""):
        return _decimal(reconciliation.get("actual_fee"))
    if truth.get("actual_fee") not in (None, ""):
        return _decimal(truth.get("actual_fee"))
    lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), Mapping) else {}
    accounting = (
        lifecycle.get("accounting")
        if isinstance(lifecycle.get("accounting"), Mapping)
        else {}
    )
    return _decimal(accounting.get("fee"))


def _difference_payload(
    real_realized: Decimal,
    paper_realized: Decimal,
    real_unrealized: Decimal | None,
    paper_unrealized: Decimal | None,
) -> dict[str, Any]:
    return {
        "realized_pnl_error": _format(abs(real_realized - paper_realized)),
        "unrealized_pnl_error": (
            _format(abs(real_unrealized - paper_unrealized))
            if real_unrealized is not None and paper_unrealized is not None
            else None
        ),
        "total_pnl_error": (
            _format(
                abs(
                    (real_realized + real_unrealized)
                    - (paper_realized + paper_unrealized)
                )
            )
            if real_unrealized is not None and paper_unrealized is not None
            else None
        ),
    }


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except Exception:
        return Decimal("0")


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _format(value: Decimal) -> str:
    return format(value, "f")


def _format_optional(value: Decimal | None) -> str | None:
    return _format(value) if value is not None else None
