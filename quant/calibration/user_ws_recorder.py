"""Authenticated user-channel health probe and idempotent event normalization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .calibration_domain import payload_hash, redact_mapping
from .probe_plan import ProbePlan

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class DurableUserWsEventJournal:
    """Append-only, idempotent own-order event journal for crash recovery."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._event_keys = {
            str(row.get("event_key") or "")
            for row in self.load_events()
            if row.get("event_key")
        }

    def append(self, row: Mapping[str, Any]) -> bool:
        event_key = str(row.get("event_key") or "")
        if not event_key:
            raise ValueError("User WS journal row has no event_key")
        if event_key in self._event_keys:
            return False
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    dict(row), sort_keys=True, separators=(",", ":"), default=str
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        self._event_keys.add(event_key)
        return True

    def load_events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(
                    f"User WS journal row {line_number} is not a JSON object"
                )
            rows.append(value)
        return rows

    def sha256(self) -> str | None:
        if not self.path.exists():
            return None
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


class UserWsRecorder:
    def __init__(
        self, plan: ProbePlan, *, environ: Mapping[str, str] | None = None
    ) -> None:
        self.plan = plan
        self.environ = os.environ if environ is None else environ

    def probe_connection(
        self,
        condition_ids: list[str],
        *,
        stability_seconds: float = 1.0,
    ) -> dict[str, Any]:
        return asyncio.run(
            self._probe_connection(condition_ids, stability_seconds=stability_seconds)
        )

    async def _probe_connection(
        self,
        condition_ids: list[str],
        *,
        stability_seconds: float,
    ) -> dict[str, Any]:
        subscription = self._subscription(condition_ids)
        connected_at = datetime.now(timezone.utc)
        async with self._connect(subscription) as websocket:
            first_message: Any = None
            try:
                first_message = await asyncio.wait_for(
                    _receive_message(websocket),
                    timeout=max(0.1, stability_seconds),
                )
                if _error_message(first_message):
                    raise RuntimeError(
                        "user websocket rejected authentication or subscription"
                    )
            except asyncio.TimeoutError:
                if websocket.closed:
                    raise RuntimeError("user websocket closed during stability probe")
            return {
                "connected": True,
                "url": USER_WS_URL,
                "proxy_url": self.plan.network.proxy_url,
                "market_count": len(subscription["markets"]),
                "connected_at": connected_at.isoformat(),
                "observed_until": datetime.now(timezone.utc).isoformat(),
                "first_message_type": _message_type(first_message),
                "credentials_persisted": False,
            }

    def normalize_event(
        self,
        probe_id: str,
        payload: Mapping[str, Any],
        *,
        identity_scope: str | None = None,
    ) -> dict[str, Any]:
        received_at = datetime.now(timezone.utc)
        canonical_payload = redact_mapping(payload)
        safe = {**canonical_payload, "_received_at": received_at.isoformat()}
        event_type = str(
            payload.get("event_type") or payload.get("type") or "UNKNOWN"
        ).upper()
        event_ts = _event_time(payload)
        identity = {
            "identity_scope": str(identity_scope or probe_id),
            "event_type": event_type,
            "id": payload.get("id"),
            "status": payload.get("status"),
            "timestamp": payload.get("timestamp") or payload.get("last_update"),
            "payload": canonical_payload,
        }
        return {
            "event_key": payload_hash(identity, prefix="userws-"),
            "probe_id": str(probe_id),
            "event_type": event_type,
            "source": "polymarket-user-ws-v2",
            "event_ts": event_ts,
            "received_at": received_at,
            "payload": safe,
        }

    def record_until_terminal(
        self,
        *,
        probe_id: str,
        condition_ids: list[str],
        order_id: str,
        timeout_seconds: float = 120.0,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        max_reconnects: int = 5,
    ) -> dict[str, Any]:
        return asyncio.run(
            self._record_until_terminal(
                probe_id=probe_id,
                condition_ids=condition_ids,
                order_id=order_id,
                timeout_seconds=timeout_seconds,
                event_sink=event_sink,
                max_reconnects=max_reconnects,
            )
        )

    def submit_while_recording(
        self,
        *,
        probe_id: str,
        condition_ids: list[str],
        submit: Callable[[], Any],
        timeout_seconds: float = 120.0,
    ) -> tuple[Any, dict[str, Any]]:
        """Open the user feed before invoking the one-shot submit callback."""

        return asyncio.run(
            self._submit_while_recording(
                probe_id=probe_id,
                condition_ids=condition_ids,
                submit=submit,
                timeout_seconds=timeout_seconds,
            )
        )

    def submit_watch_cancel(
        self,
        *,
        probe_id: str,
        condition_ids: list[str],
        submit: Callable[[], Any],
        cancel: Callable[[str], Any],
        resting_seconds: float,
        post_cancel_seconds: float = 30.0,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        submission_sink: Callable[[Any, str, datetime], Any] | None = None,
        max_reconnects: int = 5,
        cancel_on_first_fill: bool = False,
        original_size: Decimal | str | None = None,
    ) -> tuple[Any, Any | None, dict[str, Any]]:
        """Record one resting order continuously across submit and exact cancel."""

        return asyncio.run(
            self._submit_watch_cancel(
                probe_id=probe_id,
                condition_ids=condition_ids,
                submit=submit,
                cancel=cancel,
                resting_seconds=resting_seconds,
                post_cancel_seconds=post_cancel_seconds,
                event_sink=event_sink,
                submission_sink=submission_sink,
                max_reconnects=max_reconnects,
                cancel_on_first_fill=cancel_on_first_fill,
                original_size=original_size,
            )
        )

    def record_orders(
        self,
        *,
        collector_id: str,
        condition_ids: list[str],
        order_ids: list[str],
        timeout_seconds: float,
        event_sink: Callable[[Mapping[str, Any]], Any],
        identity_scope: str,
        max_reconnects: int = 5,
    ) -> dict[str, Any]:
        """Persist events for accepted orders without exposing a submit callback."""

        return asyncio.run(
            self._record_orders(
                collector_id=collector_id,
                condition_ids=condition_ids,
                order_ids=order_ids,
                timeout_seconds=timeout_seconds,
                event_sink=event_sink,
                identity_scope=identity_scope,
                max_reconnects=max_reconnects,
            )
        )

    async def _submit_watch_cancel(
        self,
        *,
        probe_id: str,
        condition_ids: list[str],
        submit: Callable[[], Any],
        cancel: Callable[[str], Any],
        resting_seconds: float,
        post_cancel_seconds: float,
        event_sink: Callable[[Mapping[str, Any]], Any] | None,
        submission_sink: Callable[[Any, str, datetime], Any] | None,
        max_reconnects: int,
        cancel_on_first_fill: bool = False,
        original_size: Decimal | str | None = None,
    ) -> tuple[Any, Any | None, dict[str, Any]]:
        subscription = self._subscription(condition_ids)
        events: list[dict[str, Any]] = []
        submission: Any | None = None
        order_id = ""
        cancellation: Any | None = None
        cancel_error: str | None = None
        submitted_at: datetime | None = None
        cancel_requested_at: datetime | None = None
        terminal_observed = False
        reconnects = 0
        cancel_trigger = "HORIZON_EXPIRED"
        first_positive_match_at: datetime | None = None
        matched_size_before_cancel = Decimal("0")
        requested_size = _decimal_or_none(original_size)
        full_observed_before_cancel = False
        loop = asyncio.get_running_loop()
        resting_deadline: float | None = None
        post_cancel_deadline: float | None = None
        while not terminal_observed:
            try:
                async with self._connect(subscription) as websocket:
                    try:
                        first = await asyncio.wait_for(
                            _receive_message(websocket), timeout=0.25
                        )
                        if _error_message(first):
                            raise RuntimeError(
                                "user websocket rejected authentication or subscription"
                            )
                    except asyncio.TimeoutError:
                        pass

                    if submission is None:
                        submission = await asyncio.to_thread(submit)
                        submitted_at = datetime.now(timezone.utc)
                        order_id = _submission_order_id(submission)
                        if not order_id:
                            raise RuntimeError(
                                "accepted submission did not expose an order id"
                            )
                        if submission_sink is not None:
                            submission_sink(submission, order_id, submitted_at)
                        resting_deadline = loop.time() + max(
                            0.1, float(resting_seconds)
                        )

                    assert resting_deadline is not None
                    while loop.time() < resting_deadline:
                        payloads = await _collect_matching_message(
                            websocket,
                            events=events,
                            probe_id=probe_id,
                            order_id=order_id,
                            timeout_seconds=min(
                                2.0,
                                max(0.05, resting_deadline - loop.time()),
                            ),
                            normalize=self.normalize_event,
                            event_sink=event_sink,
                        )
                        observed_match = max(
                            (
                                _matched_size_for_order(payload, order_id)
                                for payload in payloads
                            ),
                            default=Decimal("0"),
                        )
                        if observed_match > matched_size_before_cancel:
                            matched_size_before_cancel = observed_match
                        if observed_match > 0 and first_positive_match_at is None:
                            first_positive_match_at = datetime.now(timezone.utc)
                        if requested_size is not None and _is_full_match(
                            observed_match, requested_size
                        ):
                            cancel_trigger = "FULL_BEFORE_CANCEL"
                            full_observed_before_cancel = True
                            break
                        if cancel_on_first_fill and observed_match > 0:
                            cancel_trigger = "FIRST_PARTIAL_FILL"
                            break

                    if cancel_requested_at is None and not full_observed_before_cancel:
                        cancel_requested_at = datetime.now(timezone.utc)
                        try:
                            cancellation = await asyncio.to_thread(cancel, order_id)
                        except Exception as exc:
                            cancel_error = f"{exc.__class__.__name__}:{str(exc)[:500]}"
                        post_cancel_deadline = loop.time() + max(
                            0.1, float(post_cancel_seconds)
                        )
                    elif post_cancel_deadline is None:
                        post_cancel_deadline = loop.time() + max(
                            0.1, float(post_cancel_seconds)
                        )

                    assert post_cancel_deadline is not None
                    while loop.time() < post_cancel_deadline:
                        payloads = await _collect_matching_message(
                            websocket,
                            events=events,
                            probe_id=probe_id,
                            order_id=order_id,
                            timeout_seconds=min(
                                2.0,
                                max(0.05, post_cancel_deadline - loop.time()),
                            ),
                            normalize=self.normalize_event,
                            event_sink=event_sink,
                        )
                        if any(
                            _terminal_order_or_trade(payload) for payload in payloads
                        ):
                            terminal_observed = True
                            break
                    if loop.time() >= post_cancel_deadline:
                        break
            except (OSError, RuntimeError) as exc:
                reconnects += 1
                if reconnects > max(0, int(max_reconnects)):
                    raise RuntimeError(
                        "user websocket reconnect budget exhausted"
                    ) from exc
                await asyncio.sleep(min(2.0, 0.1 * (2 ** (reconnects - 1))))

        if submission is None:
            raise RuntimeError("user websocket closed before order submission")

        return (
            submission,
            cancellation,
            {
                "status": "TERMINAL" if terminal_observed else "TIMEOUT",
                "events": events,
                "order_id": order_id,
                "submitted_at": submitted_at.isoformat() if submitted_at else None,
                "cancel_requested_at": (
                    cancel_requested_at.isoformat() if cancel_requested_at else None
                ),
                "cancel_error": cancel_error,
                "cancel_attempted": cancel_requested_at is not None,
                "cancel_trigger": cancel_trigger,
                "cancel_on_first_fill": bool(cancel_on_first_fill),
                "first_positive_match_at": (
                    first_positive_match_at.isoformat()
                    if first_positive_match_at
                    else None
                ),
                "matched_size_before_cancel": format(
                    matched_size_before_cancel, "f"
                ),
                "original_size": (
                    format(requested_size, "f") if requested_size is not None else None
                ),
                "terminal_observed": terminal_observed,
                "reconnects": reconnects,
                "durable_event_sink": event_sink is not None,
                "credentials_persisted": False,
            },
        )

    async def _record_orders(
        self,
        *,
        collector_id: str,
        condition_ids: list[str],
        order_ids: list[str],
        timeout_seconds: float,
        event_sink: Callable[[Mapping[str, Any]], Any],
        identity_scope: str,
        max_reconnects: int,
    ) -> dict[str, Any]:
        expected = {str(order_id).lower() for order_id in order_ids if order_id}
        if not expected:
            return {
                "status": "NO_ORDERS",
                "event_count": 0,
                "terminal_order_ids": [],
                "reconnects": 0,
                "credentials_persisted": False,
            }
        subscription = self._subscription(condition_ids)
        deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
        terminal: set[str] = set()
        event_count = 0
        reconnects = 0
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with self._connect(subscription) as websocket:
                    while asyncio.get_running_loop().time() < deadline:
                        remaining = deadline - asyncio.get_running_loop().time()
                        try:
                            message = await asyncio.wait_for(
                                _receive_message(websocket),
                                timeout=min(8.0, max(0.05, remaining)),
                            )
                        except asyncio.TimeoutError:
                            await websocket.send_str("PING")
                            continue
                        if _error_message(message):
                            raise RuntimeError(
                                "user websocket rejected authentication or subscription"
                            )
                        for payload in _messages(message):
                            matched = sorted(
                                order_id
                                for order_id in expected
                                if _matches_order(payload, order_id)
                            )
                            if not matched:
                                continue
                            normalized = self.normalize_event(
                                collector_id,
                                payload,
                                identity_scope=identity_scope,
                            )
                            normalized["matched_order_ids"] = matched
                            event_sink(normalized)
                            event_count += 1
                            if _terminal_order_or_trade(payload):
                                terminal.update(matched)
                            if terminal == expected:
                                return {
                                    "status": "ALL_TERMINAL",
                                    "event_count": event_count,
                                    "terminal_order_ids": sorted(terminal),
                                    "reconnects": reconnects,
                                    "credentials_persisted": False,
                                }
            except (OSError, RuntimeError) as exc:
                reconnects += 1
                if reconnects > max(0, int(max_reconnects)):
                    raise RuntimeError(
                        "user websocket reconnect budget exhausted"
                    ) from exc
                await asyncio.sleep(min(2.0, 0.1 * (2 ** (reconnects - 1))))
        return {
            "status": "WINDOW_COMPLETE",
            "event_count": event_count,
            "terminal_order_ids": sorted(terminal),
            "reconnects": reconnects,
            "credentials_persisted": False,
        }

    async def _submit_while_recording(
        self,
        *,
        probe_id: str,
        condition_ids: list[str],
        submit: Callable[[], Any],
        timeout_seconds: float,
    ) -> tuple[Any, dict[str, Any]]:
        subscription = self._subscription(condition_ids)
        events: list[dict[str, Any]] = []
        async with self._connect(subscription) as websocket:
            try:
                first = await asyncio.wait_for(
                    _receive_message(websocket), timeout=0.25
                )
                if _error_message(first):
                    raise RuntimeError(
                        "user websocket rejected authentication or subscription"
                    )
            except asyncio.TimeoutError:
                pass

            submission = await asyncio.to_thread(submit)
            order_id = _submission_order_id(submission)
            if not order_id:
                raise RuntimeError("accepted submission did not expose an order id")
            deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    message = await asyncio.wait_for(
                        _receive_message(websocket),
                        timeout=min(8.0, remaining),
                    )
                except asyncio.TimeoutError:
                    await websocket.send_str("PING")
                    continue
                if _error_message(message):
                    raise RuntimeError(
                        "user websocket rejected authentication or subscription"
                    )
                for payload in _messages(message):
                    if not _matches_order(payload, order_id):
                        continue
                    normalized = self.normalize_event(probe_id, payload)
                    events.append(normalized)
                    if _terminal_trade(payload):
                        return submission, {
                            "status": "TERMINAL",
                            "events": events,
                            "terminal_status": str(payload.get("status") or "").upper(),
                            "credentials_persisted": False,
                        }
            return submission, {
                "status": "TIMEOUT",
                "events": events,
                "terminal_status": None,
                "credentials_persisted": False,
            }

    async def _record_until_terminal(
        self,
        *,
        probe_id: str,
        condition_ids: list[str],
        order_id: str,
        timeout_seconds: float,
        event_sink: Callable[[Mapping[str, Any]], Any] | None,
        max_reconnects: int,
    ) -> dict[str, Any]:
        subscription = self._subscription(condition_ids)
        events: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        reconnects = 0
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with self._connect(subscription) as websocket:
                    while asyncio.get_running_loop().time() < deadline:
                        remaining = deadline - asyncio.get_running_loop().time()
                        payloads = await _collect_matching_message(
                            websocket,
                            events=events,
                            probe_id=probe_id,
                            order_id=order_id,
                            timeout_seconds=min(8.0, remaining),
                            normalize=self.normalize_event,
                            event_sink=event_sink,
                        )
                        for payload in payloads:
                            if _terminal_order_or_trade(payload):
                                return {
                                    "status": "TERMINAL",
                                    "events": events,
                                    "terminal_status": str(
                                        payload.get("status") or ""
                                    ).upper(),
                                    "reconnects": reconnects,
                                    "credentials_persisted": False,
                                }
            except (OSError, RuntimeError) as exc:
                reconnects += 1
                if reconnects > max(0, int(max_reconnects)):
                    raise RuntimeError(
                        "user websocket reconnect budget exhausted"
                    ) from exc
                await asyncio.sleep(min(2.0, 0.1 * (2 ** (reconnects - 1))))
        return {
            "status": "TIMEOUT",
            "events": events,
            "terminal_status": None,
            "reconnects": reconnects,
            "credentials_persisted": False,
        }

    @asynccontextmanager
    async def _connect(self, subscription: Mapping[str, Any]):
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.ws_connect(
                USER_WS_URL,
                proxy=self.plan.network.proxy_url,
                autoping=True,
                heartbeat=None,
                max_msg_size=4 * 1024 * 1024,
            ) as websocket:
                await websocket.send_str(
                    json.dumps(subscription, separators=(",", ":"))
                )
                yield websocket

    def _credentials(self) -> dict[str, str]:
        names = {
            "apiKey": self.plan.account.api_key_env,
            "secret": self.plan.account.api_secret_env,
            "passphrase": self.plan.account.api_passphrase_env,
        }
        values = {
            key: str(self.environ.get(name) or "").strip()
            for key, name in names.items()
        }
        missing = [names[key] for key, value in values.items() if not value]
        if missing:
            raise RuntimeError(
                "user websocket credentials are missing: " + ", ".join(missing)
            )
        return values

    def _subscription(self, condition_ids: list[str]) -> dict[str, Any]:
        credentials = self._credentials()
        return {
            "auth": credentials,
            "markets": list(
                dict.fromkeys(str(item) for item in condition_ids if str(item))
            ),
            "type": "user",
        }


def _parse_message(raw: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


async def _receive_message(websocket: Any) -> Any:
    import aiohttp

    while True:
        message = await websocket.receive()
        if message.type == aiohttp.WSMsgType.TEXT:
            return _parse_message(message.data)
        if message.type == aiohttp.WSMsgType.BINARY:
            return _parse_message(message.data.decode("utf-8"))
        if message.type in {
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.ERROR,
        }:
            raise RuntimeError(f"user websocket closed: {message.type.name}")


def _submission_order_id(value: Any) -> str:
    if isinstance(value, tuple) and len(value) >= 2 and isinstance(value[1], Mapping):
        response = value[1]
    elif isinstance(value, Mapping):
        response = value
    else:
        return ""
    return str(response.get("orderID") or response.get("order_id") or "")


def _message_type(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return str(value.get("event_type") or value.get("type") or "UNKNOWN")
    if isinstance(value, list) and value:
        return _message_type(value[0])
    return None


def _error_message(value: Any) -> bool:
    if isinstance(value, list):
        return any(_error_message(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    kind = str(
        value.get("event_type") or value.get("type") or value.get("status") or ""
    ).upper()
    message = str(value.get("error") or value.get("message") or "").lower()
    return kind in {"ERROR", "FAILED", "UNAUTHORIZED"} or any(
        fragment in message
        for fragment in ("unauthorized", "authentication failed", "invalid api")
    )


def _messages(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _matches_order(payload: Mapping[str, Any], order_id: str) -> bool:
    expected = str(order_id).lower()
    if not expected:
        return False
    values = {
        str(payload.get(key) or "").lower()
        for key in ("id", "order_id", "orderID", "taker_order_id")
        if payload.get(key) not in (None, "")
    }
    maker_orders = (
        payload.get("maker_orders")
        if isinstance(payload.get("maker_orders"), list)
        else []
    )
    values.update(
        str(item.get("order_id") or "").lower()
        for item in maker_orders
        if isinstance(item, Mapping) and item.get("order_id") not in (None, "")
    )
    return expected in values


def _terminal_trade(payload: Mapping[str, Any]) -> bool:
    event_type = str(payload.get("event_type") or payload.get("type") or "").upper()
    status = str(payload.get("status") or "").upper().removeprefix("TRADE_STATUS_")
    return event_type == "TRADE" and status in {"CONFIRMED", "FAILED"}


def _terminal_order_or_trade(payload: Mapping[str, Any]) -> bool:
    event_type = str(payload.get("event_type") or payload.get("type") or "").upper()
    status = str(payload.get("status") or "").upper().removeprefix("TRADE_STATUS_")
    if event_type == "TRADE":
        return status in {"CONFIRMED", "FAILED"}
    return event_type == "ORDER" and status in {
        "CANCELED",
        "CANCELLED",
        "FILLED",
        "MATCHED",
    }


def _matched_size_for_order(
    payload: Mapping[str, Any], order_id: str
) -> Decimal:
    """Return the strongest cumulative/per-trade match evidence for one order."""

    expected = str(order_id).lower()
    observed = Decimal("0")
    direct_ids = {
        str(payload.get(key) or "").lower()
        for key in ("id", "order_id", "orderID")
        if payload.get(key) not in (None, "")
    }
    if expected in direct_ids:
        for key in ("size_matched", "matched_size", "sizeMatched"):
            observed = max(observed, _decimal_or_zero(payload.get(key)))
    maker_orders = payload.get("maker_orders")
    if isinstance(maker_orders, list):
        for maker_order in maker_orders:
            if not isinstance(maker_order, Mapping):
                continue
            maker_order_id = str(
                maker_order.get("order_id")
                or maker_order.get("orderID")
                or maker_order.get("id")
                or ""
            ).lower()
            if maker_order_id != expected:
                continue
            for key in ("matched_amount", "size_matched", "matched_size"):
                observed = max(observed, _decimal_or_zero(maker_order.get(key)))
    return observed


def _is_full_match(observed: Decimal, requested: Decimal) -> bool:
    tolerance = max(Decimal("0.000001"), requested * Decimal("0.000001"))
    return requested > 0 and observed + tolerance >= requested


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return max(Decimal("0"), parsed)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    parsed = _decimal_or_zero(value)
    return parsed if parsed > 0 else None


async def _collect_matching_message(
    websocket: Any,
    *,
    events: list[dict[str, Any]],
    probe_id: str,
    order_id: str,
    timeout_seconds: float,
    normalize: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
) -> list[Mapping[str, Any]]:
    try:
        message = await asyncio.wait_for(
            _receive_message(websocket),
            timeout=max(0.05, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        await websocket.send_str("PING")
        return []
    if _error_message(message):
        raise RuntimeError("user websocket rejected authentication or subscription")
    matched: list[Mapping[str, Any]] = []
    for payload in _messages(message):
        if not _matches_order(payload, order_id):
            continue
        matched.append(payload)
        normalized = normalize(probe_id, payload)
        events.append(normalized)
        if event_sink is not None:
            event_sink(normalized)
    return matched


def _event_time(payload: Mapping[str, Any]) -> datetime:
    value = (
        payload.get("timestamp")
        or payload.get("last_update")
        or payload.get("match_time")
    )
    if value not in (None, ""):
        try:
            numeric = float(value)
            seconds = numeric / 1000 if numeric > 10_000_000_000 else numeric
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    return datetime.now(timezone.utc)
