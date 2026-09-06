"""Strict YAML configuration for no-submit and micro-live probe plans."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import yaml

from .calibration_domain import payload_hash, redact_mapping


PLACEHOLDERS = {"", "REQUIRED", "CHANGEME", "TODO", "NONE", "NULL"}


@dataclass(frozen=True)
class AccountPolicy:
    expected_funder_address: str
    expected_signature_type: int
    private_key_env: str
    api_key_env: str
    api_secret_env: str
    api_passphrase_env: str


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal
    max_daily_gross_notional: Decimal
    max_daily_realized_loss: Decimal
    max_position_per_market: Decimal
    max_open_orders: int
    max_probes_per_hour: int


@dataclass(frozen=True)
class MarketPolicy:
    allow_market_ids: tuple[str, ...]
    deny_sports_initially: bool
    deny_itode_initially: bool
    min_seconds_to_scheduled_close: int
    require_redundant_lob: bool
    require_rest_book_match: bool
    require_user_ws_connected: bool
    max_book_age_ms: int


@dataclass(frozen=True)
class SafetyPolicy:
    require_manual_run_approval: bool
    kill_switch_file: str
    cancel_all_on_shutdown: bool
    cancel_all_on_reconciliation_failure: bool


@dataclass(frozen=True)
class NetworkPolicy:
    clob_host: str
    chain_id: int
    proxy_url: str
    read_proxy_urls: tuple[str, ...]
    read_attempts: int
    read_retry_seconds: Decimal
    max_server_clock_offset_ms: int


@dataclass(frozen=True)
class ProbeExecution:
    count: int
    sides: tuple[str, ...]
    order_types: tuple[str, ...]
    amount: Decimal
    amount_unit: str
    interval_seconds: Decimal
    expected_outcome: str = "ANY"
    price_mode: str = "DEPTH_WALK"


@dataclass(frozen=True)
class ProbePlan:
    schema_version: str
    live_probe_enabled: bool
    account: AccountPolicy
    limits: RiskLimits
    market_policy: MarketPolicy
    safety: SafetyPolicy
    network: NetworkPolicy
    execution: ProbeExecution
    source_path: str

    @property
    def plan_hash(self) -> str:
        return payload_hash(self.redacted_dict(), prefix="plan-")

    def redacted_dict(self) -> dict[str, Any]:
        return redact_mapping(asdict(self))


def parse_probe_plan(path: Path | str) -> ProbePlan:
    """Parse a plan without accepting it for execution.

    This is intentionally separate from ``load_probe_plan`` so readiness tools
    can report every missing guardrail in an unfinished fail-closed template.
    Execution paths must continue to use ``load_probe_plan``.
    """

    source = Path(path).expanduser().resolve()
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("probe plan must be a YAML object")
    account = _mapping(payload, "account")
    limits = _mapping(payload, "limits")
    market = _mapping(payload, "market_policy")
    safety = _mapping(payload, "safety")
    network = _mapping(payload, "network")
    execution = _mapping(payload, "execution")
    plan = ProbePlan(
        schema_version=str(payload.get("schema_version") or "taker_calibration_plan_v1"),
        live_probe_enabled=_required_bool(payload, "live_probe_enabled"),
        account=AccountPolicy(
            expected_funder_address=_required_text(account, "expected_funder_address"),
            expected_signature_type=_required_int(account, "expected_signature_type"),
            private_key_env=_required_text(account, "private_key_env"),
            api_key_env=_required_text(account, "api_key_env"),
            api_secret_env=_required_text(account, "api_secret_env"),
            api_passphrase_env=_required_text(account, "api_passphrase_env"),
        ),
        limits=RiskLimits(
            max_order_notional=_required_decimal(limits, "max_order_notional"),
            max_daily_gross_notional=_required_decimal(limits, "max_daily_gross_notional"),
            max_daily_realized_loss=_required_decimal(limits, "max_daily_realized_loss"),
            max_position_per_market=_required_decimal(limits, "max_position_per_market"),
            max_open_orders=_required_int(limits, "max_open_orders"),
            max_probes_per_hour=_required_int(limits, "max_probes_per_hour"),
        ),
        market_policy=MarketPolicy(
            allow_market_ids=tuple(str(item) for item in market.get("allow_market_ids") or ()),
            deny_sports_initially=_required_bool(market, "deny_sports_initially"),
            deny_itode_initially=_required_bool(market, "deny_itode_initially"),
            min_seconds_to_scheduled_close=_required_int(market, "min_seconds_to_scheduled_close"),
            require_redundant_lob=_required_bool(market, "require_redundant_lob"),
            require_rest_book_match=_required_bool(market, "require_rest_book_match"),
            require_user_ws_connected=_required_bool(market, "require_user_ws_connected"),
            max_book_age_ms=_required_int(market, "max_book_age_ms"),
        ),
        safety=SafetyPolicy(
            require_manual_run_approval=_required_bool(safety, "require_manual_run_approval"),
            kill_switch_file=_required_text(safety, "kill_switch_file"),
            cancel_all_on_shutdown=_required_bool(safety, "cancel_all_on_shutdown"),
            cancel_all_on_reconciliation_failure=_required_bool(
                safety, "cancel_all_on_reconciliation_failure"
            ),
        ),
        network=NetworkPolicy(
            clob_host=_required_text(network, "clob_host"),
            chain_id=_required_int(network, "chain_id"),
            proxy_url=_required_text(network, "proxy_url"),
            read_proxy_urls=tuple(
                str(item).strip()
                for item in network.get("read_proxy_urls") or (network.get("proxy_url"),)
                if str(item or "").strip()
            ),
            read_attempts=int(network.get("read_attempts", 3)),
            read_retry_seconds=Decimal(str(network.get("read_retry_seconds", "0.5"))),
            max_server_clock_offset_ms=_required_int(network, "max_server_clock_offset_ms"),
        ),
        execution=ProbeExecution(
            count=_required_int(execution, "count"),
            sides=tuple(str(item).upper() for item in execution.get("sides") or ()),
            order_types=tuple(str(item).upper() for item in execution.get("order_types") or ()),
            amount=_required_decimal(execution, "amount"),
            amount_unit=_required_text(execution, "amount_unit").upper(),
            interval_seconds=_required_decimal(execution, "interval_seconds"),
            expected_outcome=str(execution.get("expected_outcome") or "ANY").upper(),
            price_mode=str(execution.get("price_mode") or "DEPTH_WALK").upper(),
        ),
        source_path=str(source),
    )
    return plan


def load_probe_plan(path: Path | str) -> ProbePlan:
    plan = parse_probe_plan(path)
    issues = validate_probe_plan(plan, live=False, require_credentials=False)
    structural = [issue for issue in issues if not issue.startswith("credential_env_missing:")]
    if structural:
        raise ValueError("invalid probe plan: " + "; ".join(structural))
    return plan


def validate_probe_plan(
    plan: ProbePlan,
    *,
    live: bool,
    require_credentials: bool = True,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    import os

    env = os.environ if environ is None else environ
    issues: list[str] = []
    if plan.account.expected_signature_type not in {0, 1, 2, 3}:
        issues.append("expected_signature_type_invalid")
    address = plan.account.expected_funder_address.lower()
    if _placeholder(address) or not address.startswith("0x") or len(address) != 42:
        issues.append("expected_funder_address_invalid")
    for name in (
        "private_key_env",
        "api_key_env",
        "api_secret_env",
        "api_passphrase_env",
    ):
        env_name = getattr(plan.account, name)
        if _placeholder(env_name):
            issues.append(f"{name}_invalid")
        elif require_credentials and not str(env.get(env_name) or "").strip():
            issues.append(f"credential_env_missing:{env_name}")
    for name in (
        "max_order_notional",
        "max_daily_gross_notional",
        "max_daily_realized_loss",
        "max_position_per_market",
    ):
        if getattr(plan.limits, name) <= 0:
            issues.append(f"{name}_must_be_positive")
    if plan.limits.max_open_orders != 1:
        issues.append("max_open_orders_must_equal_one")
    if plan.limits.max_probes_per_hour <= 0:
        issues.append("max_probes_per_hour_must_be_positive")
    if plan.market_policy.min_seconds_to_scheduled_close <= 0:
        issues.append("min_seconds_to_scheduled_close_must_be_positive")
    if plan.market_policy.max_book_age_ms <= 0:
        issues.append("max_book_age_ms_must_be_positive")
    if not plan.market_policy.require_redundant_lob:
        issues.append("require_redundant_lob_must_be_true")
    if not plan.market_policy.require_rest_book_match:
        issues.append("require_rest_book_match_must_be_true")
    if not plan.market_policy.require_user_ws_connected:
        issues.append("require_user_ws_connected_must_be_true")
    if not plan.safety.cancel_all_on_shutdown:
        issues.append("cancel_all_on_shutdown_must_be_true")
    if not plan.safety.cancel_all_on_reconciliation_failure:
        issues.append("cancel_all_on_reconciliation_failure_must_be_true")
    if _placeholder(plan.safety.kill_switch_file):
        issues.append("kill_switch_file_invalid")
    if plan.network.clob_host.rstrip("/") != "https://clob.polymarket.com":
        issues.append("clob_host_must_be_official_production_host")
    if plan.network.chain_id != 137:
        issues.append("chain_id_must_equal_polygon_mainnet_137")
    if _placeholder(plan.network.proxy_url):
        issues.append("proxy_url_is_required")
    if not plan.network.read_proxy_urls or any(
        _placeholder(proxy_url) for proxy_url in plan.network.read_proxy_urls
    ):
        issues.append("read_proxy_urls_are_required")
    if plan.network.read_attempts <= 0:
        issues.append("read_attempts_must_be_positive")
    if plan.network.read_retry_seconds < 0:
        issues.append("read_retry_seconds_must_be_nonnegative")
    if plan.network.max_server_clock_offset_ms <= 0:
        issues.append("max_server_clock_offset_ms_must_be_positive")
    if plan.execution.count <= 0:
        issues.append("probe_count_must_be_positive")
    if not plan.execution.sides or set(plan.execution.sides) - {"BUY", "SELL"}:
        issues.append("execution_sides_invalid")
    allowed_order_types = {"FAK", "FOK", "GTC", "GTD"}
    if not plan.execution.order_types or set(plan.execution.order_types) - allowed_order_types:
        issues.append("execution_order_types_invalid")
    if plan.execution.amount <= 0:
        issues.append("execution_amount_must_be_positive")
    if plan.execution.amount_unit not in {"QUOTE", "SHARES"}:
        issues.append("execution_amount_unit_invalid")
    market_types = set(plan.execution.order_types) & {"FAK", "FOK"}
    limit_types = set(plan.execution.order_types) & {"GTC", "GTD"}
    if market_types and limit_types:
        issues.append("execution_cannot_mix_market_and_limit_order_types")
    if (
        "BUY" in plan.execution.sides
        and market_types
        and plan.execution.amount_unit != "QUOTE"
    ):
        issues.append("market_buy_amount_unit_must_be_quote")
    if limit_types and plan.execution.amount_unit != "SHARES":
        issues.append("limit_order_amount_unit_must_be_shares")
    if plan.execution.interval_seconds < 0:
        issues.append("execution_interval_seconds_must_be_nonnegative")
    if plan.execution.expected_outcome not in {"ANY", "FILL", "PARTIAL", "REJECT"}:
        issues.append("execution_expected_outcome_invalid")
    if plan.execution.price_mode not in {"DEPTH_WALK", "BBO_ONLY"}:
        issues.append("execution_price_mode_invalid")
    if plan.execution.amount > plan.limits.max_order_notional and plan.execution.amount_unit == "QUOTE":
        issues.append("execution_amount_exceeds_max_order_notional")
    if live:
        if not plan.live_probe_enabled:
            issues.append("live_probe_enabled_is_false")
        if not plan.market_policy.allow_market_ids:
            issues.append("live_market_allowlist_empty")
    return sorted(set(issues))


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"probe plan section is required: {key}")
    return value


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    if key not in payload:
        raise ValueError(f"probe plan value is required: {key}")
    return str(payload[key]).strip()


def _required_bool(payload: Mapping[str, Any], key: str) -> bool:
    if key not in payload or not isinstance(payload[key], bool):
        raise ValueError(f"probe plan boolean is required: {key}")
    return bool(payload[key])


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    if key not in payload or isinstance(payload[key], bool):
        raise ValueError(f"probe plan integer is required: {key}")
    try:
        return int(payload[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"probe plan integer is invalid: {key}") from exc


def _required_decimal(payload: Mapping[str, Any], key: str) -> Decimal:
    if key not in payload or isinstance(payload[key], bool):
        raise ValueError(f"probe plan decimal is required: {key}")
    try:
        return Decimal(str(payload[key]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"probe plan decimal is invalid: {key}") from exc


def _placeholder(value: Any) -> bool:
    return str(value or "").strip().upper() in PLACEHOLDERS
