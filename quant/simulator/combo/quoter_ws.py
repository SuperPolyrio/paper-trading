"""Authenticated Quoter WebSocket adapter and restart-safe event parser."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import ComboQuote, ComboRequest, payload_hash, unix_ms_to_datetime

QUOTER_WS_URL = "wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq"


class QuoterAuthProvider(Protocol):
    def auth_payload(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class QuoterEvent:
    event_id: str
    event_type: str
    rfq_id: str | None
    quote_id: str | None
    execution_status: str | None
    server_ts: datetime | None
    received_at: datetime
    payload_hash: str
    payload: Mapping[str, Any]


def parse_quoter_event(
    raw: str | bytes | Mapping[str, Any],
    *,
    received_at: datetime | None = None,
) -> QuoterEvent:
    if isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        decoded = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        parsed = json.loads(decoded)
        if not isinstance(parsed, Mapping):
            raise TypeError("Quoter WebSocket event must be a JSON object")
        payload = dict(parsed)
    event_type = str(
        payload.get("type") or payload.get("event") or payload.get("event_type") or ""
    ).strip()
    if not event_type:
        raise ValueError("Quoter WebSocket event type is required")
    body = payload.get("payload") if isinstance(payload.get("payload"), Mapping) else payload
    rfq_id = str(body.get("rfq_id") or body.get("rfqId") or "") or None
    quote_id = str(body.get("quote_id") or body.get("quoteId") or "") or None
    status = str(body.get("status") or "").upper() or None
    server_ts = unix_ms_to_datetime(
        body.get("timestamp")
        or body.get("event_ts")
        or body.get("submission_deadline")
        or None
    )
    received = received_at or datetime.now(timezone.utc)
    digest = payload_hash(payload)
    event_id = ":".join(
        item
        for item in (
            event_type,
            rfq_id or "-",
            quote_id or "-",
            status or "-",
            digest,
        )
    )
    return QuoterEvent(
        event_id=event_id,
        event_type=event_type,
        rfq_id=rfq_id,
        quote_id=quote_id,
        execution_status=status,
        server_ts=server_ts,
        received_at=received,
        payload_hash=digest,
        payload=payload,
    )


class QuoterEventCheckpoint(Protocol):
    def seen(self, event_id: str) -> bool: ...

    def record(self, event: QuoterEvent) -> bool: ...


class InMemoryQuoterCheckpoint:
    def __init__(self) -> None:
        self.events: dict[str, QuoterEvent] = {}

    def seen(self, event_id: str) -> bool:
        return event_id in self.events

    def record(self, event: QuoterEvent) -> bool:
        if event.event_id in self.events:
            return False
        self.events[event.event_id] = event
        return True


class OfficialQuoterCommandSession:
    """Deadline-validating command facade around one authenticated WS session."""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket

    async def submit_quote(
        self,
        request: ComboRequest,
        quote: ComboQuote,
        *,
        now: datetime,
    ) -> None:
        deadline = request.submission_deadline
        if deadline is None:
            raise ValueError("Quoter RFQ request is missing submission_deadline")
        if now > deadline:
            raise ValueError("quote submission_deadline elapsed")
        if quote.rfq_id != request.rfq_id:
            raise ValueError("quote belongs to a different RFQ")
        await self.websocket.send(
            json.dumps(
                {
                    "type": "RFQ_QUOTE",
                    "rfq_id": request.rfq_id,
                    "price_e6": str(quote.price_e6),
                    "size_e6": str(quote.size_e6),
                    "signed_order": dict(quote.signed_order),
                }
            )
        )

    async def cancel_quote(
        self,
        *,
        rfq_id: str,
        quote_id: str,
        signer_address: str,
        maker_address: str,
    ) -> None:
        await self.websocket.send(
            json.dumps(
                {
                    "type": "RFQ_QUOTE_CANCEL",
                    "rfq_id": rfq_id,
                    "quote_id": quote_id,
                    "signer_address": signer_address,
                    "maker_address": maker_address,
                }
            )
        )

    async def respond_last_look(
        self,
        *,
        rfq_id: str,
        quote_id: str,
        confirm_by: datetime,
        confirm: bool,
        now: datetime,
    ) -> None:
        if now > confirm_by:
            raise ValueError("last-look confirm_by elapsed")
        await self.websocket.send(
            json.dumps(
                {
                    "type": "RFQ_CONFIRMATION_RESPONSE",
                    "rfq_id": rfq_id,
                    "quote_id": quote_id,
                    "decision": "CONFIRM" if confirm else "DECLINE",
                }
            )
        )


class OfficialQuoterWsAdapter:
    """Reconnects the official Quoter channel and yields deduplicated events."""

    def __init__(
        self,
        *,
        auth_provider: QuoterAuthProvider,
        checkpoint: QuoterEventCheckpoint,
        url: str = QUOTER_WS_URL,
        proxy_url: str | None = None,
        connect_factory: Callable[..., Awaitable[Any]] | None = None,
        reconnect_min_seconds: float = 0.5,
        reconnect_max_seconds: float = 15.0,
    ) -> None:
        self.auth_provider = auth_provider
        self.checkpoint = checkpoint
        self.url = url
        self.proxy_url = proxy_url
        self.connect_factory = connect_factory
        self.reconnect_min_seconds = reconnect_min_seconds
        self.reconnect_max_seconds = reconnect_max_seconds

    async def events(self, *, stop: asyncio.Event | None = None) -> AsyncIterator[QuoterEvent]:
        backoff = self.reconnect_min_seconds
        while stop is None or not stop.is_set():
            try:
                async with self._connection() as websocket:
                    await asyncio.wait_for(
                        websocket.send(json.dumps(dict(self.auth_provider.auth_payload()))),
                        timeout=30.0,
                    )
                    backoff = self.reconnect_min_seconds
                    async for raw in websocket:
                        event = parse_quoter_event(raw)
                        if self.checkpoint.record(event):
                            yield event
                        if stop is not None and stop.is_set():
                            return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - every transport failure reconnects
                if stop is not None and stop.is_set():
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max_seconds)

    def _connect(self) -> Any:
        if self.connect_factory is not None:
            return self.connect_factory(self.url, proxy=self.proxy_url)
        import websockets

        kwargs: dict[str, Any] = {
            "ping_interval": 30,
            "ping_timeout": 120,
            "close_timeout": 5,
            "max_queue": 4096,
        }
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        return websockets.connect(self.url, **kwargs)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        connection = self._connect()
        if inspect.isawaitable(connection):
            connection = await connection
        if hasattr(connection, "__aenter__"):
            async with connection as websocket:
                yield websocket
            return
        try:
            yield connection
        finally:
            close = getattr(connection, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
