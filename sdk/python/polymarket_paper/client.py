"""Dependency-free Python client for the versioned Paper API."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


class PaperApiClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PAPER_CLIENT_ERROR",
        status_code: int | None = None,
        request_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.details = dict(details or {})


class PaperApiClient:
    """Small synchronous client with automatic idempotency for mutations."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 15.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.api_key = str(api_key).strip()
        self.timeout = float(timeout)
        if not self.base_url or not self.api_key:
            raise ValueError("base_url and api_key are required")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        selected_query = {
            key: value for key, value in (query or {}).items() if value is not None
        }
        if selected_query:
            url = f"{url}?{urlencode(selected_query)}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "polymarket-paper-python/1",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            error = payload.get("error") if isinstance(payload, dict) else None
            error = error if isinstance(error, dict) else {}
            raise PaperApiClientError(
                str(error.get("message") or f"Paper API returned HTTP {exc.code}"),
                code=str(error.get("code") or "PAPER_HTTP_ERROR"),
                status_code=exc.code,
                request_id=str(error.get("request_id") or "") or None,
                details=error.get("details")
                if isinstance(error.get("details"), dict)
                else None,
            ) from exc
        except URLError as exc:
            raise PaperApiClientError(
                f"Paper API is unreachable: {exc.reason}",
                code="PAPER_NETWORK_ERROR",
            ) from exc
        if not isinstance(payload, dict) or "data" not in payload:
            raise PaperApiClientError(
                "Paper API returned an invalid response envelope",
                code="PAPER_PROTOCOL_ERROR",
            )
        return payload

    @staticmethod
    def _key(value: str | None) -> str:
        return str(value or uuid4())

    def list_accounts(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "GET", "/v1/paper/accounts", query={"limit": limit, "cursor": cursor}
        )["data"]

    def iter_accounts(self, *, page_size: int = 50) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page = self.list_accounts(limit=page_size, cursor=cursor)
            yield from page.get("items", [])
            cursor = page.get("next_cursor")
            if not cursor:
                return

    def get_account(self, account_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/paper/accounts/{account_id}")["data"]

    def create_account(
        self,
        name: str,
        initial_cash: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/accounts",
            body={"name": name, "initial_cash": str(initial_cash)},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def fork_account(
        self,
        account_id: str,
        name: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/paper/accounts/{account_id}/fork",
            body={"name": name},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def list_orders(
        self,
        account_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/v1/paper/orders",
            query={"account_id": account_id, "limit": limit, "cursor": cursor},
        )["data"]

    def get_order(self, order_id: int) -> dict[str, Any]:
        return self._request("GET", f"/v1/paper/orders/{int(order_id)}")["data"]

    def get_order_audit(self, order_id: int) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/paper/audit/{int(order_id)}"
        )["data"]

    def create_order(
        self,
        order: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/orders",
            body=dict(order),
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def cancel_order(
        self, order_id: int, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/v1/paper/orders/{int(order_id)}",
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def cancel_orders(
        self,
        order_ids: list[int],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/orders/cancel",
            body={"order_ids": [int(order_id) for order_id in order_ids]},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def cancel_market_orders(
        self,
        account_id: str,
        *,
        asset_id: str | None = None,
        condition_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/orders/cancel-market",
            body={
                "account_id": account_id,
                "asset_id": asset_id,
                "condition_id": condition_id,
            },
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def cancel_all_orders(
        self,
        account_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/orders/cancel-all",
            body={"account_id": account_id},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def replace_order(
        self,
        order_id: int,
        *,
        limit_price: str,
        size: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/paper/orders/{int(order_id)}/replace",
            body={"limit_price": str(limit_price), "size": str(size)},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def list_positions(
        self, account_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/paper/accounts/{account_id}/positions",
            query={"limit": limit, "cursor": cursor},
        )["data"]

    def list_fills(
        self,
        account_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/paper/accounts/{account_id}/fills",
            query={"limit": limit, "cursor": cursor},
        )["data"]

    def list_ledger(
        self,
        account_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/paper/accounts/{account_id}/ledger",
            query={"limit": limit, "cursor": cursor},
        )["data"]

    def list_journal(
        self,
        account_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/paper/accounts/{account_id}/journal",
            query={"limit": limit, "cursor": cursor},
        )["data"]

    def list_tca(
        self,
        account_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/paper/accounts/{account_id}/tca",
            query={"limit": limit, "cursor": cursor},
        )["data"]

    def get_performance(
        self,
        account_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/paper/accounts/{account_id}/performance",
            query={"limit": limit, "cursor": cursor},
        )["data"]

    def list_replay_sessions(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "GET", "/v1/paper/replays", query={"limit": limit, "cursor": cursor}
        )["data"]

    def create_replay_session(
        self,
        replay: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/replays",
            body=dict(replay),
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def get_replay_session(self, replay_session_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/paper/replays/{replay_session_id}"
        )["data"]

    def pause_replay_session(
        self, replay_session_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/paper/replays/{replay_session_id}/pause",
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def resume_replay_session(
        self,
        replay_session_id: str,
        *,
        max_events: int = 1000,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/paper/replays/{replay_session_id}/resume",
            body={"max_events": int(max_events)},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def fork_replay_session(
        self,
        replay_session_id: str,
        fork: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/paper/replays/{replay_session_id}/fork",
            body=dict(fork),
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def list_replay_events(
        self,
        replay_session_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/paper/replays/{replay_session_id}/events",
            query={"limit": limit, "cursor": cursor},
        )["data"]

    def get_replay_report(self, replay_session_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/paper/replays/{replay_session_id}/report"
        )["data"]

    def list_scenario_runs(
        self, account_id: str, *, limit: int = 100
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/v1/paper/scenarios",
            query={"account_id": account_id, "limit": limit},
        )["data"]

    def create_scenario_run(
        self,
        scenario: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/scenarios",
            body=dict(scenario),
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def get_scenario_run(self, scenario_run_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/paper/scenarios/{scenario_run_id}"
        )["data"]

    def list_conditional_orders(
        self, account_id: str, *, limit: int = 100
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/v1/paper/conditional-orders",
            query={"account_id": account_id, "limit": limit},
        )["data"]

    def create_conditional_order(
        self,
        order: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/conditional-orders",
            body=dict(order),
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def get_conditional_order(self, conditional_order_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/paper/conditional-orders/{conditional_order_id}"
        )["data"]

    def cancel_conditional_order(
        self,
        conditional_order_id: str,
        *,
        reason: str = "sdk_cancel",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/v1/paper/conditional-orders/{conditional_order_id}",
            body={"reason": reason},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def get_admin_dashboard(self) -> dict[str, Any]:
        return self._request("GET", "/v1/paper/admin/dashboard")["data"]

    def set_tenant_frozen(
        self,
        frozen: bool,
        reason: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        action = "freeze" if frozen else "unfreeze"
        return self._request(
            "POST",
            f"/v1/paper/admin/tenant/{action}",
            body={"reason": reason},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def set_account_frozen(
        self,
        account_id: str,
        frozen: bool,
        reason: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        action = "freeze" if frozen else "unfreeze"
        return self._request(
            "POST",
            f"/v1/paper/admin/accounts/{account_id}/{action}",
            body={"reason": reason},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def kill_account(
        self,
        account_id: str,
        reason: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/paper/admin/accounts/{account_id}/kill",
            body={"reason": reason},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def reconcile_account(
        self,
        account_id: str,
        *,
        mode: str = "DRY_RUN",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/paper/admin/accounts/{account_id}/reconcile",
            body={"mode": mode},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def list_admin_jobs(self, *, limit: int = 100) -> dict[str, Any]:
        return self._request(
            "GET", "/v1/paper/admin/jobs", query={"limit": limit}
        )["data"]

    def list_dlq_events(self, *, limit: int = 100) -> dict[str, Any]:
        return self._request(
            "GET", "/v1/paper/admin/dlq", query={"limit": limit}
        )["data"]

    def replay_dlq_event(
        self, dlq_event_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/paper/admin/dlq/{dlq_event_id}/replay",
            body={},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def create_notice(
        self, notice: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/admin/notices",
            body=notice,
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def create_incident(
        self, incident: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/admin/incidents",
            body=incident,
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def list_retention_policies(self) -> dict[str, Any]:
        return self._request("GET", "/v1/paper/admin/retention")["data"]

    def upsert_retention_policy(
        self,
        resource_type: str,
        retention_days: int,
        *,
        legal_hold: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/paper/admin/retention/{resource_type}",
            body={"retention_days": retention_days, "legal_hold": legal_hold},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def create_evidence_bundle(
        self,
        *,
        account_id: str | None = None,
        incident_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/paper/admin/evidence-bundles",
            body={"account_id": account_id, "incident_id": incident_id},
            idempotency_key=self._key(idempotency_key),
        )["data"]

    def download_evidence_bundle(self, bundle_id: str) -> bytes:
        request = Request(
            f"{self.base_url}/v1/paper/admin/evidence-bundles/{bundle_id}/download",
            headers={
                "Accept": "application/zip",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "polymarket-paper-python/1",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            raise PaperApiClientError(
                f"Paper API returned HTTP {exc.code}",
                code="PAPER_HTTP_ERROR",
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise PaperApiClientError(
                f"Paper API is unreachable: {exc.reason}",
                code="PAPER_NETWORK_ERROR",
            ) from exc

    def export_account(
        self,
        account_id: str,
        resource: str,
        *,
        output_format: str = "csv",
    ) -> bytes:
        selected_format = str(output_format).lower()
        if selected_format not in {"csv", "jsonl", "parquet"}:
            raise ValueError("output_format must be csv, jsonl, or parquet")
        query = urlencode({"resource": resource, "format": selected_format})
        request = Request(
            f"{self.base_url}/v1/paper/accounts/{account_id}/export?{query}",
            headers={
                "Accept": "text/csv, application/x-ndjson, application/vnd.apache.parquet",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "polymarket-paper-python/1",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            error = payload.get("error") if isinstance(payload, dict) else None
            error = error if isinstance(error, dict) else {}
            raise PaperApiClientError(
                str(error.get("message") or f"Paper API returned HTTP {exc.code}"),
                code=str(error.get("code") or "PAPER_HTTP_ERROR"),
                status_code=exc.code,
                request_id=str(error.get("request_id") or "") or None,
            ) from exc
        except URLError as exc:
            raise PaperApiClientError(
                f"Paper API is unreachable: {exc.reason}",
                code="PAPER_NETWORK_ERROR",
            ) from exc
