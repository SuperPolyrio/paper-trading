"""Network adapter for public Polymarket account-truth endpoints."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


@dataclass(frozen=True)
class OfficialFetchResult:
    endpoint: str
    request_parameters: Mapping[str, Any]
    observed_at: datetime
    http_status: int
    content_type: str
    content: bytes
    payload_hash: str
    response_headers: Mapping[str, str]
    fetch_status: str = "PASS"
    error_code: str | None = None

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class OfficialAccountFetchError(RuntimeError):
    def __init__(self, result: OfficialFetchResult) -> None:
        super().__init__(
            f"official account request failed: {result.endpoint} {result.error_code}"
        )
        self.result = result


class OfficialAccountClient:
    """The only HTTP boundary used by account-truth services."""

    def __init__(
        self,
        *,
        base_url: str = "https://data-api.polymarket.com",
        session: requests.Session | None = None,
        proxy_url: str | None = None,
        timeout_seconds: float = 15.0,
        min_request_interval_seconds: float = 0.0,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
            retry = Retry(
                total=4,
                connect=4,
                read=4,
                status=4,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        self.proxy_url = str(proxy_url or "").strip() or None
        self.timeout_seconds = float(timeout_seconds)
        self.min_request_interval_seconds = max(
            0.0, float(min_request_interval_seconds)
        )
        self._last_request_monotonic = 0.0

    @property
    def _proxies(self) -> dict[str, str] | None:
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def fetch_positions(
        self,
        *,
        account_address: str,
        include_archived: bool = True,
        size_threshold: str = "0",
        page_size: int = 500,
        max_offset: int = 10_000,
    ) -> tuple[OfficialFetchResult, tuple[Mapping[str, Any], ...]]:
        account = _address(account_address)
        if not 1 <= int(page_size) <= 500:
            raise ValueError("positions page size must be between 1 and 500")
        rows: list[Mapping[str, Any]] = []
        page_results: list[OfficialFetchResult] = []
        offset = 0
        while True:
            params = {
                "user": account,
                "sizeThreshold": str(size_threshold),
                "includeArchived": str(bool(include_archived)).lower(),
                "limit": int(page_size),
                "offset": offset,
                "sortBy": "TOKENS",
                "sortDirection": "DESC",
            }
            result = self._get("/positions", params=params)
            payload = result.json()
            if not isinstance(payload, list) or any(
                not isinstance(row, Mapping) for row in payload
            ):
                raise TypeError("positions endpoint returned a non-list payload")
            page_results.append(result)
            rows.extend(payload)
            if len(payload) < page_size:
                break
            offset += page_size
            if offset > max_offset:
                raise RuntimeError(
                    "positions pagination exceeded official offset limit"
                )
        unique: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            asset = str(row.get("asset") or "")
            if not asset:
                raise ValueError("positions row is missing asset")
            if asset in unique and _stable_json(unique[asset]) != _stable_json(row):
                raise RuntimeError(
                    f"positions pagination returned conflicting asset: {asset}"
                )
            unique[asset] = row
        return _combine_pages("/positions", page_results), tuple(
            unique[key] for key in sorted(unique)
        )

    def fetch_closed_positions(
        self,
        *,
        account_address: str,
        page_size: int = 50,
        max_offset: int = 100_000,
    ) -> tuple[OfficialFetchResult, tuple[Mapping[str, Any], ...]]:
        account = _address(account_address)
        if not 1 <= int(page_size) <= 50:
            raise ValueError("closed positions page size must be between 1 and 50")
        rows: list[Mapping[str, Any]] = []
        page_results: list[OfficialFetchResult] = []
        offset = 0
        while True:
            params = {
                "user": account,
                "limit": int(page_size),
                "offset": offset,
                "sortBy": "TIMESTAMP",
                "sortDirection": "ASC",
            }
            result = self._get("/closed-positions", params=params)
            payload = result.json()
            if not isinstance(payload, list) or any(
                not isinstance(row, Mapping) for row in payload
            ):
                raise TypeError("closed positions endpoint returned a non-list payload")
            page_results.append(result)
            rows.extend(payload)
            if len(payload) < page_size:
                break
            offset += page_size
            if offset > max_offset:
                raise RuntimeError(
                    "closed positions pagination exceeded official offset limit"
                )
        unique: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            key = "|".join(
                (
                    str(row.get("asset") or ""),
                    str(row.get("conditionId") or ""),
                    str(row.get("timestamp") or ""),
                )
            )
            if not key.strip("|"):
                raise ValueError("closed position row has no stable identity")
            if key in unique and _stable_json(unique[key]) != _stable_json(row):
                raise RuntimeError(f"closed positions returned conflicting row: {key}")
            unique[key] = row
        return _combine_pages("/closed-positions", page_results), tuple(
            unique[key] for key in sorted(unique)
        )

    def fetch_accounting_snapshot(self, *, account_address: str) -> OfficialFetchResult:
        return self._get(
            "/v1/accounting/snapshot",
            params={"user": _address(account_address)},
        )

    def _get(self, endpoint: str, *, params: Mapping[str, Any]) -> OfficialFetchResult:
        elapsed = time.monotonic() - self._last_request_monotonic
        if elapsed < self.min_request_interval_seconds:
            time.sleep(self.min_request_interval_seconds - elapsed)
        try:
            response = self.session.get(
                f"{self.base_url}{endpoint}",
                params=dict(params),
                proxies=self._proxies,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            self._last_request_monotonic = time.monotonic()
            observed_at = datetime.now(timezone.utc)
            error_code = f"NETWORK_{type(exc).__name__.upper()}"
            content = _stable_json(
                {"endpoint": endpoint, "error_code": error_code}
            ).encode("utf-8")
            raise OfficialAccountFetchError(
                OfficialFetchResult(
                    endpoint=endpoint,
                    request_parameters=dict(params),
                    observed_at=observed_at,
                    http_status=0,
                    content_type="application/json",
                    content=content,
                    payload_hash=hashlib.sha256(content).hexdigest(),
                    response_headers={},
                    fetch_status="FAIL",
                    error_code=error_code,
                )
            ) from exc
        self._last_request_monotonic = time.monotonic()
        observed_at = datetime.now(timezone.utc)
        content = bytes(response.content)
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            status = int(response.status_code)
            raise OfficialAccountFetchError(
                OfficialFetchResult(
                    endpoint=endpoint,
                    request_parameters=dict(params),
                    observed_at=observed_at,
                    http_status=status,
                    content_type=str(response.headers.get("content-type") or ""),
                    content=content,
                    payload_hash=hashlib.sha256(content).hexdigest(),
                    response_headers=_selected_headers(response.headers),
                    fetch_status="FAIL",
                    error_code=f"HTTP_{status}",
                )
            ) from exc
        return OfficialFetchResult(
            endpoint=endpoint,
            request_parameters=dict(params),
            observed_at=observed_at,
            http_status=int(response.status_code),
            content_type=str(response.headers.get("content-type") or ""),
            content=content,
            payload_hash=hashlib.sha256(content).hexdigest(),
            response_headers=_selected_headers(response.headers),
        )


def _combine_pages(
    endpoint: str, pages: list[OfficialFetchResult]
) -> OfficialFetchResult:
    if not pages:
        raise ValueError(f"official endpoint returned no page result: {endpoint}")
    decoded: list[Any] = []
    for page in pages:
        payload = page.json()
        if isinstance(payload, list):
            decoded.extend(payload)
        else:
            decoded.append(payload)
    content = _stable_json(decoded).encode("utf-8")
    return OfficialFetchResult(
        endpoint=endpoint,
        request_parameters={
            "pages": len(pages),
            "page_requests": [dict(page.request_parameters) for page in pages],
        },
        observed_at=max(page.observed_at for page in pages),
        http_status=200,
        content_type="application/json",
        content=content,
        payload_hash=hashlib.sha256(content).hexdigest(),
        response_headers={"page-count": str(len(pages))},
    )


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _selected_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() in {"content-type", "content-length", "etag", "date"}
    }


def _address(value: str) -> str:
    normalized = str(value).strip().lower()
    if not _ADDRESS_RE.fullmatch(normalized):
        raise ValueError("account truth user must be a 20-byte EVM address")
    return normalized
