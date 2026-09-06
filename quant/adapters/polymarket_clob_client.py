"""Async CLOB REST adapter for book readiness probes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from quant.market.clob_book_probe import validate_book_payload


class PolymarketClobError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class PolymarketClobClient:
    base_url: str = "https://clob.polymarket.com"
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

    async def get_book(self, token_id: str) -> dict[str, Any]:
        response: httpx.Response | None = None
        for attempt in range(max(1, int(self.max_retries) + 1)):
            try:
                async with self._client() as client:
                    response = await client.get(self.base_url.rstrip("/") + "/book", params={"token_id": token_id})
                break
            except httpx.TimeoutException as exc:
                if attempt >= int(self.max_retries):
                    raise PolymarketClobError("TIMEOUT", _error_message(exc)) from exc
                await asyncio.sleep(float(self.backoff_seconds) * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                if attempt >= int(self.max_retries):
                    raise PolymarketClobError("HTTP_ERROR", _error_message(exc)) from exc
                await asyncio.sleep(float(self.backoff_seconds) * (attempt + 1))
        if response is None:  # pragma: no cover
            raise PolymarketClobError("HTTP_ERROR", "no response")
        if response.status_code == 404:
            raise PolymarketClobError("BOOK_NOT_FOUND", response.text)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "").strip()
            message = f"{_error_message(exc)} body={body[:500]}" if body else _error_message(exc)
            raise PolymarketClobError("HTTP_ERROR", message) from exc
        except Exception as exc:
            raise PolymarketClobError("HTTP_ERROR", _error_message(exc)) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise PolymarketClobError("INVALID_SCHEMA", "book response is not an object")
        ok, error = validate_book_payload(payload)
        if not ok:
            raise PolymarketClobError("INVALID_SCHEMA", error or "invalid book payload")
        return payload

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        payload = await self._get_json_object(
            "/markets/" + str(condition_id).strip(),
        )
        if not isinstance(payload.get("tokens"), list):
            raise PolymarketClobError("INVALID_SCHEMA", "market tokens are missing")
        return payload

    async def get_clob_market_info(self, condition_id: str) -> dict[str, Any]:
        """Return authoritative CLOB V2 market parameters."""

        payload = await self._get_json_object(
            "/clob-markets/" + str(condition_id).strip(),
        )
        if not isinstance(payload.get("t"), list):
            raise PolymarketClobError(
                "INVALID_SCHEMA",
                "CLOB V2 market tokens are missing",
            )
        return payload

    async def get_fee_rate(self, token_id: str) -> int:
        payload = await self._get_json_object(
            "/fee-rate",
            params={"token_id": str(token_id)},
        )
        try:
            return int(payload["base_fee"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PolymarketClobError("INVALID_SCHEMA", "base_fee is missing") from exc

    async def _get_json_object(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response: httpx.Response | None = None
        for attempt in range(max(1, int(self.max_retries) + 1)):
            try:
                async with self._client() as client:
                    response = await client.get(
                        self.base_url.rstrip("/") + path,
                        params=params,
                    )
                break
            except httpx.TimeoutException as exc:
                if attempt >= int(self.max_retries):
                    raise PolymarketClobError("TIMEOUT", _error_message(exc)) from exc
                await asyncio.sleep(float(self.backoff_seconds) * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                if attempt >= int(self.max_retries):
                    raise PolymarketClobError("HTTP_ERROR", _error_message(exc)) from exc
                await asyncio.sleep(float(self.backoff_seconds) * (attempt + 1))
        if response is None:  # pragma: no cover
            raise PolymarketClobError("HTTP_ERROR", "no response")
        try:
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "").strip()
            message = f"{_error_message(exc)} body={body[:500]}" if body else _error_message(exc)
            raise PolymarketClobError("HTTP_ERROR", message) from exc
        except Exception as exc:
            raise PolymarketClobError("INVALID_SCHEMA", _error_message(exc)) from exc
        if not isinstance(payload, dict):
            raise PolymarketClobError("INVALID_SCHEMA", "response is not an object")
        return payload

    async def get_books(self, token_ids: Iterable[str], *, batch_size: int = 500) -> dict[str, dict[str, Any]]:
        """Fetch order books in protocol-sized batches.

        The endpoint omits unknown token ids from an otherwise successful
        response, so callers can distinguish a missing token from a transport
        failure.
        """

        ids = list(dict.fromkeys(str(item).strip() for item in token_ids if str(item).strip()))
        result: dict[str, dict[str, Any]] = {}
        size = min(500, max(1, int(batch_size)))
        for index in range(0, len(ids), size):
            result.update(await self._get_books_batch(ids[index : index + size]))
        return result

    async def _get_books_batch(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        response: httpx.Response | None = None
        for attempt in range(max(1, int(self.max_retries) + 1)):
            try:
                async with self._client() as client:
                    response = await client.post(
                        self.base_url.rstrip("/") + "/books",
                        json=[{"token_id": token_id} for token_id in token_ids],
                    )
                break
            except httpx.TimeoutException as exc:
                if attempt >= int(self.max_retries):
                    raise PolymarketClobError("TIMEOUT", _error_message(exc)) from exc
                await asyncio.sleep(float(self.backoff_seconds) * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                if attempt >= int(self.max_retries):
                    raise PolymarketClobError("HTTP_ERROR", _error_message(exc)) from exc
                await asyncio.sleep(float(self.backoff_seconds) * (attempt + 1))
        if response is None:  # pragma: no cover
            raise PolymarketClobError("HTTP_ERROR", "no response")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "").strip()
            message = f"{_error_message(exc)} body={body[:500]}" if body else _error_message(exc)
            raise PolymarketClobError("HTTP_ERROR", message) from exc
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise PolymarketClobError("INVALID_SCHEMA", _error_message(exc)) from exc
        if not isinstance(payload, list):
            raise PolymarketClobError("INVALID_SCHEMA", "books response is not an array")
        books: dict[str, dict[str, Any]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or item.get("token_id") or "").strip()
            if asset_id:
                books[asset_id] = item
        return books


def _error_message(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__
