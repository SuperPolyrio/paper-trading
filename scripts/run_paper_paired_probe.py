#!/usr/bin/env python3
"""Run and reconcile Sprint 5 paired probes without submitting exchange orders."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.backtest.order_state import upsert_real_order_state_events
from quant.backtest.shadow_live_validation import (
    load_shadow_live_event_rows,
)
from quant.calibration.paired_probe_bridge import (
    sync_calibration_probe_to_paired,
)
from quant.calibration.probe_scheduler import (
    _paper_shadow_connection_factory,
)
from quant.calibration.store import CalibrationStore
from quant.paper.live_shadow_store import LiveShadowStore
from quant.paper.paired_probe import (
    NO_SUBMIT,
    RECORD_ONLY,
    OrderFilledEvidenceClient,
    attach_live_lifecycle,
    attach_orderfilled_evidence,
    build_paired_probe_report,
    report_to_json,
    run_paired_probe,
)

DEFAULT_OUTPUT = Path("runtime_outputs/paper_paired_probe/latest.json")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Create A and a safe B/C placeholder.")
    start.add_argument("--mode", choices=(NO_SUBMIT, RECORD_ONLY), default=NO_SUBMIT)
    start.add_argument("--strategy-id")
    start.add_argument("--side", choices=("BUY", "SELL"), default="BUY")
    start.add_argument("--wait-seconds", type=float, default=15.0)
    start.add_argument("--evidence-window-seconds", type=float, default=30.0)
    start.add_argument("--skip-orderfilled-query", action="store_true")
    start.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)

    attach = sub.add_parser("attach-live", help="Attach an externally recorded B lifecycle.")
    attach.add_argument("--probe-id", required=True)
    attach.add_argument("--input", required=True, type=Path)
    attach.add_argument("--source", default="micro-live-recorder")
    attach.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)

    reconcile = sub.add_parser("reconcile-orderfilled", help="Build delayed C evidence with ex-self filtering.")
    reconcile.add_argument("--probe-id", required=True)
    reconcile.add_argument("--window-seconds", type=float, default=120.0)
    reconcile.add_argument("--window-end")
    reconcile.add_argument("--own-address", action="append", default=[])
    reconcile.add_argument("--own-order-hash", action="append", default=[])
    reconcile.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)

    sync = sub.add_parser(
        "sync-calibration",
        help="Idempotently attach persisted live calibration lifecycles.",
    )
    sync.add_argument("--run-id")
    sync.add_argument("--limit", type=int, default=500)
    sync.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)

    pending = sub.add_parser(
        "reconcile-pending",
        help="Retry delayed OrderFilled evidence for record-only probes.",
    )
    pending.add_argument("--limit", type=int, default=20)
    pending.add_argument("--min-age-seconds", type=float, default=30.0)
    pending.add_argument("--window-seconds", type=float, default=300.0)
    pending.add_argument("--max-runtime-seconds", type=float, default=90.0)
    pending.add_argument("--write-retries", type=int, default=3)
    pending.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)

    report = sub.add_parser("report", help="Aggregate thresholds and required Sprint 5 buckets.")
    report.add_argument("--limit", type=int, default=500)
    report.add_argument(
        "--mode",
        choices=("all", NO_SUBMIT, RECORD_ONLY),
        default=RECORD_ONLY,
    )
    report.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _load_paper_control_env()
    store = LiveShadowStore(_paper_shadow_connection_factory(os.environ))
    calibration_store = CalibrationStore(store.connection_factory)
    if args.command == "start":
        probe = run_paired_probe(
            store=store,
            mode=args.mode,
            strategy_id=args.strategy_id,
            side=args.side,
            wait_seconds=max(0.1, args.wait_seconds),
            evidence_client=None if args.skip_orderfilled_query else OrderFilledEvidenceClient(),
            evidence_window_seconds=max(0.0, args.evidence_window_seconds),
        )
        payload = {
            "command": "start",
            "exchange_order_submitted": False,
            "probe": probe,
            "report": build_paired_probe_report([probe]),
        }
    elif args.command == "attach-live":
        probe = _required_probe(store, args.probe_id)
        events = load_shadow_live_event_rows(args.input)
        updated = attach_live_lifecycle(probe, events, source=args.source)
        lifecycle_events = list(updated["live_lifecycle"].get("events") or [])
        with store.connection_factory(readonly=False) as conn:
            events_written = upsert_real_order_state_events(conn, lifecycle_events)
        persisted = store.upsert_paired_probe(updated)
        payload = {
            "command": "attach-live",
            "exchange_order_submitted": False,
            "real_order_state_events_written": events_written,
            "probe": persisted,
            "report": build_paired_probe_report([persisted]),
        }
    elif args.command == "reconcile-orderfilled":
        probe = _required_probe(store, args.probe_id)
        decision_ts = _parse_datetime(probe["decision_ts"])
        window_end = _parse_datetime(args.window_end) if args.window_end else datetime.now(timezone.utc)
        max_end = decision_ts + timedelta(seconds=max(0.0, args.window_seconds))
        window_end = min(window_end, max_end)
        evidence_client = OrderFilledEvidenceClient()
        source_watermark = evidence_client.coverage_watermark().get("block_time")
        rows = evidence_client.fetch_window(
            asset_id=str(probe["asset_id"]),
            start=decision_ts,
            end=window_end,
        )
        expected_transactions = list(
            ((probe.get("audit") or {}).get("external_live") or {}).get(
                "transaction_hashes"
            ) or []
        )
        rows.extend(evidence_client.fetch_transactions(expected_transactions))
        updated = attach_orderfilled_evidence(
            probe,
            rows,
            own_addresses=args.own_address,
            own_order_hashes=args.own_order_hash,
            window_end=window_end,
            source_watermark=source_watermark,
            expected_transaction_hashes=expected_transactions,
        )
        persisted = store.upsert_paired_probe(updated)
        payload = {
            "command": "reconcile-orderfilled",
            "exchange_order_submitted": False,
            "probe": persisted,
            "report": build_paired_probe_report([persisted]),
        }
    elif args.command == "sync-calibration":
        rows = (
            calibration_store.load_probes(args.run_id)
            if args.run_id
            else calibration_store.load_linked_live_probes(
                limit=max(1, args.limit)
            )
        )
        eligible = [
            row
            for row in rows
            if row.get("paired_probe_id")
            and bool(row.get("exchange_submit_called"))
        ]
        synchronized = []
        retry_required = []
        for row in eligible:
            try:
                synchronized.append(
                    sync_calibration_probe_to_paired(row, store=store)
                )
            except Exception as exc:  # noqa: BLE001 - isolate one evidence row
                retry_required.append(
                    {
                        "probe_id": row.get("probe_id"),
                        "paired_probe_id": row.get("paired_probe_id"),
                        "error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
                    }
                )
        payload = {
            "command": "sync-calibration",
            "exchange_order_submitted": False,
            "status": "PASS" if not retry_required else "RETRY_REQUIRED",
            "eligible_count": len(eligible),
            "synchronized_count": len(synchronized),
            "retry_required": retry_required,
            "report": build_paired_probe_report(synchronized),
        }
    elif args.command == "reconcile-pending":
        now = datetime.now(timezone.utc)
        deadline = time.monotonic() + max(1.0, args.max_runtime_seconds)
        probes = store.load_paired_probes(
            limit=max(1, args.limit),
            mode=RECORD_ONLY,
            status="PENDING_ORDERFILLED_EX_SELF",
        )
        client = OrderFilledEvidenceClient()
        source_coverage = client.coverage_watermark()
        source_watermark = source_coverage.get("block_time")
        reconciled = []
        retry_required = []
        skipped_too_young = 0
        deferred_by_budget = 0
        deferred_source_lag = 0
        for probe in probes:
            if time.monotonic() >= deadline:
                deferred_by_budget += 1
                continue
            decision_ts = _parse_datetime(probe["decision_ts"])
            if (now - decision_ts).total_seconds() < max(
                0.0, args.min_age_seconds
            ):
                skipped_too_young += 1
                continue
            max_end = decision_ts + timedelta(
                seconds=max(0.0, args.window_seconds)
            )
            window_end = min(now, max_end)
            external = dict(
                (probe.get("audit") or {}).get("external_live") or {}
            )
            expected_transactions = list(
                external.get("transaction_hashes") or []
            )
            current_evidence = dict(probe.get("orderfilled_ex_self") or {})
            if (
                source_watermark is not None
                and source_watermark < window_end
                and bool(current_evidence.get("transaction_confirmation_complete"))
            ):
                deferred_source_lag += 1
                continue
            try:
                rows = client.fetch_window(
                    asset_id=str(probe["asset_id"]),
                    start=decision_ts,
                    end=window_end,
                )
                rows.extend(client.fetch_transactions(expected_transactions))
                updated = attach_orderfilled_evidence(
                    probe,
                    rows,
                    own_addresses=external.get("own_addresses") or [],
                    own_order_hashes=external.get("own_order_hashes") or [],
                    window_end=window_end,
                    source_watermark=source_watermark,
                    expected_transaction_hashes=expected_transactions,
                )
                last_error = None
                for attempt in range(max(1, args.write_retries)):
                    try:
                        reconciled.append(store.upsert_paired_probe(updated))
                        last_error = None
                        break
                    except Exception as exc:  # noqa: BLE001 - retry DB boundary
                        last_error = exc
                        if attempt + 1 < max(1, args.write_retries):
                            time.sleep(min(5.0, 1.5 * (attempt + 1)))
                if last_error is not None:
                    raise last_error
            except Exception as exc:  # noqa: BLE001 - preserve batch progress
                retry_required.append(
                    {
                        "probe_id": probe.get("probe_id"),
                        "error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
                    }
                )
        payload = {
            "command": "reconcile-pending",
            "exchange_order_submitted": False,
            "status": "PASS" if not retry_required else "RETRY_REQUIRED",
            "pending_count": len(probes),
            "reconciled_count": len(reconciled),
            "skipped_too_young": skipped_too_young,
            "deferred_by_budget": deferred_by_budget,
            "deferred_source_lag": deferred_source_lag,
            "orderfilled_source_coverage": source_coverage,
            "retry_required": retry_required,
            "report": build_paired_probe_report(reconciled),
        }
    else:
        probes = store.load_paired_probes(
            limit=max(1, args.limit),
            mode=None if args.mode == "all" else args.mode,
        )
        payload = {
            "command": "report",
            "exchange_order_submitted": False,
            "report": build_paired_probe_report(probes),
        }
    _write_json(args.json_out, payload)
    print(report_to_json(payload), end="")
    status = str((payload.get("report") or {}).get("status") or "PENDING")
    return 2 if status == "FAIL" else 0


def _load_paper_control_env() -> None:
    path = Path(
        os.environ.get(
            "POLY_QUANT_PAPER_DB_CONTROL_ENV",
            "~/.config/prediction-market-quant/paper-db-control.env",
        )
    ).expanduser()
    if path.is_file():
        load_dotenv(path, override=False)


def _required_probe(store: LiveShadowStore, probe_id: str) -> dict[str, Any]:
    probe = store.load_paired_probe(probe_id)
    if probe is None:
        raise ValueError(f"unknown paired probe: {probe_id}")
    return probe


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report_to_json(payload), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
