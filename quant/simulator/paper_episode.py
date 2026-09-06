"""Export one recorded paper lifecycle as a read-only deterministic SimEvent episode."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from quant.core.db import postgres_connection

from .kernel.deterministic_id import deterministic_id
from .kernel.event import SimEvent
from .kernel.event_journal import EventJournal
from .kernel.event_priority import EventPriority

MODEL_VERSION = "paper-lifecycle-adapter-v1"

_LIFECYCLE_EVENT_TYPES: dict[str, tuple[str, int, int]] = {
    "CREATED": ("STRATEGY_INTENT", int(EventPriority.STRATEGY_INTENT), 10),
    "SUBMIT_QUEUED": ("STRATEGY_INTENT", int(EventPriority.STRATEGY_INTENT), 20),
    "MARKET_TERMS_RESOLVED": (
        "MARKET_TRADING_STATUS",
        int(EventPriority.VENUE_STATE),
        30,
    ),
    "VENUE_REGIME_BOUND": ("MARKET_TRADING_STATUS", int(EventPriority.VENUE_STATE), 25),
    "RISK_DECISION": ("STRATEGY_INTENT", int(EventPriority.STRATEGY_INTENT), 40),
    "SUBMIT_INFLIGHT": (
        "PAPER_COMMAND_ARRIVAL",
        int(EventPriority.PAPER_COMMAND_ARRIVAL),
        50,
    ),
    "VENUE_ACCEPTED": (
        "PAPER_COMMAND_ARRIVAL",
        int(EventPriority.PAPER_COMMAND_ARRIVAL),
        60,
    ),
    "WORKING": ("PAPER_MATCH", int(EventPriority.PAPER_VENUE_ACTION), 70),
    "PARTIALLY_MATCHED_PROVISIONAL": (
        "PAPER_MATCH",
        int(EventPriority.PAPER_VENUE_ACTION),
        80,
    ),
    "MATCHED_PROVISIONAL": ("PAPER_MATCH", int(EventPriority.PAPER_VENUE_ACTION), 80),
    "CANCELED": ("PAPER_CANCEL", int(EventPriority.PAPER_VENUE_ACTION), 80),
    "CANCELLED": ("PAPER_CANCEL", int(EventPriority.PAPER_VENUE_ACTION), 80),
    "EXPIRED": ("PAPER_EXPIRE", int(EventPriority.PAPER_VENUE_ACTION), 80),
    "SETTLEMENT_PENDING": (
        "POSITION_OPERATION_CONFIRMATION",
        int(EventPriority.POSITION_OPERATION),
        90,
    ),
    "CONFIRMED": (
        "POSITION_OPERATION_CONFIRMATION",
        int(EventPriority.POSITION_OPERATION),
        100,
    ),
    "FAILED_REVERSED": (
        "POSITION_OPERATION_CONFIRMATION",
        int(EventPriority.POSITION_OPERATION),
        100,
    ),
    "REJECTED": ("PAPER_CANCEL", int(EventPriority.PAPER_VENUE_ACTION), 80),
}


def export_paper_episode(audit_key: str, output: Path) -> dict[str, Any]:
    """Read one existing paper order lifecycle and write a portable episode.

    This exporter deliberately records the historical state transitions; it
    does not invoke the taker matcher, recalculate a fill, or submit an order.
    """
    loaded = load_paper_lifecycle_events(audit_key)
    if loaded is None:
        return {
            "status": "BLOCKED",
            "reason": "paper_taker_audit_not_found",
            "audit_key": str(audit_key),
            "read_only": True,
            "live_submission_performed": False,
        }
    audit, events = loaded
    regime = _bound_regime(audit)
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    journal = EventJournal(events)
    journal.write_jsonl(destination / "sim_events.jsonl")
    manifest = {
        "schema_version": "paper_lifecycle_sim_episode_v1",
        "audit_key": str(audit_key),
        "strategy_id": str(audit["strategy_id"]),
        "client_order_id": str(audit["client_order_id"]),
        "asset_id": str(audit["asset_id"]),
        "paper_status": str(audit["status"]),
        "paper_reason": str(audit["reason"]),
        "event_count": journal.event_count,
        "journal_hash": journal.journal_hash,
        "source_lifecycle_event_count": len(events),
        "evidence_level": "RECORDED_PAPER_LIFECYCLE_NOT_FULL_MATCHER_REPLAY",
        "model_version": MODEL_VERSION,
        "venue_regime_id": regime["regime_id"],
        "venue_regime_source_hash": regime["source_hash"],
        "venue_regime_source": "recorded_paper_order",
        "read_only": True,
        "live_submission_performed": False,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "PASS", "output": str(destination), **manifest}


def load_paper_lifecycle_events(
    audit_key: str,
    *,
    connection_factory=postgres_connection,
) -> tuple[Mapping[str, Any], tuple[SimEvent, ...]] | None:
    """Read an existing paper lifecycle without recalculating or submitting it."""

    with connection_factory(readonly=True) as conn:
        audit = _fetch_audit(conn, audit_key)
        lifecycle_rows = _fetch_lifecycle_rows(conn, audit_key)
    if audit is None:
        return None
    return audit, lifecycle_rows_to_sim_events(audit, lifecycle_rows)


def lifecycle_rows_to_sim_events(
    audit: Mapping[str, Any],
    lifecycle_rows: Sequence[Mapping[str, Any]],
) -> tuple[SimEvent, ...]:
    """Convert existing paper-order state rows without relying on database IDs."""
    aggregate_key = f"paper-order:{audit['audit_key']}"
    if lifecycle_rows:
        events = [_lifecycle_event(audit, row, aggregate_key) for row in lifecycle_rows]
        return tuple(sorted(events, key=lambda event: event.sort_key))
    return _audit_only_events(audit, aggregate_key)


def _lifecycle_event(
    audit: Mapping[str, Any],
    row: Mapping[str, Any],
    aggregate_key: str,
) -> SimEvent:
    event_type, priority, source_sequence = _lifecycle_spec(row)
    idempotency_key = str(row["idempotency_key"])
    tiebreaker = deterministic_id(
        "paper-lifecycle-tie",
        {
            "audit_key": str(audit["audit_key"]),
            "event_ts_ns": _timestamp_ns(row["event_ts"]),
            "recorded_event_type": str(row["event_type"]),
            "from_state": _text_or_none(row.get("from_state")),
            "to_state": _text_or_none(row.get("to_state")),
            "reason": _text_or_none(row.get("reason")),
            "checkpoint_id": _text_or_none(row.get("checkpoint_id")),
            "source_sequence": source_sequence,
        },
    )
    return SimEvent.build(
        event_type=event_type,
        event_ts_ns=_timestamp_ns(row["event_ts"]),
        source_sequence=source_sequence,
        aggregate_key=aggregate_key,
        priority=priority,
        source_event_id=idempotency_key,
        deterministic_tiebreaker=tiebreaker,
        model_version=MODEL_VERSION,
        payload={
            "audit_key": str(audit["audit_key"]),
            "strategy_id": str(audit["strategy_id"]),
            "client_order_id": str(audit["client_order_id"]),
            "asset_id": str(audit["asset_id"]),
            "recorded_event_type": str(row["event_type"]),
            "from_state": _text_or_none(row.get("from_state")),
            "to_state": _text_or_none(row.get("to_state")),
            "reason": _text_or_none(row.get("reason")),
            "checkpoint_id": _text_or_none(row.get("checkpoint_id")),
            "venue_regime_id": _bound_regime(audit)["regime_id"],
            "venue_regime_source_hash": _bound_regime(audit)["source_hash"],
            "recorded_payload": dict(row.get("payload") or {}),
        },
    )


def _audit_only_events(
    audit: Mapping[str, Any], aggregate_key: str
) -> tuple[SimEvent, ...]:
    """Retain an honest minimum episode when old rows predate lifecycle journaling."""
    common_payload = {
        "audit_key": str(audit["audit_key"]),
        "strategy_id": str(audit["strategy_id"]),
        "client_order_id": str(audit["client_order_id"]),
        "asset_id": str(audit["asset_id"]),
        "paper_status": str(audit["status"]),
        "paper_reason": str(audit["reason"]),
        "source": "paper_taker_order_audits_without_paper_order_events",
        "venue_regime_id": _bound_regime(audit)["regime_id"],
        "venue_regime_source_hash": _bound_regime(audit)["source_hash"],
    }
    decision = SimEvent.build(
        event_type="STRATEGY_INTENT",
        event_ts_ns=_timestamp_ns(audit["decision_ts"]),
        source_sequence=10,
        aggregate_key=aggregate_key,
        priority=int(EventPriority.STRATEGY_INTENT),
        source_event_id=f"audit:{audit['audit_key']}:decision",
        deterministic_tiebreaker=f"audit:{audit['audit_key']}:decision",
        model_version=MODEL_VERSION,
        payload=common_payload,
    )
    arrival = SimEvent.build(
        event_type="PAPER_COMMAND_ARRIVAL",
        event_ts_ns=_timestamp_ns(audit["arrival_ts"]),
        source_sequence=20,
        aggregate_key=aggregate_key,
        priority=int(EventPriority.PAPER_COMMAND_ARRIVAL),
        source_event_id=f"audit:{audit['audit_key']}:arrival",
        deterministic_tiebreaker=f"audit:{audit['audit_key']}:arrival",
        model_version=MODEL_VERSION,
        payload=common_payload,
    )
    recorded_at = max(_timestamp_ns(audit["created_at"]), arrival.event_ts_ns)
    result = SimEvent.build(
        event_type="ACCOUNTING_MARK",
        event_ts_ns=recorded_at,
        source_sequence=30,
        aggregate_key=aggregate_key,
        priority=int(EventPriority.ACCOUNTING),
        source_event_id=f"audit:{audit['audit_key']}:recorded-result",
        deterministic_tiebreaker=f"audit:{audit['audit_key']}:recorded-result",
        model_version=MODEL_VERSION,
        payload=common_payload,
    )
    return (decision, arrival, result)


def _fetch_audit(connection: Any, audit_key: str) -> Mapping[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT audit_key, strategy_id, client_order_id, asset_id,
                   decision_ts, arrival_ts, status, reason, created_at,
                   intent,fidelity
            FROM quant.paper_taker_order_audits
            WHERE audit_key = %s
            """,
            (str(audit_key),),
        )
        return cur.fetchone()


def _fetch_lifecycle_rows(connection: Any, audit_key: str) -> list[Mapping[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT e.idempotency_key, e.event_type, e.from_state, e.to_state,
                   e.reason, e.checkpoint_id, e.payload, e.event_ts
            FROM quant.paper_order_events AS e
            JOIN quant.paper_live_order_intents AS i ON i.intent_id = e.intent_id
            WHERE i.result_audit_key = %s
            ORDER BY e.event_ts, e.idempotency_key
            """,
            (str(audit_key),),
        )
        return list(cur.fetchall())


def _lifecycle_spec(row: Mapping[str, Any]) -> tuple[str, int, int]:
    name = str(row.get("event_type") or row.get("to_state") or "").upper()
    return _LIFECYCLE_EVENT_TYPES.get(
        name,
        ("ACCOUNTING_MARK", int(EventPriority.ACCOUNTING), 110),
    )


def _timestamp_ns(value: Any) -> int:
    if not isinstance(value, datetime):
        raise ValueError("paper lifecycle timestamp is required")
    timestamp = value.astimezone(timezone.utc)
    delta = timestamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        delta.days * 86_400_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _bound_regime(audit: Mapping[str, Any]) -> dict[str, str | None]:
    intent = audit.get("intent") if isinstance(audit.get("intent"), Mapping) else {}
    fidelity = (
        audit.get("fidelity") if isinstance(audit.get("fidelity"), Mapping) else {}
    )
    regime_id = str(
        intent.get("venue_regime_id") or fidelity.get("venue_regime_id") or "UNBOUND"
    )
    source_hash = intent.get("venue_regime_source_hash")
    return {
        "regime_id": regime_id,
        "source_hash": None if source_hash is None else str(source_hash),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    payload = export_paper_episode(args.audit_key, args.output)
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
