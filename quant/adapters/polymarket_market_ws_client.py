"""Async Polymarket market WebSocket adapter."""

from __future__ import annotations

import json
import asyncio
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import socket
import struct
import termios
import time
from typing import Any, AsyncIterator

import websockets

try:
    import orjson as _orjson
except ImportError:  # Keep the adapter portable in minimal local/test environments.
    _orjson = None


@dataclass
class PolymarketMarketWsClient:
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ping_interval: float | None = 20.0
    ping_timeout: float | None = 20.0
    application_ping_interval: float | None = 10.0
    application_ping_timeout: float | None = 30.0
    reconnect_seconds: float = 5.0
    proxy_url: str | None = None
    max_queue: int | None = 256
    open_timeout: float | None = 15.0
    close_timeout: float | None = 3.0
    backend: str = "websockets"
    _websocket: Any | None = field(default=None, init=False, repr=False)
    _session: Any | None = field(default=None, init=False, repr=False)
    _connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _heartbeat_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    last_disconnect_reason: str | None = field(default=None, init=False)
    last_transport_at: datetime | None = field(default=None, init=False)
    last_application_ping_rtt_ms: float | None = field(
        default=None,
        init=False,
    )
    _last_application_ping_sent_monotonic: float | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _last_transport_monotonic: float | None = field(
        default=None,
        init=False,
        repr=False,
    )

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._websocket is not None:
                return
            if self.backend == "aiohttp":
                await self._connect_aiohttp()
                return
            if self.backend != "websockets":
                raise ValueError(f"unsupported websocket backend: {self.backend}")
            kwargs: dict[str, Any] = {
                "ping_interval": self.ping_interval,
                "ping_timeout": self.ping_timeout,
                "compression": None,
                "max_queue": self.max_queue,
                "open_timeout": self.open_timeout,
                "close_timeout": self.close_timeout,
                "additional_headers": {
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                    "Origin": "https://polymarket.com",
                },
            }
            if self.proxy_url:
                kwargs["proxy"] = self.proxy_url
            websocket = await websockets.connect(self.ws_url, **kwargs)
            self._websocket = websocket
            self.last_transport_at = datetime.now(timezone.utc)
            self._last_transport_monotonic = time.monotonic()
            self._last_application_ping_sent_monotonic = None
            self.last_application_ping_rtt_ms = None
            self._start_application_heartbeat(websocket)

    async def _connect_aiohttp(self) -> None:
        import aiohttp

        connector = None
        proxy_url = self.proxy_url
        if proxy_url and proxy_url.lower().startswith(("socks4://", "socks5://", "socks5h://")):
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(proxy_url.replace("socks5h://", "socks5://", 1))
            proxy_url = None
        session = aiohttp.ClientSession(
            connector=connector,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                "Origin": "https://polymarket.com",
            },
            timeout=aiohttp.ClientTimeout(total=None, connect=self.open_timeout),
            trust_env=False,
        )
        try:
            websocket = await session.ws_connect(
                self.ws_url,
                proxy=proxy_url,
                autoping=True,
                heartbeat=_aiohttp_heartbeat(
                    self.ping_interval,
                    self.ping_timeout,
                    application_ping_interval=self.application_ping_interval,
                ),
                compress=0,
                max_msg_size=0,
                timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=self.close_timeout),
            )
        except BaseException:
            await session.close()
            raise
        self._session = session
        self._websocket = websocket
        self.last_transport_at = datetime.now(timezone.utc)
        self._last_transport_monotonic = time.monotonic()
        self._last_application_ping_sent_monotonic = None
        self.last_application_ping_rtt_ms = None
        self._start_application_heartbeat(websocket)

    async def close(self, *, abort: bool = False) -> None:
        async with self._connect_lock:
            websocket = self._websocket
            session = self._session
            heartbeat_task = self._heartbeat_task
            self._websocket = None
            self._session = None
            self._heartbeat_task = None
        await _cancel_task(heartbeat_task)
        if websocket is None:
            if session is not None:
                await session.close()
            return
        try:
            if abort:
                _abort_transport(websocket)
            else:
                await websocket.close()
        except Exception:  # noqa: BLE001
            _abort_transport(websocket)
        finally:
            if session is not None:
                await session.close()

    async def subscribe(
        self,
        asset_ids: list[str],
        *,
        custom_feature_enabled: bool = True,
        initial: bool = True,
        initial_dump: bool = True,
    ) -> None:
        payload = {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": custom_feature_enabled,
            "initial_dump": bool(initial_dump),
        }
        if not initial:
            payload["operation"] = "subscribe"
        await self._send(payload)

    async def unsubscribe(self, asset_ids: list[str]) -> None:
        # Polymarket's dynamic market-channel control envelope deliberately
        # omits ``type``.  ``type=market`` belongs to the initial subscription
        # handshake; retaining it here was accepted by the socket writer but
        # did not reliably retire the assets upstream, so migrated tokens could
        # keep streaming on both their old and new shards until the old socket
        # happened to reconnect.
        await self._send({"assets_ids": asset_ids, "operation": "unsubscribe"})

    async def recv(self) -> list[dict[str, Any]]:
        raw = await self.recv_raw()
        return _decode(raw)

    async def recv_timestamped(
        self,
        *,
        executor: Executor | None = None,
        offload_decode: bool = True,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Receive a frame, timestamp it, then decode away from the event loop.

        Large market frames can take long enough to decode that doing so on the
        asyncio thread delays reads for every other logical connection in the
        same collector process.  The timestamp deliberately represents frame
        receipt, not completion of JSON decoding.
        """

        raw = await self.recv_raw()
        received_ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if offload_decode:
            loop = asyncio.get_running_loop()
            messages = await loop.run_in_executor(
                executor, decode_market_ws_payload, raw
            )
        else:
            messages = decode_market_ws_payload(raw)
        return received_ts_ms, messages

    async def recv_raw(self) -> Any:
        if self._websocket is None:
            await self.connect()
        assert self._websocket is not None
        websocket = self._websocket
        try:
            raw = await self._recv_raw(websocket)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._discard_broken(websocket)
            raise
        return raw

    async def _recv_raw(self, websocket: Any) -> Any:
        if self.backend != "aiohttp":
            raw = await websocket.recv()
            self.last_transport_at = datetime.now(timezone.utc)
            self._last_transport_monotonic = time.monotonic()
            self._record_application_pong(raw)
            return raw
        import aiohttp

        message = await websocket.receive()
        self.last_transport_at = datetime.now(timezone.utc)
        self._last_transport_monotonic = time.monotonic()
        if message.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
            self._record_application_pong(message.data)
            return message.data
        if message.type in {aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG}:
            return ""
        detail = (
            f"type={message.type.name} data={message.data!r} "
            f"close_code={getattr(websocket, 'close_code', None)!r} "
            f"exception={websocket.exception()!r}"
        )
        raise ConnectionError(f"websocket closed: {detail}")

    async def iter_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            if self._websocket is None:
                await self.connect()
            assert self._websocket is not None
            try:
                while True:
                    for item in await self.recv():
                        yield item
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_disconnect_reason = str(exc)
                await self.close()
                await asyncio.sleep(max(0.1, float(self.reconnect_seconds)))

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._websocket is None:
            await self.connect()
        assert self._websocket is not None
        websocket = self._websocket
        try:
            await self._send_raw(websocket, json.dumps(payload))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._discard_broken(websocket)
            raise

    async def _send_raw(self, websocket: Any, encoded: str) -> None:
        async with self._send_lock:
            if self._websocket is not websocket:
                raise ConnectionError("websocket changed before send")
            if self.backend == "aiohttp":
                await websocket.send_str(encoded)
            else:
                await websocket.send(encoded)

    def _start_application_heartbeat(self, websocket: Any) -> None:
        interval = self.application_ping_interval
        if interval is None or float(interval) <= 0:
            return
        self._heartbeat_task = asyncio.create_task(
            self._application_heartbeat_loop(websocket),
            name="polymarket-market-ws-heartbeat",
        )

    async def _application_heartbeat_loop(self, websocket: Any) -> None:
        interval = max(0.1, float(self.application_ping_interval or 0))
        try:
            while self._websocket is websocket:
                await asyncio.sleep(interval)
                if self._websocket is not websocket:
                    return
                now = time.monotonic()
                sent = self._last_application_ping_sent_monotonic
                timeout = self.application_ping_timeout
                if sent is not None:
                    if (
                        timeout is not None
                        and float(timeout) > 0
                        and now - sent > float(timeout)
                    ):
                        # A market-data frame received after this PING proves
                        # that the socket is alive. On a busy LOB shard the
                        # textual PONG can sit behind market frames long enough
                        # to exceed the heartbeat timeout. Closing that healthy
                        # stream manufactured 1006 reconnects and recovery
                        # gaps. Retire the stale probe; a truly half-open socket
                        # has no transport progress and still times out below.
                        if (
                            self._last_transport_monotonic is not None
                            and self._last_transport_monotonic > sent
                        ):
                            self._last_application_ping_sent_monotonic = None
                            continue
                        raise TimeoutError(
                            "Polymarket application PONG timed out"
                        )
                    continue
                self._last_application_ping_sent_monotonic = now
                await self._send_raw(websocket, "PING")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_disconnect_reason = f"application heartbeat failed: {exc}"
            await self._discard_broken(websocket)

    def _record_application_pong(self, raw: Any) -> None:
        value = (
            raw.decode("utf-8", errors="replace")
            if isinstance(raw, bytes)
            else raw
        )
        if not isinstance(value, str) or value.strip().upper() != "PONG":
            return
        sent = self._last_application_ping_sent_monotonic
        if sent is not None:
            self.last_application_ping_rtt_ms = max(
                0.0,
                (time.monotonic() - sent) * 1000.0,
            )
        self._last_application_ping_sent_monotonic = None

    def transport_metrics(self) -> dict[str, float | int | None]:
        """Return best-effort Linux socket pressure without reading payloads."""

        transport = _websocket_transport(self._websocket)
        raw_socket = (
            transport.get_extra_info("socket")
            if transport is not None
            and callable(getattr(transport, "get_extra_info", None))
            else None
        )
        receive_queue_bytes: int | None = None
        tcp_rtt_ms: float | None = None
        if raw_socket is not None:
            try:
                encoded = fcntl.ioctl(
                    raw_socket.fileno(),
                    termios.FIONREAD,
                    struct.pack("I", 0),
                )
                receive_queue_bytes = int(
                    struct.unpack("I", encoded)[0]
                )
            except (OSError, ValueError, TypeError, struct.error):
                pass
            try:
                info = raw_socket.getsockopt(
                    socket.IPPROTO_TCP,
                    socket.TCP_INFO,
                    256,
                )
                # Linux tcp_info.tcpi_rtt is a u32 at byte offset 68 and is
                # expressed in microseconds.
                if len(info) >= 72:
                    tcp_rtt_ms = (
                        float(struct.unpack_from("I", info, 68)[0])
                        / 1000.0
                    )
            except (OSError, ValueError, TypeError, struct.error):
                pass
        return {
            "application_ping_rtt_ms": (
                self.last_application_ping_rtt_ms
            ),
            "tcp_rtt_ms": tcp_rtt_ms,
            "tcp_receive_queue_bytes": receive_queue_bytes,
        }

    async def _discard_broken(self, websocket: Any) -> None:
        session = None
        heartbeat_task = None
        if self._websocket is websocket:
            self._websocket = None
            session = self._session
            self._session = None
            heartbeat_task = self._heartbeat_task
            self._heartbeat_task = None
            self._last_application_ping_sent_monotonic = None
        await _cancel_task(heartbeat_task)
        _abort_transport(websocket)
        if session is not None:
            await session.close()


async def _cancel_task(task: asyncio.Task[Any] | None) -> None:
    if task is None or task is asyncio.current_task():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _abort_transport(websocket: Any) -> None:
    transport = getattr(websocket, "transport", None)
    abort = getattr(transport, "abort", None)
    if callable(abort):
        try:
            abort()
        except Exception:  # noqa: BLE001
            pass


def _websocket_transport(websocket: Any) -> Any | None:
    if websocket is None:
        return None
    transport = getattr(websocket, "transport", None)
    if transport is not None:
        return transport
    response = getattr(websocket, "_response", None)
    connection = getattr(response, "connection", None)
    return getattr(connection, "transport", None)


def _aiohttp_heartbeat(
    ping_interval: float | None,
    ping_timeout: float | None,
    *,
    application_ping_interval: float | None = None,
) -> float | None:
    """Avoid competing active heartbeats when Polymarket text PING is enabled."""

    if application_ping_interval is not None and float(application_ping_interval) > 0:
        return None
    if ping_timeout is None or float(ping_timeout) <= 0:
        return None
    if ping_interval is None or float(ping_interval) <= 0:
        return None
    return float(ping_interval)


def _decode(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.upper() in _MARKET_WS_CONTROL_MESSAGES:
            return []
        try:
            parsed = _orjson.loads(text) if _orjson is not None else json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
    else:
        parsed = raw
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def decode_market_ws_payload(raw: Any) -> list[dict[str, Any]]:
    """Public decoder used by collectors that pipeline socket reads."""

    return _decode(raw)


def decode_market_ws_payload_strict(raw: Any) -> list[dict[str, Any]]:
    """Decode a durable frame while distinguishing keepalives from corruption."""

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise TypeError(
            f"durable market WS frame must be text, got {type(raw).__name__}"
        )
    text = raw.strip()
    if not text or text.upper() in _MARKET_WS_CONTROL_MESSAGES:
        return []
    parsed = _orjson.loads(text) if _orjson is not None else json.loads(text)
    if isinstance(parsed, list):
        if not all(isinstance(item, dict) for item in parsed):
            raise ValueError("market WS frame list contains non-object items")
        return list(parsed)
    if isinstance(parsed, dict):
        return [parsed]
    raise ValueError(
        f"market WS frame JSON must be object or list, got {type(parsed).__name__}"
    )


# The upstream sends NO NEW ASSETS as the acknowledgement for a native market
# discovery poll when there is nothing to announce.  It is a valid transport
# control frame, not malformed market data, and must therefore advance the WAL
# parser commit without entering quarantine.
_MARKET_WS_CONTROL_MESSAGES = frozenset({"PING", "PONG", "NO NEW ASSETS"})
