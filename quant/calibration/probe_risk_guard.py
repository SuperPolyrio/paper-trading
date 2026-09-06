"""Fail-closed preflight checks for no-submit and real micro-live probes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP
import os
from pathlib import Path
from typing import Any, Mapping

from .calibration_domain import ModelState, ProbeState, payload_hash
from .kill_switch import KillSwitch
from .market_taxonomy import normalize_market_domain
from .probe_plan import ProbePlan, validate_probe_plan


LIVE_ENABLE_PHRASE = "I_UNDERSTAND_REAL_ORDERS"


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    state: str
    checked_at: datetime
    issues: tuple[str, ...]
    checks: Mapping[str, Any]
    snapshot_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "state": self.state,
            "checked_at": self.checked_at.isoformat(),
            "issues": list(self.issues),
            "checks": dict(self.checks),
            "snapshot_hash": self.snapshot_hash,
        }


def evaluate_preflight(
    plan: ProbePlan,
    context: Mapping[str, Any],
    *,
    live: bool,
    run_id: str,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> PreflightResult:
    env = os.environ if environ is None else environ
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issues = validate_probe_plan(plan, live=live, require_credentials=True, environ=env)
    checks: dict[str, Any] = {
        "mode": "live" if live else "no-submit",
        "run_id": str(run_id),
        "model_state": str(context.get("model_state") or ""),
        "sdk_version": str(context.get("sdk_version") or ""),
        "kill_switch_file": plan.safety.kill_switch_file,
        "kill_switch_active": KillSwitch(Path(plan.safety.kill_switch_file)).active,
        "exchange_submit_allowed": False,
    }

    if checks["model_state"] != ModelState.CALIBRATING.value:
        issues.append("model_state_not_calibrating")
    if checks["sdk_version"] in {"", "NOT_INSTALLED", "UNKNOWN"}:
        issues.append("official_v2_sdk_unavailable")
    if checks["kill_switch_active"]:
        issues.append("kill_switch_active")

    observed_funder = str(context.get("observed_funder_address") or "").lower()
    expected_funder = plan.account.expected_funder_address.lower()
    checks["expected_funder_address"] = expected_funder
    checks["observed_funder_address"] = observed_funder or None
    if observed_funder != expected_funder:
        issues.append("funder_address_mismatch")

    observed_signature_type = _int(context.get("observed_signature_type"), -1)
    checks["observed_signature_type"] = observed_signature_type
    if observed_signature_type != plan.account.expected_signature_type:
        issues.append("signature_type_mismatch")

    clock_offset = abs(_decimal(context.get("server_clock_offset_ms"), Decimal("999999999")))
    checks["server_clock_offset_ms"] = str(clock_offset)
    if clock_offset > plan.network.max_server_clock_offset_ms:
        issues.append("server_clock_offset_exceeded")

    user_ws_connected = bool(context.get("user_ws_connected"))
    checks["user_ws_connected"] = user_ws_connected
    if plan.market_policy.require_user_ws_connected and not user_ws_connected:
        issues.append("user_ws_not_connected")

    matching_mode = str(context.get("matching_engine_mode") or "UNKNOWN").upper()
    checks["matching_engine_mode"] = matching_mode
    if matching_mode not in {"NORMAL", "ACTIVE"}:
        issues.append("matching_engine_not_normal")

    write_route_geoblock = (
        context.get("write_route_geoblock")
        if isinstance(context.get("write_route_geoblock"), Mapping)
        else {}
    )
    checks["write_route_geoblock"] = dict(write_route_geoblock)
    unified_admission = _unified_admission(write_route_geoblock)
    route_allowed = _write_route_allowed(write_route_geoblock)
    checks["write_route_admission"] = (
        dict(unified_admission) if unified_admission is not None else None
    )
    if live and not route_allowed:
        issues.append("write_route_geoblocked_or_unverified")

    candidate = context.get("candidate") if isinstance(context.get("candidate"), Mapping) else {}
    _check_candidate(plan, candidate, checked_at, issues, checks, live=live)

    side = str(context.get("side") or (plan.execution.sides[0] if plan.execution.sides else "")).upper()
    order_notional = _order_notional(plan, candidate, side)
    estimated_taker_fee = _estimated_taker_fee(plan, candidate, side)
    checks["order_notional"] = str(order_notional)
    checks["estimated_taker_fee"] = str(estimated_taker_fee)
    if order_notional <= 0 or order_notional > plan.limits.max_order_notional:
        issues.append("order_notional_outside_limit")
    daily_gross = _decimal(context.get("daily_gross_notional"))
    daily_loss_value = context.get("daily_realized_loss")
    daily_loss = abs(_decimal(daily_loss_value))
    market_position = abs(_decimal(context.get("market_position")))
    projected_market_position = _projected_market_position(
        plan,
        candidate,
        side,
        current_position=market_position,
    )
    open_orders = _int(context.get("open_orders_count"), 999999)
    hourly_submitted = _int(context.get("hourly_submitted_orders"), 0)
    checks.update(
        {
            "daily_gross_notional": str(daily_gross),
            "daily_realized_loss": str(daily_loss),
            "market_position": str(market_position),
            "projected_market_position": str(projected_market_position),
            "open_orders_count": open_orders,
            "hourly_submitted_orders": hourly_submitted,
        }
    )
    if daily_gross + order_notional > plan.limits.max_daily_gross_notional:
        issues.append("daily_gross_notional_limit_exceeded")
    if live and daily_loss_value in (None, ""):
        issues.append("daily_realized_loss_unavailable")
    elif daily_loss >= plan.limits.max_daily_realized_loss:
        issues.append("daily_realized_loss_limit_reached")
    if (
        side == "BUY"
        and projected_market_position > plan.limits.max_position_per_market
    ):
        issues.append("market_position_limit_reached")
    if open_orders >= plan.limits.max_open_orders:
        issues.append("open_order_limit_reached")
    if hourly_submitted >= plan.limits.max_probes_per_hour:
        issues.append("hourly_probe_limit_reached")

    collateral_balance = _decimal(context.get("collateral_balance"), Decimal("-1"))
    collateral_allowance = _decimal(context.get("collateral_allowance"), Decimal("-1"))
    token_balance = _decimal(context.get("conditional_token_balance"), Decimal("-1"))
    token_allowance = _decimal(context.get("conditional_token_allowance"), Decimal("-1"))
    checks["account_resource_checks"] = {
        "side": side,
        "collateral_balance": str(collateral_balance),
        "collateral_allowance": str(collateral_allowance),
        "collateral_allowance_spender": context.get(
            "collateral_allowance_spender"
        ),
        "collateral_allowance_source": context.get("collateral_allowance_source"),
        "conditional_token_balance": str(token_balance),
        "conditional_token_allowance": str(token_allowance),
        "conditional_token_allowance_spender": context.get(
            "conditional_token_allowance_spender"
        ),
        "conditional_token_allowance_source": context.get(
            "conditional_token_allowance_source"
        ),
    }
    if side == "BUY":
        required_collateral = order_notional + estimated_taker_fee
        checks["required_collateral_with_fee"] = str(required_collateral)
        checks["maximum_cash_outflow"] = str(required_collateral)
        if required_collateral > plan.limits.max_order_notional:
            issues.append("maximum_cash_outflow_outside_limit")
        if collateral_balance < required_collateral:
            issues.append("insufficient_collateral_balance")
        if collateral_allowance < required_collateral:
            issues.append("insufficient_collateral_allowance")
    elif side == "SELL":
        required_shares = plan.execution.amount
        if token_balance < required_shares:
            issues.append("insufficient_conditional_token_balance")
        if token_allowance < required_shares:
            issues.append("insufficient_conditional_token_allowance")

    if live and plan.safety.require_manual_run_approval:
        enable_phrase = str(env.get("POLY_QUANT_LIVE_PROBE_ENABLE") or "")
        run_approval = str(env.get("POLY_QUANT_LIVE_PROBE_APPROVAL") or "")
        checks["live_enable_phrase_present"] = enable_phrase == LIVE_ENABLE_PHRASE
        checks["manual_run_approval_matches"] = run_approval == str(run_id)
        if enable_phrase != LIVE_ENABLE_PHRASE:
            issues.append("live_enable_phrase_missing")
        if run_approval != str(run_id):
            issues.append("manual_run_approval_missing")
    elif live:
        checks["live_enable_phrase_present"] = None
        checks["manual_run_approval_matches"] = None
        checks["authorization_mode"] = "AUTO_WITHIN_CONFIGURED_LIMITS"
    checks["exchange_submit_allowed"] = live and not issues

    unique_issues = tuple(sorted(set(issues)))
    snapshot = {"checks": checks, "issues": unique_issues, "plan_hash": plan.plan_hash}
    return PreflightResult(
        passed=not unique_issues,
        state=(ProbeState.PREFLIGHT_OK if not unique_issues else ProbeState.RISK_ABORTED).value,
        checked_at=checked_at,
        issues=unique_issues,
        checks=checks,
        snapshot_hash=payload_hash(snapshot, prefix="risk-"),
    )


def _unified_admission(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    admission = snapshot.get("admission")
    return admission if isinstance(admission, Mapping) else None


def _write_route_allowed(snapshot: Mapping[str, Any]) -> bool:
    admission = _unified_admission(snapshot)
    if admission is not None:
        return admission.get("allowed") is True
    return snapshot.get("blocked") is False


def _check_candidate(
    plan: ProbePlan,
    candidate: Mapping[str, Any],
    now: datetime,
    issues: list[str],
    checks: dict[str, Any],
    *,
    live: bool,
) -> None:
    market_id = str(candidate.get("market_id") or "")
    source_category = candidate.get("source_category") or candidate.get("category")
    category = normalize_market_domain(
        source_category,
        market_title=candidate.get("market_title"),
        event_title=candidate.get("event_title"),
        market_slug=candidate.get("market_slug"),
    )
    coverage_grade = str(candidate.get("coverage_grade") or "")
    redundant = bool(candidate.get("redundant_feed_match"))
    has_gap = bool(candidate.get("has_gap", True))
    rest_match = bool(candidate.get("rest_book_match"))
    book_age_ms = _decimal(candidate.get("book_age_ms"), Decimal("999999999"))
    itode = bool(candidate.get("itode"))
    tick_size = _decimal(candidate.get("tick_size"), Decimal("-1"))
    min_order_size = _decimal(candidate.get("min_order_size"), Decimal("-1"))
    fee_rate_bps = candidate.get("fee_rate_bps")
    fee_rate = candidate.get("fee_rate")
    fee_exponent = candidate.get("fee_exponent")
    fee_taker_only = candidate.get("fee_taker_only")
    neg_risk = candidate.get("neg_risk")
    checks["candidate"] = {
        "market_id": market_id,
        "asset_id": str(candidate.get("asset_id") or ""),
        "market_state": str(candidate.get("market_state") or ""),
        "execution_eligible": bool(candidate.get("execution_eligible")),
        "coverage_grade": coverage_grade,
        "redundant_feed_match": redundant,
        "has_gap": has_gap,
        "rest_book_match": rest_match,
        "book_age_ms": str(book_age_ms),
        "checkpoint_book_age_ms": candidate.get("checkpoint_book_age_ms"),
        "book_validation_mode": candidate.get("book_validation_mode"),
        "rest_book_observed_at": candidate.get("rest_book_observed_at"),
        "rest_book_hash": candidate.get("rest_book_hash"),
        "category": category,
        "source_category": str(source_category or "unknown"),
        "itode": itode,
        "tick_size": str(tick_size),
        "min_order_size": str(min_order_size),
        "fee_rate_bps": fee_rate_bps,
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "fee_taker_only": fee_taker_only,
        "neg_risk": neg_risk,
    }
    if not market_id or not candidate.get("asset_id"):
        issues.append("candidate_identity_missing")
    if str(candidate.get("market_state") or "") != "LIVE" or not bool(candidate.get("execution_eligible")):
        issues.append("candidate_not_execution_live")
    if coverage_grade not in {"A_PLUS", "A"}:
        issues.append("candidate_coverage_not_a_or_a_plus")
    if plan.market_policy.require_redundant_lob and not redundant:
        issues.append("candidate_lob_not_redundant")
    if has_gap:
        issues.append("candidate_book_has_gap")
    if plan.market_policy.require_rest_book_match and not rest_match:
        issues.append("candidate_rest_book_mismatch")
    if book_age_ms > plan.market_policy.max_book_age_ms:
        issues.append("candidate_book_stale")
    if tick_size <= 0 or min_order_size <= 0:
        issues.append("market_order_constraints_missing")
    else:
        side = str(
            plan.execution.sides[0] if plan.execution.sides else ""
        ).upper()
        price_key = "best_ask" if side == "BUY" else "best_bid"
        price = _decimal(candidate.get(price_key), Decimal("0"))
        requested_shares = (
            plan.execution.amount
            if plan.execution.amount_unit == "SHARES"
            else (
                plan.execution.amount / price
                if price > 0
                else Decimal("0")
            )
        )
        checks["candidate"]["requested_shares"] = str(requested_shares)
        if requested_shares < min_order_size:
            issues.append("order_size_below_market_minimum")
    if fee_rate_bps in (None, "") or fee_rate in (None, "") or fee_exponent in (None, ""):
        issues.append("market_fee_metadata_missing")
    elif _decimal(fee_rate, Decimal("-1")) < 0 or _decimal(fee_exponent, Decimal("-1")) < 0:
        issues.append("market_fee_metadata_invalid")
    if not isinstance(fee_taker_only, bool):
        issues.append("market_fee_taker_flag_missing")
    if not isinstance(neg_risk, bool):
        issues.append("market_neg_risk_metadata_missing")
    if plan.market_policy.deny_sports_initially and category == "sports":
        issues.append("sports_market_denied")
    if plan.market_policy.deny_itode_initially and itode:
        issues.append("itode_market_denied")
    if live and market_id not in plan.market_policy.allow_market_ids:
        issues.append("market_not_in_live_allowlist")
    end_date = _time(candidate.get("end_date"))
    if end_date is None:
        issues.append("scheduled_close_missing")
    elif (end_date - now).total_seconds() < plan.market_policy.min_seconds_to_scheduled_close:
        issues.append("market_too_close_to_scheduled_close")


def _order_notional(plan: ProbePlan, candidate: Mapping[str, Any], side: str) -> Decimal:
    if plan.execution.amount_unit == "QUOTE":
        return plan.execution.amount
    price_key = "best_ask" if side == "BUY" else "best_bid"
    return plan.execution.amount * _decimal(candidate.get(price_key))


def _projected_market_position(
    plan: ProbePlan,
    candidate: Mapping[str, Any],
    side: str,
    *,
    current_position: Decimal,
) -> Decimal:
    if plan.execution.amount_unit == "SHARES":
        shares = plan.execution.amount
    else:
        price = _decimal(candidate.get("best_ask" if side == "BUY" else "best_bid"))
        if price <= 0:
            return Decimal("Infinity")
        shares = plan.execution.amount / price
    if side == "BUY":
        return current_position + shares
    if side == "SELL":
        return max(Decimal("0"), current_position - shares)
    return Decimal("Infinity")


def _estimated_taker_fee(
    plan: ProbePlan,
    candidate: Mapping[str, Any],
    side: str,
) -> Decimal:
    price = _decimal(candidate.get("best_ask" if side == "BUY" else "best_bid"))
    rate = max(Decimal("0"), _decimal(candidate.get("fee_rate")))
    exponent = max(Decimal("0"), _decimal(candidate.get("fee_exponent")))
    if price <= 0 or rate <= 0:
        return Decimal("0")
    shares = (
        plan.execution.amount / price
        if plan.execution.amount_unit == "QUOTE"
        else plan.execution.amount
    )
    base = price * (Decimal("1") - price)
    try:
        fee = shares * rate * (base**exponent)
    except Exception:
        fee = Decimal(str(float(shares) * float(rate) * (float(base) ** float(exponent))))
    return fee.quantize(Decimal("0.00001"), rounding=ROUND_UP)


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else default
    except Exception:
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
