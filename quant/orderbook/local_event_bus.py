"""Bounded Unix-socket event bus between colocated L2 collectors and Paper."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "poly-quant-local-l2-event-v1"
MAX_LINE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class LocalEventEnvelope:
    source: str
    producer_id: str
    kind: str
    sent_at: str
    received_ts_ms: int
    shard_id: int | None
    messages: tuple[dict[str, Any], ...]

    def to_json_line(self) -> bytes:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        return (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            + b"\n"
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LocalEventEnvelope":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported local L2 event schema")
        source = str(payload.get("source") or "").strip()
        producer_id = str(payload.get("producer_id") or "").strip()
        if not source or not producer_id:
            raise ValueError("local L2 event source and producer_id are required")
        messages = payload.get("messages") or ()
        if not isinstance(messages, (list, tuple)):
            raise ValueError("local L2 event messages must be a list")
        return cls(
            source=source,
            producer_id=producer_id,
            kind=str(payload.get("kind") or "events"),
            sent_at=str(payload.get("sent_at") or _utc_now()),
            received_ts_ms=int(payload.get("received_ts_ms") or 0),
            shard_id=(
                int(payload["shard_id"])
                if payload.get("shard_id") is not None
                else None
            ),
            messages=tuple(dict(item) for item in messages if isinstance(item, Mapping)),
        )


class UnixEventPublisher:
    """Best-effort publisher which can never backpressure the L2 collector."""

    def __init__(
        self,
        socket_path: Path,
        *,
        source: str,
        producer_id: str,
        queue_size: int = 1024,
        reconnect_seconds: float = 1.0,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.source = str(source)
        self.producer_id = str(producer_id)
        self.queue: asyncio.Queue[LocalEventEnvelope] = asyncio.Queue(
            maxsize=max(1, int(queue_size))
        )
        self.reconnect_seconds = max(0.1, float(reconnect_seconds))
        self.connected = False
        self.enqueued = 0
        self.published = 0
        self.dropped = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.last_published_at: str | None = None
        self.connected_socket_inode: int | None = None
        self._gap_assets: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name=f"local-l2-publisher-{self.producer_id}",
            )

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def publish(
        self,
        *,
        messages: Iterable[Mapping[str, Any]] = (),
        received_ts_ms: int | None = None,
        shard_id: int | None = None,
        kind: str = "events",
    ) -> bool:
        if kind == "heartbeat" and (not self.connected or not self.queue.empty()):
            return False
        rows = tuple(dict(item) for item in messages)
        now_ms = int(time.time() * 1000) if received_ts_ms is None else int(received_ts_ms)
        envelope = LocalEventEnvelope(
            source=self.source,
            producer_id=self.producer_id,
            kind=kind,
            sent_at=_utc_now(),
            received_ts_ms=now_ms,
            shard_id=shard_id,
            messages=rows,
        )
        try:
            self.queue.put_nowait(envelope)
        except asyncio.QueueFull:
            self.dropped += 1
            self._gap_assets.update(_affected_asset_ids(rows))
            return False
        self.enqueued += 1
        return True

    def health(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "source": self.source,
            "producer_id": self.producer_id,
            "socket_path": str(self.socket_path),
            "connected": self.connected,
            "queue_size": self.queue.qsize(),
            "queue_capacity": self.queue.maxsize,
            "enqueued": self.enqueued,
            "published": self.published,
            "dropped": self.dropped,
            "reconnects": self.reconnects,
            "pending_gap_assets": len(self._gap_assets),
            "last_published_at": self.last_published_at,
            "last_error": self.last_error,
            "connected_socket_inode": self.connected_socket_inode,
        }

    async def _run(self) -> None:
        while not self._stop.is_set():
            writer: asyncio.StreamWriter | None = None
            try:
                reader, writer = await asyncio.open_unix_connection(
                    path=str(self.socket_path),
                    limit=MAX_LINE_BYTES,
                )
                self.connected_socket_inode = _socket_inode(self.socket_path)
                self.connected = True
                self.last_error = None
                if self._gap_assets:
                    await self._write_gap_envelope(writer)
                while not self._stop.is_set():
                    envelope = await self.queue.get()
                    try:
                        current_inode = _socket_inode(self.socket_path)
                        if (
                            reader.at_eof()
                            or writer.is_closing()
                            or current_inode is None
                            or current_inode != self.connected_socket_inode
                        ):
                            self._gap_assets.update(
                                _affected_asset_ids(envelope.messages)
                            )
                            raise ConnectionResetError(
                                "paper event consumer replaced or closed the Unix socket"
                            )
                        if self._gap_assets:
                            await self._write_gap_envelope(writer)
                        writer.write(envelope.to_json_line())
                        await writer.drain()
                        self.published += 1
                        self.last_published_at = _utc_now()
                    finally:
                        self.queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.reconnects += 1
                self.last_error = f"{exc.__class__.__name__}: {str(exc)[:300]}"
                await asyncio.sleep(self.reconnect_seconds)
            finally:
                self.connected = False
                self.connected_socket_inode = None
                if writer is not None:
                    writer.close()
                    await asyncio.gather(writer.wait_closed(), return_exceptions=True)

    async def _write_gap_envelope(self, writer: asyncio.StreamWriter) -> None:
        asset_ids = sorted(self._gap_assets)
        if not asset_ids:
            return
        now_ms = int(time.time() * 1000)
        envelope = LocalEventEnvelope(
            source=self.source,
            producer_id=self.producer_id,
            kind="publisher_gap",
            sent_at=_utc_now(),
            received_ts_ms=now_ms,
            shard_id=None,
            messages=tuple(
                {
                    "event_type": "connection_gap",
                    "asset_id": asset_id,
                    "timestamp": now_ms,
                    "hash": "local_event_bus_queue_overflow",
                }
                for asset_id in asset_ids
            ),
        )
        writer.write(envelope.to_json_line())
        await writer.drain()
        self._gap_assets.difference_update(asset_ids)


class UnixEventServer:
    """Multi-producer Unix stream server for local collector envelopes."""

    def __init__(
        self,
        socket_path: Path,
        handler: Callable[[LocalEventEnvelope], Awaitable[None]],
    ) -> None:
        self.socket_path = Path(socket_path)
        self.handler = handler
        self.server: asyncio.AbstractServer | None = None
        self.connected_clients = 0
        self.received_envelopes = 0
        self.decode_errors = 0
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
            limit=MAX_LINE_BYTES,
        )
        os.chmod(self.socket_path, 0o660)

    async def serve_forever(self) -> None:
        if self.server is None:
            await self.start()
        assert self.server is not None
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        writers = tuple(self._writers)
        if writers:
            await asyncio.gather(
                *(_close_writer(writer) for writer in writers),
                return_exceptions=True,
            )
        self._writers.clear()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.connected_clients += 1
        self._writers.add(writer)
        try:
            while line := await reader.readline():
                try:
                    payload = json.loads(line)
                    envelope = LocalEventEnvelope.from_payload(payload)
                except (ValueError, TypeError, json.JSONDecodeError):
                    self.decode_errors += 1
                    continue
                self.received_envelopes += 1
                await self.handler(envelope)
        finally:
            self.connected_clients = max(0, self.connected_clients - 1)
            self._writers.discard(writer)
            await _close_writer(writer)


def filter_execution_messages(
    messages: Iterable[Mapping[str, Any]],
    execution_asset_ids: set[str],
) -> tuple[dict[str, Any], ...]:
    """Keep only messages relevant to the execution universe."""

    filtered: list[dict[str, Any]] = []
    for raw in messages:
        message = dict(raw)
        event_type = str(message.get("event_type") or message.get("type") or "")
        if event_type == "price_change":
            changes = [
                dict(change)
                for change in message.get("price_changes") or ()
                if isinstance(change, Mapping)
                and str(change.get("asset_id") or change.get("token_id") or "")
                in execution_asset_ids
            ]
            if changes:
                message["price_changes"] = changes
                filtered.append(message)
            continue
        asset_id = str(message.get("asset_id") or message.get("token_id") or "")
        if asset_id in execution_asset_ids:
            filtered.append(message)
    return tuple(filtered)


def _affected_asset_ids(messages: Iterable[Mapping[str, Any]]) -> set[str]:
    asset_ids: set[str] = set()
    for message in messages:
        direct = str(message.get("asset_id") or message.get("token_id") or "").strip()
        if direct:
            asset_ids.add(direct)
        for change in message.get("price_changes") or ():
            if not isinstance(change, Mapping):
                continue
            asset_id = str(change.get("asset_id") or change.get("token_id") or "").strip()
            if asset_id:
                asset_ids.add(asset_id)
    return asset_ids


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _socket_inode(path: Path) -> int | None:
    try:
        return int(path.stat().st_ino)
    except OSError:
        return None


async def _close_writer(
    writer: asyncio.StreamWriter,
    *,
    timeout_seconds: float = 2.0,
) -> None:
    writer.close()
    try:
        await asyncio.wait_for(
            writer.wait_closed(),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except (TimeoutError, ConnectionError, OSError):
        transport = getattr(writer, "transport", None)
        if transport is not None:
            transport.abort()
