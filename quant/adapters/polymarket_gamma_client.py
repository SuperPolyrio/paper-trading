"""Async Gamma REST adapter for market discovery."""

from __future__ import annotations

import hashlib
import json
import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


class PolymarketGammaError(RuntimeError):
    pass


@dataclass(frozen=True)
class PolymarketGammaClient:
    base_url: str = "https://gamma-api.polymarket.com"
    timeout_seconds: float = 15.0
    proxy_url: str | None = None
    max_retries: int = 2
    backoff_seconds: float = 0.5

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": self.timeout_seconds}
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
            kwargs["trust_env"] = False
        return httpx.AsyncClient(**kwargs)

    async def fetch_events(
        self,
        *,
        active: bool | None = None,
        closed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str | None = None,
        ascending: bool | None = None,
        tag_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if order:
            params["order"] = order
        if ascending is not None:
            params["ascending"] = str(ascending).lower()
        if tag_id:
            params["tag_id"] = tag_id
        return await self._get_list("/events", params)

    async def fetch_markets(
        self,
        *,
        active: bool | None = None,
        closed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str | None = None,
        ascending: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch a bounded Gamma market page through the shared adapter."""

        params: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if order:
            params["order"] = order
        if ascending is not None:
            params["ascending"] = str(ascending).lower()
        return await self._get_list("/markets", params)

    async def fetch_top_active_markets(
        self,
        *,
        max_markets: int = 500,
        page_size: int = 100,
        order: str = "volume24hrClob,liquidityClob",
    ) -> list[dict[str, Any]]:
        """Fetch active order-book markets in descending activity order."""

        rows: list[dict[str, Any]] = []
        offset = 0
        bounded_page_size = max(1, min(500, int(page_size)))
        while len(rows) < max(1, int(max_markets)):
            limit = min(bounded_page_size, int(max_markets) - len(rows))
            page = await self.fetch_markets(
                active=True,
                closed=False,
                limit=limit,
                offset=offset,
                order=order,
                ascending=False,
            )
            rows.extend(
                row
                for row in page
                if row.get("enableOrderBook") is not False
                and row.get("acceptingOrders") is not False
            )
            if len(page) < limit:
                break
            offset += len(page)
        return rows[: max(1, int(max_markets))]

    async def fetch_all_active_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self.fetch_events(active=True, closed=False, limit=limit, offset=offset)
            rows.extend(page)
            if len(page) < int(limit):
                break
            offset += len(page)
        return rows

    async def fetch_event_by_slug(self, slug: str) -> dict[str, Any] | None:
        rows = await self._get_list("/events", {"slug": slug, "limit": 1})
        return rows[0] if rows else None

    async def fetch_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        rows = await self._get_list("/markets", {"slug": slug, "limit": 1})
        return rows[0] if rows else None

    async def _get_list(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(max(1, int(self.max_retries) + 1)):
            try:
                async with self._client() as client:
                    response = await client.get(self.base_url.rstrip("/") + path, params=params)
                    response.raise_for_status()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= int(self.max_retries):
                    raise PolymarketGammaError(str(exc)) from exc
                await asyncio.sleep(float(self.backoff_seconds) * (attempt + 1))
        else:  # pragma: no cover
            raise PolymarketGammaError(str(last_error))
        payload = response.json()
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("events") or payload.get("markets") or payload.get("data") or []
        else:
            rows = []
        normalized = [item for item in rows if isinstance(item, dict)]
        for row in normalized:
            row.setdefault("_raw_payload_hash", raw_payload_hash(row))
        return normalized


def raw_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
