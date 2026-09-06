"""Official CLOB V2 wrapper with signing separated from submission."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from fractions import Fraction
import hashlib
from importlib import metadata
from math import gcd, lcm
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, TypeVar

import httpx

from quant.execution.venue_rules import classify_venue_error, normalize_order_response
from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    GeoblockSnapshot,
    MemoryAdmissionStore,
    PostgresAdmissionStore,
    UnifiedAdmissionService,
    order_exposure_effect,
)

from .calibration_domain import canonical_json, redact_mapping
from .kill_switch import KillSwitch
from .order_rest_reconciler import correlates_order
from .probe_plan import ProbePlan
from .probe_risk_guard import LIVE_ENABLE_PHRASE


_SDK_HTTP_LOCK = threading.Lock()
_READ_RESULT = TypeVar("_READ_RESULT")


class ClobV2AdapterError(RuntimeError):
    pass


class SubmitOutcomeUnknown(ClobV2AdapterError):
    def __init__(self, message: str, *, audit: SignedOrderAudit | None = None) -> None:
        super().__init__(message)
        self.audit = audit


class CancelOutcomeUnknown(ClobV2AdapterError):
    def __init__(self, message: str, *, order_id: str) -> None:
        super().__init__(message)
        self.order_id = str(order_id)


class OrderSubmissionRejected(ClobV2AdapterError):
    def __init__(
        self,
        message: str,
        *,
        audit: SignedOrderAudit | None = None,
        response: Mapping[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.audit = audit
        self.response = redact_mapping(dict(response or {}))
        self.status_code = status_code


class VenueMaintenance(OrderSubmissionRejected):
    pass


class VenueRestrictedMode(OrderSubmissionRejected):
    pass


class WriteRouteRestricted(ClobV2AdapterError):
    pass


@dataclass(frozen=True)
class SignedOrderAudit:
    sdk_name: str
    sdk_version: str
    signed_at: datetime
    sign_duration_ms: int
    order_hash: str
    signed_order_fingerprint: str
    signature_fingerprint: str
    maker: str
    signer: str
    signature_type: int
    token_id: str
    side: str
    order_type: str
    amount: str
    amount_unit: str
    worst_price: str
    maker_amount: str
    taker_amount: str
    timestamp: str
    metadata: str
    builder: str
    neg_risk: bool
    tick_size: str
    post_only: bool = False
    expiration: int = 0
    user_usdc_balance_provided: bool = False
    implied_price: str = ""
    exact_tick_aligned: bool = False
    exact_amount_rounding: bool = False
    exchange_submit_called: bool = False
    raw_signature_persisted: bool = False

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["signed_at"] = self.signed_at.isoformat()
        return row


@dataclass(frozen=True)
class AccountSnapshot:
    funder_address: str
    signer_address: str
    signature_type: int
    collateral: Mapping[str, Any]
    conditional: Mapping[str, Any]
    open_orders: tuple[Mapping[str, Any], ...]
    matching_engine_mode: str
    matching_engine_status: Mapping[str, Any]
    observed_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "funder_address": self.funder_address,
            "signer_address": self.signer_address,
            "signature_type": self.signature_type,
            "collateral": redact_mapping(self.collateral),
            "conditional": redact_mapping(self.conditional),
            "open_orders": [redact_mapping(row) for row in self.open_orders],
            "matching_engine_mode": self.matching_engine_mode,
            "matching_engine_status": redact_mapping(self.matching_engine_status),
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass(frozen=True)
class PreparedLiveOrder:
    """Opaque in-memory order; only the redacted audit may be persisted."""

    signed_order: Any = field(repr=False)
    audit: SignedOrderAudit


class PolymarketV2LiveAdapter:
    """Only component allowed to import the official trading SDK."""

    def __init__(
        self,
        plan: ProbePlan,
        *,
        environ: Mapping[str, str] | None = None,
        admission_service: UnifiedAdmissionService | None = None,
    ) -> None:
        self.plan = plan
        self.environ = os.environ if environ is None else environ
        self.submit_calls = 0
        self._sdk = self._load_sdk()
        from py_clob_client_v2.config import get_contract_config

        self._contract_config = get_contract_config(plan.network.chain_id)
        self._configure_sdk_http(plan.network.proxy_url)
        self._route_admission = admission_service or UnifiedAdmissionService(
            store=PostgresAdmissionStore(),
        )

    @property
    def sdk_version(self) -> str:
        try:
            return metadata.version("py-clob-client-v2")
        except metadata.PackageNotFoundError:
            return "NOT_INSTALLED"

    def get_server_clock_offset_ms(self) -> int:
        return self._read_with_failover(
            "server_time",
            lambda: self._server_clock_offset_ms(
                self._client(authenticated=False, signing=False)
            ),
        )

    def get_write_route_geoblock_snapshot(self) -> dict[str, Any]:
        """Check the pinned write route; this check must never use read failover."""

        proxy_url = str(self.plan.network.proxy_url or "").strip() or None
        attempts = max(1, int(self.plan.network.read_attempts))
        last_error: Exception | None = None
        payload: Any = None
        for attempt in range(attempts):
            try:
                with httpx.Client(proxy=proxy_url, timeout=5.0, trust_env=False) as client:
                    response = client.get("https://polymarket.com/api/geoblock")
                    response.raise_for_status()
                    payload = response.json()
                break
            except Exception as exc:
                last_error = exc
                if not _transient_read_error(exc) or attempt + 1 >= attempts:
                    raise WriteRouteRestricted(
                        "write route geoblock status unavailable via "
                        f"{proxy_url or 'direct'} after {attempt + 1} attempt(s)"
                    ) from exc
                delay = float(self.plan.network.read_retry_seconds) * (2**attempt)
                if delay > 0:
                    time.sleep(min(delay, 2.0))
        if last_error is not None and payload is None:
            raise WriteRouteRestricted(
                f"write route geoblock status unavailable via {proxy_url or 'direct'}"
            ) from last_error
        if not isinstance(payload, Mapping) or not isinstance(payload.get("blocked"), bool):
            raise WriteRouteRestricted("write route geoblock response is invalid")
        return {
            "blocked": bool(payload["blocked"]),
            "country": str(payload.get("country") or ""),
            "region": str(payload.get("region") or ""),
            "ip": str(payload.get("ip") or ""),
            "proxy_url": proxy_url,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def require_write_route_allowed(
        self,
        *,
        side: str = "BUY",
        asset_id: str | None = None,
        exposure_before: Decimal | None = None,
        exposure_after: Decimal | None = None,
    ) -> dict[str, Any]:
        snapshot = self.get_write_route_geoblock_snapshot()
        observed_at = datetime.fromisoformat(
            str(snapshot["observed_at"]).replace("Z", "+00:00")
        )
        raw_payload_hash = hashlib.sha256(
            canonical_json(
                {
                    "blocked": snapshot["blocked"],
                    "country": snapshot["country"],
                    "region": snapshot["region"],
                    "ip": snapshot["ip"],
                }
            ).encode("utf-8")
        ).hexdigest()
        geo = GeoblockSnapshot(
            blocked=bool(snapshot["blocked"]),
            country=str(snapshot["country"]),
            region=str(snapshot["region"]),
            detected_ip=str(snapshot["ip"]),
            observed_at=observed_at,
            expires_at=observed_at + timedelta(seconds=30),
            raw_payload_hash=raw_payload_hash,
            proxy_url=snapshot.get("proxy_url"),
        )
        effect = order_exposure_effect(side)
        route_admission = getattr(self, "_route_admission", None)
        if route_admission is None:
            route_admission = UnifiedAdmissionService(store=MemoryAdmissionStore())
            self._route_admission = route_admission
        account_policy = getattr(self.plan, "account", None)
        account_id = str(
            getattr(account_policy, "expected_funder_address", "")
            or "UNSPECIFIED_ACCOUNT"
        )
        decision = route_admission.decide(
            AdmissionRequest(
                request_id=(
                    f"live-route:{geo.snapshot_id}:{str(side).upper()}:"
                    f"{asset_id or 'UNSPECIFIED'}:{exposure_before}:{exposure_after}"
                ),
                operation=AdmissionOperation.ORDER,
                account_id=account_id,
                strategy_id=None,
                asset_id=asset_id,
                exposure_effect=effect,
                exposure_before=exposure_before,
                exposure_after=exposure_after,
                observed_at=observed_at,
                metadata={"source": "real_live_adapter"},
            ),
            geoblock_snapshot=geo,
        )
        snapshot["admission"] = decision.as_dict()
        if not decision.allowed:
            raise WriteRouteRestricted(
                "write route is not eligible for this order "
                f"(country={snapshot['country'] or 'UNKNOWN'}, "
                f"region={snapshot['region'] or 'UNKNOWN'}, "
                f"mode={decision.jurisdiction_mode.value}, effect={effect.value})"
            )
        return snapshot

    @staticmethod
    def _server_clock_offset_ms(client: Any) -> int:
        before = time.time()
        server_value = client.get_server_time()
        after = time.time()
        server_seconds = _server_seconds(server_value)
        local_midpoint = (before + after) / 2
        return int((server_seconds - local_midpoint) * 1000)

    def get_market_snapshot(self, *, asset_id: str, condition_id: str) -> dict[str, Any]:
        return self._read_with_failover(
            "market_snapshot",
            lambda: self._get_market_snapshot_once(
                asset_id=str(asset_id), condition_id=str(condition_id)
            ),
        )

    def _get_market_snapshot_once(self, *, asset_id: str, condition_id: str) -> dict[str, Any]:
        client = self._client(authenticated=False, signing=False)
        market_info = client.get_clob_market_info(str(condition_id))
        official_market = client.get_market(str(condition_id))
        if not isinstance(official_market, Mapping):
            raise ClobV2AdapterError("official market metadata response is invalid")
        rest_book = self._book_snapshot(client, asset_id=str(asset_id))
        fee_details = market_info.get("fd") if isinstance(market_info.get("fd"), Mapping) else {}
        active = bool(official_market.get("active"))
        closed = bool(official_market.get("closed"))
        accepting_orders = bool(official_market.get("accepting_orders"))
        category = _official_market_category(official_market)
        return {
            "asset_id": str(asset_id),
            "condition_id": str(condition_id),
            "tick_size": str(client.get_tick_size(str(asset_id))),
            "neg_risk": bool(client.get_neg_risk(str(asset_id))),
            "fee_rate_bps": int(client.get_fee_rate_bps(str(asset_id))),
            "fee_rate": str(fee_details.get("r", 0)),
            "fee_exponent": str(fee_details.get("e", 0)),
            "fee_taker_only": bool(fee_details.get("to", True)),
            "min_order_size": str(market_info.get("mos") or "0"),
            "itode": bool(market_info.get("itode")),
            "minimum_order_age_seconds": str(market_info.get("oas") or "0"),
            "end_date": official_market.get("end_date_iso"),
            "market_state": "LIVE" if active and not closed else "CLOSING",
            "execution_eligible": active and not closed and accepting_orders,
            "market_title": official_market.get("question"),
            "market_slug": official_market.get("market_slug"),
            "source_category": category,
            "category": category,
            "event_title": _official_event_title(official_market),
            **rest_book,
            "clob_market_info": redact_mapping(market_info),
            "official_market_info": redact_mapping(official_market),
            "matching_engine_public_ok": _engine_public_ok(client.get_ok()),
            "server_clock_offset_ms": self._server_clock_offset_ms(client),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_book_snapshot(self, *, asset_id: str) -> dict[str, Any]:
        """Fetch only the current REST book for a low-latency WS/REST BBO gate."""

        return self._read_with_failover(
            "book_snapshot",
            lambda: self._book_snapshot(
                self._client(authenticated=False, signing=False), asset_id=str(asset_id)
            ),
        )

    def get_reporting_book_snapshots(
        self,
        *,
        asset_ids: list[str] | tuple[str, ...],
        batch_size: int = 4,
        timeout_seconds: float = 2.0,
        attempts: int = 2,
    ) -> dict[str, dict[str, Any]]:
        """Return bounded, read-only REST marks for portfolio reporting.

        This intentionally uses the official public ``/books`` endpoint rather
        than the SDK's process-global HTTP client.  The trading/preflight path
        continues to use the SDK wrapper above; reporting needs bounded batch
        latency and must never hold up a PnL refresh behind a slow proxy.
        """

        unique_ids = tuple(dict.fromkeys(str(asset_id) for asset_id in asset_ids if asset_id))
        if not unique_ids:
            return {}
        batch_size = max(1, int(batch_size))
        results: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique_ids), batch_size):
            requested = unique_ids[start : start + batch_size]
            try:
                books = self._reporting_books_batch(
                    requested,
                    timeout_seconds=max(0.1, float(timeout_seconds)),
                    attempts=max(1, int(attempts)),
                )
            except Exception as exc:  # noqa: BLE001
                error = f"{exc.__class__.__name__}:{str(exc)[:200]}"
                results.update(
                    {
                        asset_id: {"reporting_book_error": error}
                        for asset_id in requested
                    }
                )
                continue
            by_asset = {
                str(book.get("asset_id") or ""): book
                for book in books
                if isinstance(book, Mapping)
            }
            for asset_id in requested:
                book = by_asset.get(asset_id)
                results[asset_id] = (
                    self._book_payload_snapshot(book)
                    if book is not None
                    else {"reporting_book_error": "asset_missing_from_books_response"}
                )
        return results

    @staticmethod
    def _book_snapshot(client: Any, *, asset_id: str) -> dict[str, Any]:
        rest_book = client.get_order_book(str(asset_id))
        if is_dataclass(rest_book):
            rest_payload = asdict(rest_book)
        elif isinstance(rest_book, Mapping):
            rest_payload = dict(rest_book)
        else:
            rest_payload = dict(vars(rest_book))
        return PolymarketV2LiveAdapter._book_payload_snapshot(rest_payload)

    @staticmethod
    def _book_payload_snapshot(rest_payload: Mapping[str, Any]) -> dict[str, Any]:
        rest_best_bid, rest_best_ask = _book_bbo(rest_payload)
        return {
            "rest_best_bid": str(rest_best_bid) if rest_best_bid is not None else None,
            "rest_best_ask": str(rest_best_ask) if rest_best_ask is not None else None,
            "rest_book_hash": (
                str(rest_payload.get("hash") or "")
            ),
            "min_order_size": str(rest_payload.get("min_order_size") or "0"),
            "tick_size": str(rest_payload.get("tick_size") or "0"),
            "neg_risk": bool(rest_payload.get("neg_risk")),
            "rest_book_observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _reporting_books_batch(
        self,
        asset_ids: tuple[str, ...],
        *,
        timeout_seconds: float,
        attempts: int,
    ) -> list[Mapping[str, Any]]:
        proxies = tuple(dict.fromkeys(self.plan.network.read_proxy_urls))
        errors: list[str] = []
        endpoint = f"{self.plan.network.clob_host.rstrip('/')}/books"
        for attempt in range(attempts):
            proxy_url = proxies[attempt % len(proxies)]
            try:
                with httpx.Client(
                    http2=True,
                    proxy=proxy_url,
                    trust_env=False,
                    timeout=httpx.Timeout(timeout_seconds),
                ) as client:
                    response = client.post(
                        endpoint,
                        json=[{"token_id": asset_id} for asset_id in asset_ids],
                    )
                    response.raise_for_status()
                    payload = response.json()
                if not isinstance(payload, list):
                    raise ClobV2AdapterError("official /books response is not a list")
                return [item for item in payload if isinstance(item, Mapping)]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{proxy_url}:{exc.__class__.__name__}:{str(exc)[:160]}")
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status_code is None or int(status_code) in {
                    408,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                if not retryable or attempt + 1 >= attempts:
                    raise ClobV2AdapterError(
                        "portfolio_reporting_books failed after "
                        f"{attempt + 1} read attempt(s): " + " | ".join(errors)
                    ) from exc
                delay = min(float(self.plan.network.read_retry_seconds), 0.5)
                if delay > 0:
                    time.sleep(delay)
        raise AssertionError("unreachable reporting books retry state")

    def get_account_snapshot(self, *, asset_id: str) -> AccountSnapshot:
        return self._read_with_failover(
            "account_snapshot",
            lambda: self._get_account_snapshot_once(asset_id=str(asset_id)),
        )

    def required_order_allowance(
        self,
        account: AccountSnapshot,
        *,
        asset_type: str,
        neg_risk: bool,
    ) -> dict[str, str]:
        """Resolve the allowance for the V2 exchange that signs this order."""

        config = self._contract_config
        spender = (
            config.neg_risk_exchange_v2 if neg_risk else config.exchange_v2
        )
        source = (
            account.collateral
            if str(asset_type).upper() == "COLLATERAL"
            else account.conditional
        )
        allowances = (
            source.get("allowances")
            if isinstance(source.get("allowances"), Mapping)
            else {}
        )
        normalized = {str(key).lower(): value for key, value in allowances.items()}
        allowance = normalized.get(str(spender).lower())
        return {
            "asset_type": str(asset_type).upper(),
            "spender": str(spender),
            "allowance": str(allowance if allowance not in (None, "") else "-1"),
            "source": "official_v2_contract_config",
        }

    def _get_account_snapshot_once(self, *, asset_id: str) -> AccountSnapshot:
        sdk = self._sdk
        client = self._client(authenticated=True, signing=True)
        collateral_raw = client.get_balance_allowance(
            sdk.BalanceAllowanceParams(
                asset_type=sdk.AssetType.COLLATERAL,
                signature_type=self.plan.account.expected_signature_type,
            )
        )
        conditional_raw = client.get_balance_allowance(
            sdk.BalanceAllowanceParams(
                asset_type=sdk.AssetType.CONDITIONAL,
                token_id=str(asset_id),
                signature_type=self.plan.account.expected_signature_type,
            )
        )
        orders = client.get_open_orders(only_first_page=False)
        closed_only = client.get_closed_only_mode()
        engine_mode = "CLOSED_ONLY" if _closed_only_enabled(closed_only) else "ACTIVE"
        return AccountSnapshot(
            funder_address=self.plan.account.expected_funder_address,
            signer_address=str(client.get_address()).lower(),
            signature_type=self.plan.account.expected_signature_type,
            collateral=_normalize_balance_allowance(collateral_raw),
            conditional=_normalize_balance_allowance(conditional_raw),
            open_orders=tuple(dict(row) for row in orders or ()),
            matching_engine_mode=engine_mode,
            matching_engine_status=redact_mapping(
                dict(closed_only) if isinstance(closed_only, Mapping) else {"value": closed_only}
            ),
            observed_at=datetime.now(timezone.utc),
        )

    def get_order_reconciliation_snapshot(
        self,
        *,
        order_id: str,
        condition_id: str,
        asset_id: str,
        after: int | None = None,
        before: int | None = None,
    ) -> dict[str, Any]:
        """Fetch authenticated REST truth with read-only route failover."""

        return self._read_with_failover(
            "order_reconciliation",
            lambda: self._get_order_reconciliation_snapshot_once(
                order_id=str(order_id),
                condition_id=str(condition_id),
                asset_id=str(asset_id),
                after=after,
                before=before,
            ),
        )

    def get_authenticated_trades(
        self,
        *,
        maker_address: str | None = None,
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the authenticated wallet trade history through the SDK wrapper."""

        return self._read_with_failover(
            "authenticated_trades",
            lambda: self._get_authenticated_trades_once(
                maker_address=str(
                    maker_address or self.plan.account.expected_funder_address
                ),
                after=after,
                before=before,
            ),
        )

    def _get_authenticated_trades_once(
        self,
        *,
        maker_address: str,
        after: int | None,
        before: int | None,
    ) -> list[dict[str, Any]]:
        client = self._client(authenticated=True, signing=True)
        trades = client.get_trades(
            self._sdk.TradeParams(
                maker_address=str(maker_address),
                after=after,
                before=before,
            ),
            only_first_page=False,
        )
        return [redact_mapping(dict(row)) for row in trades or ()]

    def _get_order_reconciliation_snapshot_once(
        self,
        *,
        order_id: str,
        condition_id: str,
        asset_id: str,
        after: int | None,
        before: int | None,
    ) -> dict[str, Any]:
        sdk = self._sdk
        client = self._client(authenticated=True, signing=True)
        order_lookup_error = None
        try:
            order = client.get_order(str(order_id))
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            order = {}
            order_lookup_error = "HTTP_404_ORDER_NOT_FOUND"
        trades = client.get_trades(
            sdk.TradeParams(
                market=str(condition_id),
                maker_address=str(self.plan.account.expected_funder_address),
                after=after,
                before=before,
            ),
            only_first_page=False,
        )
        open_orders = client.get_open_orders(
            sdk.OpenOrderParams(market=str(condition_id), asset_id=str(asset_id)),
            only_first_page=False,
        )
        redacted_trades = [redact_mapping(dict(row)) for row in trades or ()]
        correlated_trades = [
            row for row in redacted_trades if correlates_order(row, order_id)
        ]
        return {
            "order": redact_mapping(dict(order or {})),
            "trades": correlated_trades,
            "open_orders": [redact_mapping(dict(row)) for row in open_orders or ()],
            "order_lookup_error": order_lookup_error,
            "raw_trade_count": len(redacted_trades),
            "correlated_trade_count": len(correlated_trades),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "retry_performed": False,
        }

    def build_and_sign_no_submit(
        self,
        *,
        asset_id: str,
        side: str,
        order_type: str,
        amount: str,
        amount_unit: str,
        worst_price: str,
        tick_size: str,
        neg_risk: bool,
        user_usdc_balance: str | None = None,
    ) -> SignedOrderAudit:
        signed, audit = self._build_signed_market_order(
            asset_id=asset_id,
            side=side,
            order_type=order_type,
            amount=amount,
            amount_unit=amount_unit,
            worst_price=worst_price,
            tick_size=tick_size,
            neg_risk=neg_risk,
            user_usdc_balance=user_usdc_balance,
        )
        del signed
        if self.submit_calls != 0:
            raise RuntimeError("no-submit signing path observed an exchange submit call")
        return audit

    def build_and_submit_once(
        self,
        *,
        run_id: str,
        risk_passed: bool,
        asset_id: str,
        side: str,
        order_type: str,
        amount: str,
        amount_unit: str,
        worst_price: str,
        tick_size: str,
        neg_risk: bool,
        user_usdc_balance: str | None = None,
    ) -> tuple[SignedOrderAudit, Mapping[str, Any]]:
        prepared = self.prepare_live_order(
            asset_id=asset_id,
            side=side,
            order_type=order_type,
            amount=amount,
            amount_unit=amount_unit,
            worst_price=worst_price,
            tick_size=tick_size,
            neg_risk=neg_risk,
            user_usdc_balance=user_usdc_balance,
        )
        return self.submit_prepared_once(
            prepared,
            run_id=run_id,
            risk_passed=risk_passed,
        )

    def prepare_live_order(
        self,
        *,
        asset_id: str,
        side: str,
        order_type: str,
        amount: str,
        amount_unit: str,
        worst_price: str,
        tick_size: str,
        neg_risk: bool,
        user_usdc_balance: str | None = None,
    ) -> PreparedLiveOrder:
        self._configure_sdk_http(self.plan.network.proxy_url)
        signed, audit = self._build_signed_market_order(
            asset_id=asset_id,
            side=side,
            order_type=order_type,
            amount=amount,
            amount_unit=amount_unit,
            worst_price=worst_price,
            tick_size=tick_size,
            neg_risk=neg_risk,
            user_usdc_balance=user_usdc_balance,
        )
        return PreparedLiveOrder(signed_order=signed, audit=audit)

    def build_and_sign_limit_no_submit(
        self,
        *,
        asset_id: str,
        side: str,
        order_type: str,
        size: str,
        price: str,
        tick_size: str,
        neg_risk: bool,
        expiration: int = 0,
        post_only: bool = True,
        user_usdc_balance: str | None = None,
    ) -> SignedOrderAudit:
        signed, audit = self._build_signed_limit_order(
            asset_id=asset_id,
            side=side,
            order_type=order_type,
            size=size,
            price=price,
            tick_size=tick_size,
            neg_risk=neg_risk,
            expiration=expiration,
            post_only=post_only,
            user_usdc_balance=user_usdc_balance,
        )
        del signed
        if self.submit_calls != 0:
            raise RuntimeError("no-submit limit signing observed an exchange submit call")
        return audit

    def prepare_limit_order(
        self,
        *,
        asset_id: str,
        side: str,
        order_type: str,
        size: str,
        price: str,
        tick_size: str,
        neg_risk: bool,
        expiration: int = 0,
        post_only: bool = True,
        user_usdc_balance: str | None = None,
    ) -> PreparedLiveOrder:
        self._configure_sdk_http(self.plan.network.proxy_url)
        signed, audit = self._build_signed_limit_order(
            asset_id=asset_id,
            side=side,
            order_type=order_type,
            size=size,
            price=price,
            tick_size=tick_size,
            neg_risk=neg_risk,
            expiration=expiration,
            post_only=post_only,
            user_usdc_balance=user_usdc_balance,
        )
        return PreparedLiveOrder(signed_order=signed, audit=audit)

    def submit_prepared_once(
        self,
        prepared: PreparedLiveOrder,
        *,
        run_id: str,
        risk_passed: bool,
    ) -> tuple[SignedOrderAudit, Mapping[str, Any]]:
        self._require_live_authorization(run_id=run_id, risk_passed=risk_passed)
        # The write path is pinned to the primary route and is never retried.
        self._configure_sdk_http(self.plan.network.proxy_url)
        signed = prepared.signed_order
        audit = prepared.audit
        client = self._client(authenticated=True, signing=True)
        self.submit_calls += 1
        submitted = replace(audit, exchange_submit_called=True)
        try:
            response = client.post_order(
                signed,
                audit.order_type,
                post_only=bool(audit.post_only),
            )
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            error = getattr(exc, "error_msg", None)
            response_payload = (
                dict(error)
                if isinstance(error, Mapping)
                else {"error": str(error or exc)}
            )
            decision = classify_venue_error(
                status_code,
                response_payload,
                retry_after=getattr(exc, "retry_after", None),
            )
            if decision.code == "ENGINE_RESTART":
                raise VenueMaintenance(
                    f"matching engine maintenance for precomputed hash {audit.order_hash}",
                    audit=submitted,
                    response={
                        **response_payload,
                        "venue_error_code": decision.code,
                        "retry_after_seconds": decision.retry_after_seconds,
                    },
                    status_code=status_code,
                ) from exc
            if decision.code in {"CANCEL_ONLY_MODE", "POST_ONLY_MODE", "RATE_LIMIT"}:
                raise VenueRestrictedMode(
                    f"venue restricted mode {decision.code} for hash {audit.order_hash}",
                    audit=submitted,
                    response={
                        **response_payload,
                        "venue_error_code": decision.code,
                        "retry_after_seconds": decision.retry_after_seconds,
                    },
                    status_code=status_code,
                ) from exc
            if isinstance(status_code, int) and 400 <= status_code < 500:
                raise OrderSubmissionRejected(
                    f"order submission was rejected with HTTP {status_code} for hash {audit.order_hash}",
                    audit=submitted,
                    response={**response_payload, "venue_error_code": decision.code},
                    status_code=status_code,
                ) from exc
            raise SubmitOutcomeUnknown(
                f"order submission outcome is unknown for precomputed hash {audit.order_hash}",
                audit=submitted,
            ) from exc
        response_payload = (
            normalize_order_response(response)
            if isinstance(response, Mapping)
            else normalize_order_response({})
        )
        if not response_payload["accepted"]:
            raise OrderSubmissionRejected(
                f"order submission returned no accepted order id for hash {audit.order_hash}",
                audit=submitted,
                response=response_payload,
            )
        return submitted, redact_mapping(response_payload)

    def cancel_order(self, order_id: str) -> Mapping[str, Any]:
        """Cancel exactly one order on the pinned write route without retrying."""

        normalized = str(order_id or "").strip()
        if not normalized:
            raise ValueError("order_id is required for exact cancellation")
        self._configure_sdk_http(self.plan.network.proxy_url)
        client = self._client(authenticated=True, signing=True)
        try:
            response = client.cancel_order(self._sdk.OrderPayload(orderID=normalized))
        except Exception as exc:
            raise CancelOutcomeUnknown(
                f"exact cancellation outcome is unknown for order {normalized}",
                order_id=normalized,
            ) from exc
        if isinstance(response, Mapping):
            return redact_mapping(dict(response))
        return {"response": str(response)}

    def cancel_all(self) -> Mapping[str, Any]:
        """Emergency-only cancellation through the authenticated wrapper."""

        client = self._client(authenticated=True, signing=True)
        response = client.cancel_all()
        if isinstance(response, Mapping):
            return redact_mapping(dict(response))
        return {"response": str(response)}

    def _build_signed_market_order(
        self,
        *,
        asset_id: str,
        side: str,
        order_type: str,
        amount: str,
        amount_unit: str,
        worst_price: str,
        tick_size: str,
        neg_risk: bool,
        user_usdc_balance: str | None,
    ) -> tuple[Any, SignedOrderAudit]:
        side_text = str(side).upper()
        tif = str(order_type).upper()
        unit = str(amount_unit).upper()
        if tif not in {"FAK", "FOK"}:
            raise ValueError("micro-live adapter only accepts FAK/FOK market orders")
        if side_text == "BUY" and unit != "QUOTE":
            raise ValueError("Polymarket FAK/FOK BUY amount must use quote currency")
        if side_text == "SELL" and unit != "SHARES":
            raise ValueError("Polymarket FAK/FOK SELL amount must use shares")
        sdk = self._sdk
        side_value = _sdk_order_side(sdk, side_text)

        client = self._client(authenticated=False, signing=True)
        args = sdk.MarketOrderArgs(
            token_id=str(asset_id),
            amount=float(amount),
            side=side_value,
            price=float(worst_price),
            order_type=getattr(sdk.OrderType, tif),
            user_usdc_balance=(
                float(user_usdc_balance)
                if side_text == "BUY" and user_usdc_balance not in (None, "")
                else 0
            ),
        )
        options = sdk.PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=bool(neg_risk))
        started = time.perf_counter()
        exact_amount_rounding = _install_exact_market_order_amounts(client, sdk=sdk)
        signed = client.create_market_order(args, options=options)
        implied_price = _validate_signed_market_order_price(
            signed,
            side=side_text,
            tick_size=str(tick_size),
            worst_price=str(worst_price),
            requested_amount=str(amount),
        )
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        order_hash = _official_order_hash(client, signed, neg_risk=bool(neg_risk))
        signature = str(getattr(signed, "signature", ""))
        if not signature:
            raise ClobV2AdapterError("official SDK returned an unsigned order")
        safe_payload = _signed_order_safe_payload(signed)
        audit = SignedOrderAudit(
            sdk_name="py-clob-client-v2",
            sdk_version=self.sdk_version,
            signed_at=datetime.now(timezone.utc),
            sign_duration_ms=duration_ms,
            order_hash=order_hash,
            signed_order_fingerprint=_sha256(canonical_json(safe_payload) + signature),
            signature_fingerprint=_sha256(signature),
            maker=str(signed.maker).lower(),
            signer=str(signed.signer).lower(),
            signature_type=int(signed.signatureType),
            token_id=str(signed.tokenId),
            side=side_text,
            order_type=tif,
            amount=str(amount),
            amount_unit=unit,
            worst_price=str(worst_price),
            maker_amount=str(signed.makerAmount),
            taker_amount=str(signed.takerAmount),
            timestamp=str(signed.timestamp),
            metadata=str(signed.metadata),
            builder=str(signed.builder),
            neg_risk=bool(neg_risk),
            tick_size=str(tick_size),
            user_usdc_balance_provided=(
                side_text == "BUY" and user_usdc_balance not in (None, "")
            ),
            implied_price=implied_price,
            exact_tick_aligned=True,
            exact_amount_rounding=exact_amount_rounding,
        )
        if audit.maker != self.plan.account.expected_funder_address.lower():
            raise ClobV2AdapterError("signed order maker does not match expected funder")
        if audit.signature_type != self.plan.account.expected_signature_type:
            raise ClobV2AdapterError("signed order signature type does not match frozen plan")
        return signed, audit

    def _build_signed_limit_order(
        self,
        *,
        asset_id: str,
        side: str,
        order_type: str,
        size: str,
        price: str,
        tick_size: str,
        neg_risk: bool,
        expiration: int,
        post_only: bool,
        user_usdc_balance: str | None,
    ) -> tuple[Any, SignedOrderAudit]:
        side_text = str(side).upper()
        tif = str(order_type).upper()
        if side_text not in {"BUY", "SELL"}:
            raise ValueError("limit order side must be BUY or SELL")
        if tif not in {"GTC", "GTD"}:
            raise ValueError("limit order type must be GTC or GTD")
        if not post_only:
            raise ValueError("maker calibration limit orders must be post-only")
        if tif == "GTD" and int(expiration) < int(time.time()) + 180:
            raise ValueError("GTD stated expiration must be at least 3 minutes in the future")
        if tif == "GTC" and int(expiration) != 0:
            raise ValueError("GTC expiration must be zero")

        requested_size = Decimal(str(size))
        requested_price = Decimal(str(price))
        tick = Decimal(str(tick_size))
        if requested_size <= 0:
            raise ValueError("limit order size must be positive")
        if requested_price <= 0 or requested_price >= 1:
            raise ValueError("limit order price must be between zero and one")
        if tick <= 0 or (requested_price / tick) != (requested_price / tick).to_integral_value():
            raise ValueError("limit order price must be exactly tick aligned")

        sdk = self._sdk
        client = self._client(authenticated=False, signing=True)
        args = sdk.OrderArgs(
            token_id=str(asset_id),
            price=float(requested_price),
            size=float(requested_size),
            side=_sdk_order_side(sdk, side_text),
            expiration=int(expiration),
            user_usdc_balance=(
                float(user_usdc_balance)
                if side_text == "BUY" and user_usdc_balance not in (None, "")
                else None
            ),
        )
        options = sdk.PartialCreateOrderOptions(
            tick_size=str(tick_size),
            neg_risk=bool(neg_risk),
        )
        started = time.perf_counter()
        signed = client.create_order(args, options=options)
        implied_price, exact_size = _validate_signed_limit_order(
            signed,
            side=side_text,
            price=requested_price,
            size=requested_size,
            tick_size=tick,
        )
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        order_hash = _official_order_hash(client, signed, neg_risk=bool(neg_risk))
        signature = str(getattr(signed, "signature", ""))
        if not signature:
            raise ClobV2AdapterError("official SDK returned an unsigned limit order")
        safe_payload = _signed_order_safe_payload(signed)
        audit = SignedOrderAudit(
            sdk_name="py-clob-client-v2",
            sdk_version=self.sdk_version,
            signed_at=datetime.now(timezone.utc),
            sign_duration_ms=duration_ms,
            order_hash=order_hash,
            signed_order_fingerprint=_sha256(canonical_json(safe_payload) + signature),
            signature_fingerprint=_sha256(signature),
            maker=str(signed.maker).lower(),
            signer=str(signed.signer).lower(),
            signature_type=int(signed.signatureType),
            token_id=str(signed.tokenId),
            side=side_text,
            order_type=tif,
            amount=format(requested_size, "f"),
            amount_unit="SHARES",
            worst_price=format(requested_price, "f"),
            maker_amount=str(signed.makerAmount),
            taker_amount=str(signed.takerAmount),
            timestamp=str(signed.timestamp),
            metadata=str(signed.metadata),
            builder=str(signed.builder),
            neg_risk=bool(neg_risk),
            tick_size=str(tick_size),
            post_only=True,
            expiration=int(expiration),
            user_usdc_balance_provided=(
                side_text == "BUY" and user_usdc_balance not in (None, "")
            ),
            implied_price=implied_price,
            exact_tick_aligned=True,
            exact_amount_rounding=exact_size,
        )
        if audit.maker != self.plan.account.expected_funder_address.lower():
            raise ClobV2AdapterError("signed limit order maker does not match expected funder")
        if audit.signature_type != self.plan.account.expected_signature_type:
            raise ClobV2AdapterError(
                "signed limit order signature type does not match frozen plan"
            )
        return signed, audit

    def _client(self, *, authenticated: bool, signing: bool) -> Any:
        key = self._credential(self.plan.account.private_key_env) if signing else None
        creds = None
        if authenticated:
            sdk = self._sdk
            creds = sdk.ApiCreds(
                api_key=self._credential(self.plan.account.api_key_env),
                api_secret=self._credential(self.plan.account.api_secret_env),
                api_passphrase=self._credential(self.plan.account.api_passphrase_env),
            )
        return self._sdk.ClobClient(
            self.plan.network.clob_host,
            chain_id=self.plan.network.chain_id,
            key=key,
            creds=creds,
            signature_type=self.plan.account.expected_signature_type if signing else None,
            funder=self.plan.account.expected_funder_address if signing else None,
            use_server_time=True,
            retry_on_error=False,
        )

    def _read_with_failover(
        self,
        operation: str,
        callback: Callable[[], _READ_RESULT],
    ) -> _READ_RESULT:
        read_proxies = tuple(dict.fromkeys(self.plan.network.read_proxy_urls))
        write_proxy = str(getattr(self.plan.network, "proxy_url", "") or "").strip()
        # A healthy, geoblock-cleared write route is also a valid last-resort
        # source for public CLOB reads. Auxiliary read routes remain preferred,
        # but their transient failure must not strand an otherwise valid probe.
        proxies = tuple(
            dict.fromkeys(
                (*read_proxies, *((write_proxy,) if write_proxy else ()))
            )
        )
        attempts = max(1, self.plan.network.read_attempts, len(proxies))
        errors: list[str] = []
        for attempt in range(attempts):
            proxy_url = proxies[attempt % len(proxies)]
            self._configure_sdk_http(proxy_url, timeout_seconds=5.0)
            try:
                return callback()
            except Exception as exc:
                errors.append(f"{proxy_url}:{exc.__class__.__name__}:{str(exc)[:160]}")
                if not _transient_read_error(exc) or attempt + 1 >= attempts:
                    raise ClobV2AdapterError(
                        f"{operation} failed after {attempt + 1} read attempt(s): "
                        + " | ".join(errors)
                    ) from exc
                delay = float(self.plan.network.read_retry_seconds) * (2**attempt)
                if delay > 0:
                    time.sleep(min(delay, 2.0))
        raise AssertionError("unreachable read retry state")

    def _credential(self, name: str) -> str:
        value = str(self.environ.get(name) or "").strip()
        if not value:
            raise ClobV2AdapterError(f"required credential environment variable is missing: {name}")
        return value

    def _require_live_authorization(self, *, run_id: str, risk_passed: bool) -> None:
        if not risk_passed:
            raise ClobV2AdapterError("live submission blocked because preflight did not pass")
        if not self.plan.live_probe_enabled:
            raise ClobV2AdapterError("live submission is disabled in the frozen plan")
        KillSwitch(Path(self.plan.safety.kill_switch_file)).require_clear()
        if self.plan.safety.require_manual_run_approval:
            if str(self.environ.get("POLY_QUANT_LIVE_PROBE_ENABLE") or "") != LIVE_ENABLE_PHRASE:
                raise ClobV2AdapterError("live submission enable phrase is absent")
            if str(self.environ.get("POLY_QUANT_LIVE_PROBE_APPROVAL") or "") != str(run_id):
                raise ClobV2AdapterError("manual run approval does not match run_id")

    @staticmethod
    def _load_sdk() -> Any:
        try:
            import py_clob_client_v2 as sdk
        except Exception as exc:
            raise ClobV2AdapterError("py-clob-client-v2==1.1.0 is unavailable") from exc
        return sdk

    @staticmethod
    def _configure_sdk_http(proxy_url: str, *, timeout_seconds: float = 15.0) -> None:
        from py_clob_client_v2.http_helpers import helpers

        with _SDK_HTTP_LOCK:
            old = getattr(helpers, "_http_client", None)
            if old is not None:
                old.close()
            helpers._http_client = httpx.Client(
                http2=True,
                proxy=str(proxy_url),
                trust_env=False,
                timeout=httpx.Timeout(float(timeout_seconds)),
            )


def _official_order_hash(client: Any, signed: Any, *, neg_risk: bool) -> str:
    from py_clob_client_v2.config import get_contract_config
    from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2

    config = get_contract_config(client.chain_id)
    exchange = config.neg_risk_exchange_v2 if neg_risk else config.exchange_v2
    builder = ExchangeOrderBuilderV2(exchange, client.chain_id, client.builder.signer)
    typed_data = builder.build_order_typed_data(signed)
    return builder.build_order_hash(typed_data)


def _install_exact_market_order_amounts(client: Any, *, sdk: Any) -> bool:
    """Apply the integer-lot calculation proposed in upstream PR #60."""

    builder = getattr(client, "builder", None)
    if builder is None or not hasattr(builder, "get_market_order_amounts"):
        return False

    def exact_amounts(side: Any, amount: float, price: float, round_config: Any) -> tuple[Any, int, int]:
        buy_side = side == "BUY" or side == getattr(sdk.Side, "BUY", object())
        sell_side = side == "SELL" or side == getattr(sdk.Side, "SELL", object())
        if not buy_side and not sell_side:
            raise ValueError("market order side must be BUY or SELL")
        maker_amount, taker_amount = _exact_market_order_base_amounts(
            side="BUY" if buy_side else "SELL",
            amount=amount,
            price=price,
            price_places=int(round_config.price),
            size_places=int(round_config.size),
        )
        return (
            sdk.Side.BUY if buy_side else sdk.Side.SELL,
            maker_amount,
            taker_amount,
        )

    builder.get_market_order_amounts = exact_amounts
    return True


def _exact_market_order_base_amounts(
    *,
    side: str,
    amount: Any,
    price: Any,
    price_places: int,
    size_places: int,
) -> tuple[int, int]:
    price_scale = 10**price_places
    rounded_price = Decimal(str(price)).quantize(
        Decimal(1).scaleb(-price_places),
        rounding=ROUND_DOWN,
    )
    price_units = int(rounded_price * price_scale)
    if price_units <= 0:
        raise ValueError("market order price must be greater than zero")
    divisor = gcd(price_units, price_scale)
    price_numerator = price_units // divisor
    price_denominator = price_scale // divisor

    rounded_amount = Decimal(str(amount)).quantize(
        Decimal(1).scaleb(-size_places),
        rounding=ROUND_DOWN,
    )
    amount_units = int(rounded_amount * Decimal("1000000"))
    if side == "BUY":
        maker_factor, taker_factor = price_numerator, price_denominator
    elif side == "SELL":
        maker_factor, taker_factor = price_denominator, price_numerator
    else:
        raise ValueError("market order side must be BUY or SELL")

    # The CLOB validates decimal accuracy in addition to the exact implied
    # price. Maker amount follows RoundConfig.size; taker amount follows
    # RoundConfig.amount, which is price precision + 2 for supported ticks.
    maker_quantum = 10 ** max(0, 6 - size_places)
    taker_places = min(6, price_places + 2)
    taker_quantum = 10 ** max(0, 6 - taker_places)
    lot_step = lcm(
        maker_quantum // gcd(maker_factor, maker_quantum),
        taker_quantum // gcd(taker_factor, taker_quantum),
    )
    lots = (amount_units // maker_factor // lot_step) * lot_step
    if lots <= 0:
        raise ValueError("market order amount is too small for exact tick-aligned encoding")
    if side == "BUY":
        return lots * price_numerator, lots * price_denominator
    return lots * price_denominator, lots * price_numerator


def validate_exact_market_order_request(
    *,
    side: str,
    amount: Any,
    price: Any,
    tick_size: Any,
) -> tuple[int, int]:
    """Validate exact integer amounts before creating a live run."""

    normalized_side = str(side).upper()
    tick = Decimal(str(tick_size))
    selected_price = Decimal(str(price))
    if tick <= 0 or selected_price <= 0:
        raise ValueError("market order price and tick size must be positive")
    if selected_price % tick != 0:
        raise ClobV2AdapterError("market order price is not tick aligned")
    price_places = max(0, -tick.normalize().as_tuple().exponent)
    maker_amount, taker_amount = _exact_market_order_base_amounts(
        side=normalized_side,
        amount=amount,
        price=selected_price,
        price_places=price_places,
        size_places=2,
    )
    requested_units = int(Decimal(str(amount)) * Decimal("1000000"))
    if normalized_side == "BUY":
        if maker_amount < 1_000_000:
            raise ClobV2AdapterError(
                "market BUY cannot encode the approved amount at this price "
                "without falling below the CLOB 1 USDC minimum"
            )
        if maker_amount > requested_units:
            raise ClobV2AdapterError("market BUY exceeds the approved spend")
    elif normalized_side == "SELL":
        if maker_amount > requested_units:
            raise ClobV2AdapterError("market SELL exceeds the approved shares")
    else:
        raise ValueError("market order side must be BUY or SELL")
    return maker_amount, taker_amount


def _validate_signed_market_order_price(
    signed: Any,
    *,
    side: str,
    tick_size: str,
    worst_price: str,
    requested_amount: str,
) -> str:
    maker_amount = int(signed.makerAmount)
    taker_amount = int(signed.takerAmount)
    if maker_amount <= 0 or taker_amount <= 0:
        raise ClobV2AdapterError("signed market order contains a non-positive amount")
    implied = (
        Fraction(maker_amount, taker_amount)
        if side == "BUY"
        else Fraction(taker_amount, maker_amount)
    )
    tick = Fraction(Decimal(str(tick_size)))
    expected = Fraction(Decimal(str(worst_price)))
    if (implied / tick).denominator != 1:
        raise ClobV2AdapterError(
            f"signed market order implied price {float(implied):.12g} is not tick aligned"
        )
    if implied != expected:
        raise ClobV2AdapterError(
            f"signed market order implied price {float(implied):.12g} does not match "
            f"worst price {worst_price}"
        )
    if side == "BUY":
        requested_units = int(Decimal(str(requested_amount)) * Decimal("1000000"))
        if maker_amount < 1_000_000:
            raise ClobV2AdapterError(
                "signed market BUY amount is below the CLOB 1 USDC minimum after exact rounding"
            )
        if maker_amount > requested_units:
            raise ClobV2AdapterError("signed market BUY amount exceeds the approved spend")
    elif side == "SELL":
        requested_units = int(Decimal(str(requested_amount)) * Decimal("1000000"))
        if maker_amount > requested_units:
            raise ClobV2AdapterError("signed market SELL amount exceeds the approved shares")
    return format(Decimal(implied.numerator) / Decimal(implied.denominator), "f")


def _validate_signed_limit_order(
    signed: Any,
    *,
    side: str,
    price: Decimal,
    size: Decimal,
    tick_size: Decimal,
) -> tuple[str, bool]:
    maker_amount = int(signed.makerAmount)
    taker_amount = int(signed.takerAmount)
    if maker_amount <= 0 or taker_amount <= 0:
        raise ClobV2AdapterError("signed limit order contains a non-positive amount")
    implied = (
        Fraction(maker_amount, taker_amount)
        if side == "BUY"
        else Fraction(taker_amount, maker_amount)
    )
    expected = Fraction(price)
    tick = Fraction(tick_size)
    if (implied / tick).denominator != 1:
        raise ClobV2AdapterError("signed limit order implied price is not tick aligned")
    if implied != expected:
        raise ClobV2AdapterError(
            "signed limit order implied price does not match the approved price"
        )
    signed_size_units = taker_amount if side == "BUY" else maker_amount
    requested_size_units = int(size * Decimal("1000000"))
    if signed_size_units > requested_size_units:
        raise ClobV2AdapterError("signed limit order exceeds the approved share size")
    exact_size = signed_size_units == requested_size_units
    return (
        format(Decimal(implied.numerator) / Decimal(implied.denominator), "f"),
        exact_size,
    )


def _transient_read_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        return True
    return int(status_code) in {408, 425, 429, 500, 502, 503, 504}


def _sdk_order_side(sdk: Any, side: str) -> Any:
    injected = getattr(sdk, side, None)
    if injected is not None:
        return injected
    from py_clob_client_v2.order_builder.constants import BUY, SELL

    return BUY if side == "BUY" else SELL


def _signed_order_safe_payload(signed: Any) -> dict[str, Any]:
    if is_dataclass(signed):
        payload = asdict(signed)
    else:
        payload = dict(vars(signed))
    payload.pop("signature", None)
    return payload


def _server_seconds(value: Any) -> float:
    if isinstance(value, Mapping):
        for key in ("server_time", "serverTime", "timestamp", "time"):
            if key in value:
                return _server_seconds(value[key])
    numeric = float(value)
    return numeric / 1000 if numeric > 10_000_000_000 else numeric


def _engine_public_ok(value: Any) -> bool:
    if isinstance(value, Mapping):
        candidate = value.get("ok", value.get("status", value.get("message")))
        if candidate is None:
            return bool(value)
        value = candidate
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"OK", "TRUE", "ACTIVE", "HEALTHY"}


def _official_event_title(market: Mapping[str, Any]) -> str | None:
    events = market.get("events")
    if not isinstance(events, (list, tuple)):
        return None
    for event in events:
        if not isinstance(event, Mapping):
            continue
        for key in ("title", "question", "name"):
            value = str(event.get(key) or "").strip()
            if value:
                return value
    return None


def _official_market_category(market: Mapping[str, Any]) -> str | None:
    explicit = str(market.get("category") or "").strip()
    if explicit:
        return explicit
    tags = market.get("tags")
    if not isinstance(tags, (list, tuple)):
        return None
    for tag in tags:
        value = str(
            tag.get("label") if isinstance(tag, Mapping) else tag
        ).strip().lower()
        if value in {"crypto", "politics", "sports", "weather"}:
            return value
    return None


def _closed_only_enabled(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key in ("closed_only", "closedOnly", "enabled", "value"):
            if key in value:
                return _closed_only_enabled(value[key])
        return False
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "enabled", "closed_only"}


def _book_bbo(value: Any) -> tuple[Decimal | None, Decimal | None]:
    payload = value if isinstance(value, Mapping) else {}
    bids = [
        _optional_decimal(item.get("price"))
        for item in payload.get("bids") or ()
        if isinstance(item, Mapping)
    ]
    asks = [
        _optional_decimal(item.get("price"))
        for item in payload.get("asks") or ()
        if isinstance(item, Mapping)
    ]
    valid_bids = [item for item in bids if item is not None]
    valid_asks = [item for item in asks if item is not None]
    return (max(valid_bids) if valid_bids else None, min(valid_asks) if valid_asks else None)


def _optional_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_balance_allowance(value: Any) -> dict[str, Any]:
    payload = dict(value or {}) if isinstance(value, Mapping) else {}
    balance = payload.get("balance")
    allowances = payload.get("allowances") if isinstance(payload.get("allowances"), Mapping) else {}
    return {
        **payload,
        "balance_raw": balance,
        "balance": _base_units_to_units(balance),
        "allowances_raw": dict(allowances),
        "allowances": {str(key): _base_units_to_units(item) for key, item in allowances.items()},
    }


def _base_units_to_units(value: Any) -> str:
    text = str(value or "0").strip()
    try:
        numeric = Decimal(text)
    except Exception:
        return "0"
    if "." not in text and "e" not in text.lower():
        numeric /= Decimal("1000000")
    return format(numeric, "f")
