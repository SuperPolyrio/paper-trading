"""Read-only adapter for recent public Polymarket taker trades."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class PolymarketDataTradesError(RuntimeError):
    pass


class PolymarketDataTradesClient:
    """The HTTP boundary used for current public-trade candidate evidence."""

    def __init__(
        self,
        *,
        base_url: str = "https://data-api.polymarket.com",
        session: requests.Session | None = None,
        proxy_url: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
            retry = Retry(
                total=3,
                connect=3,
                read=3,
                status=3,
                backoff_factor=0.25,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(
                max_retries=retry,
                pool_connections=2,
                pool_maxsize=2,
            )
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        self.proxy_url = str(proxy_url or "").strip() or None
        self.timeout_seconds = max(0.1, float(timeout_seconds))

    @property
    def _proxies(self) -> dict[str, str] | None:
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def fetch_recent_taker_trades(
        self,
        *,
        condition_id: str | None = None,
        limit: int = 500,
    ) -> tuple[dict[str, Any], ...]:
        market = str(condition_id or "").strip()
        bounded_limit = max(1, min(10_000, int(limit)))
        params: dict[str, Any] = {
            "limit": bounded_limit,
            "takerOnly": "true",
        }
        if market:
            params["market"] = market
        try:
            response = self.session.get(
                f"{self.base_url}/trades",
                params=params,
                proxies=self._proxies,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PolymarketDataTradesError(
                f"recent public trades unavailable: {type(exc).__name__}:{exc}"
            ) from exc
        if not isinstance(payload, list) or any(
            not isinstance(row, Mapping) for row in payload
        ):
            raise PolymarketDataTradesError("recent public trades schema is not a list")
        return tuple(dict(row) for row in payload)

    def summarize_compatible_maker_activity(
        self,
        *,
        condition_id: str,
        asset_id: str,
        maker_side: str,
        limit_price: Decimal,
        lookback_seconds: int = 900,
        limit: int = 500,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        side = str(maker_side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("maker_side must be BUY or SELL")
        cutoff = now - timedelta(seconds=max(1, int(lookback_seconds)))
        expected_taker_side = "SELL" if side == "BUY" else "BUY"
        rows = self.fetch_recent_taker_trades(
            condition_id=condition_id,
            limit=limit,
        )
        compatible: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("asset") or "") != str(asset_id):
                continue
            if str(row.get("side") or "").upper() != expected_taker_side:
                continue
            try:
                event_at = datetime.fromtimestamp(
                    int(row.get("timestamp") or 0),
                    tz=timezone.utc,
                )
                price = Decimal(str(row.get("price")))
                size = Decimal(str(row.get("size")))
            except (InvalidOperation, TypeError, ValueError, OSError):
                continue
            if event_at < cutoff or event_at > now + timedelta(seconds=5):
                continue
            price_compatible = (
                price <= limit_price if side == "BUY" else price >= limit_price
            )
            if not price_compatible or size <= 0:
                continue
            compatible.append(
                {
                    "asset_id": str(asset_id),
                    "taker_side": expected_taker_side,
                    "price": format(price, "f"),
                    "size": format(size, "f"),
                    "event_at": event_at.isoformat(),
                    "transaction_hash": str(row.get("transactionHash") or ""),
                }
            )
        sizes = [Decimal(row["size"]) for row in compatible]
        latest = max(
            (datetime.fromisoformat(row["event_at"]) for row in compatible),
            default=None,
        )
        return {
            "schema_version": "maker_recent_public_trade_activity_v1",
            "status": "READY",
            "source": "official_data_api_taker_trades",
            "source_ready": True,
            "condition_id": str(condition_id),
            "asset_id": str(asset_id),
            "maker_side": side,
            "required_taker_side": expected_taker_side,
            "limit_price": format(limit_price, "f"),
            "window_start": cutoff.isoformat(),
            "window_end": now.isoformat(),
            "lookback_seconds": max(1, int(lookback_seconds)),
            "compatible_trade_count": len(compatible),
            "compatible_trade_volume": format(sum(sizes, Decimal(0)), "f"),
            "median_trade_size": (
                format(statistics.median(sizes), "f") if sizes else "0"
            ),
            "last_compatible_trade_at": latest.isoformat() if latest else None,
            "last_compatible_trade_age_seconds": (
                format(Decimal(str(max(0.0, (now - latest).total_seconds()))), "f")
                if latest
                else None
            ),
            "rows": compatible[:100],
            "payload_sha256": _payload_hash(compatible),
            "prediction_truth_claimed": False,
            "own_order_execution_truth_claimed": False,
        }


def _payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
