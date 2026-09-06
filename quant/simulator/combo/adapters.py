"""Official REST/SDK adapter boundaries for Combo markets and Builder RFQs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import (
    ComboMarket,
    ComboQuote,
    ComboRequest,
    OfficialRfqSnapshot,
    payload_hash,
)


class RequestHeaderProvider(Protocol):
    """Creates official auth headers for the exact request being sent."""

    def headers(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
    ) -> Mapping[str, str]: ...


class ComboSdkClient(Protocol):
    """Narrow official SDK surface used for Combo position operations."""

    def get_combo_positions(self, **kwargs: Any) -> Any: ...

    def plan_collateral_return(self) -> Any: ...

    def execute_collateral_return_plan(self, *, plan: Any) -> Any: ...


@dataclass(frozen=True)
class ComboMarketPage:
    markets: tuple[ComboMarket, ...]
    next_cursor: str | None
    raw_payload_hash: str


class OfficialComboRestAdapter:
    """The only HTTP boundary for public Combo markets and Builder RFQ calls."""

    def __init__(
        self,
        *,
        base_url: str = "https://combos-rfq-api.polymarket.com",
        session: requests.Session | None = None,
        proxy_url: str | None = None,
        timeout_seconds: float = 15.0,
        account_headers: RequestHeaderProvider | None = None,
        builder_headers: RequestHeaderProvider | None = None,
        sdk_client: ComboSdkClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
            retry = Retry(
                total=3,
                connect=3,
                read=3,
                status=3,
                backoff_factor=0.4,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        self.proxy_url = proxy_url
        self.timeout_seconds = timeout_seconds
        self.account_headers = account_headers
        self.builder_headers = builder_headers
        self.sdk_client = sdk_client

    @property
    def _proxies(self) -> dict[str, str] | None:
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def combo_market_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        exclude_condition_ids: Sequence[str] = (),
    ) -> ComboMarketPage:
        if not 1 <= limit <= 100:
            raise ValueError("Combo market page limit must be between 1 and 100")
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if exclude_condition_ids:
            params["exclude"] = ",".join(str(item) for item in exclude_condition_ids)
        response = self.session.get(
            f"{self.base_url}/v1/rfq/combo-markets",
            params=params,
            proxies=self._proxies,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("markets"), list
        ):
            raise TypeError("Combo market endpoint returned an invalid page")
        return ComboMarketPage(
            markets=tuple(ComboMarket.from_api(row) for row in payload["markets"]),
            next_cursor=(
                str(payload["next_cursor"])
                if payload.get("next_cursor") is not None
                else None
            ),
            raw_payload_hash=payload_hash(payload),
        )

    def all_combo_markets(self, *, max_pages: int = 10_000) -> tuple[ComboMarket, ...]:
        rows: dict[str, ComboMarket] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            page = self.combo_market_page(cursor=cursor)
            for market in page.markets:
                current = rows.get(market.market_id)
                if current is not None and current != market:
                    raise ValueError("Combo market changed within one pagination walk")
                rows[market.market_id] = market
            if page.next_cursor is None:
                return tuple(rows[key] for key in sorted(rows))
            if page.next_cursor in seen_cursors:
                raise RuntimeError("Combo market pagination cursor loop detected")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        raise RuntimeError("Combo market pagination exceeded max_pages")

    def create_builder_rfq(self, request: ComboRequest) -> OfficialRfqSnapshot:
        path = "/v1/builder/rfq/requests"
        body = request.as_builder_payload()
        return self._builder_request("POST", path, body=body, require_builder=True)

    def accept_builder_rfq(
        self,
        *,
        rfq_id: str,
        quote_id: str,
        signed_order: Mapping[str, Any],
    ) -> OfficialRfqSnapshot:
        path = f"/v1/builder/rfq/requests/{rfq_id}/accept"
        body = {"quote_id": quote_id, "signed_order": dict(signed_order)}
        return self._builder_request("POST", path, body=body, require_builder=True)

    def builder_rfq_status(self, *, rfq_id: str) -> OfficialRfqSnapshot:
        path = f"/v1/builder/rfq/requests/{rfq_id}"
        return self._builder_request("GET", path, body=None, require_builder=False)

    def submit_maker_quote(
        self,
        *,
        quote: ComboQuote,
        signer_address: str,
        maker_address: str,
        signature_type: int,
    ) -> OfficialRfqSnapshot:
        path = "/v1/maker/quotes"
        body = {
            "quote_id": quote.quote_id,
            "rfq_id": quote.rfq_id,
            "signer_address": signer_address,
            "maker_address": maker_address,
            "signature_type": int(signature_type),
            "price_e6": str(quote.price_e6),
            "size_e6": str(quote.size_e6),
            "signed_order": dict(quote.signed_order),
        }
        return self._account_request("POST", path, body=body)

    def cancel_maker_quote(
        self,
        *,
        rfq_id: str,
        quote_id: str,
        signer_address: str,
        maker_address: str,
        signature_type: int,
    ) -> OfficialRfqSnapshot:
        path = "/v1/maker/quotes/cancel"
        body = {
            "rfq_id": rfq_id,
            "quote_id": quote_id,
            "signer_address": signer_address,
            "maker_address": maker_address,
            "signature_type": int(signature_type),
        }
        # The returned acknowledgement is not proof that selection did not race it.
        return self._account_request("POST", path, body=body)

    def confirm_maker_last_look(
        self,
        *,
        rfq_id: str,
        quote_id: str,
        signer_address: str,
        maker_address: str,
        signature_type: int,
        confirm: bool,
    ) -> OfficialRfqSnapshot:
        path = "/v1/maker/confirmations"
        body = {
            "rfq_id": rfq_id,
            "quote_id": quote_id,
            "signer_address": signer_address,
            "maker_address": maker_address,
            "signature_type": int(signature_type),
            "decision": "CONFIRM" if confirm else "DECLINE",
        }
        return self._account_request("POST", path, body=body)

    def combo_positions(self, **filters: Any) -> tuple[Mapping[str, Any], ...]:
        if self.sdk_client is None:
            raise RuntimeError("official Combo SDK client is not configured")
        payload = self.sdk_client.get_combo_positions(**filters)
        if isinstance(payload, Mapping):
            payload = payload.get("positions") or payload.get("data") or ()
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            raise TypeError("Combo SDK returned invalid position data")
        return tuple(dict(item) for item in payload)

    def collateral_return_plan(self) -> Mapping[str, Any]:
        if self.sdk_client is None:
            raise RuntimeError("official Combo SDK client is not configured")
        payload = self.sdk_client.plan_collateral_return()
        if not isinstance(payload, Mapping):
            raise TypeError("Combo SDK returned an invalid collateral return plan")
        return dict(payload)

    def execute_collateral_return_plan(
        self, *, plan: Mapping[str, Any], execute: bool = False
    ) -> Any:
        if not execute:
            return {
                "status": "DRY_RUN",
                "plan_hash": plan.get("planHash") or plan.get("plan_hash"),
                "payload_hash": payload_hash(plan),
            }
        if self.sdk_client is None:
            raise RuntimeError("official Combo SDK client is not configured")
        return self.sdk_client.execute_collateral_return_plan(plan=dict(plan))

    def _builder_request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None,
        require_builder: bool,
    ) -> OfficialRfqSnapshot:
        if self.account_headers is None:
            raise RuntimeError("official account authentication is not configured")
        headers = dict(
            self.account_headers.headers(method=method, path=path, body=body)
        )
        if require_builder:
            if self.builder_headers is None:
                raise RuntimeError("approved Builder authentication is not configured")
            headers.update(
                self.builder_headers.headers(method=method, path=path, body=body)
            )
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            json=dict(body) if body is not None else None,
            headers=headers,
            proxies=self._proxies,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("Builder RFQ endpoint returned a non-object payload")
        # HTTP 200 is transport success only; the body is the business truth.
        return OfficialRfqSnapshot.from_api(payload)

    def _account_request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any],
    ) -> OfficialRfqSnapshot:
        if self.account_headers is None:
            raise RuntimeError("official account authentication is not configured")
        headers = dict(
            self.account_headers.headers(method=method, path=path, body=body)
        )
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            json=dict(body),
            headers=headers,
            proxies=self._proxies,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("maker RFQ endpoint returned a non-object payload")
        return OfficialRfqSnapshot.from_api(payload)
