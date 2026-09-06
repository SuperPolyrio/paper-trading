"""Long-running, no-submit collector for authenticated Maker order truth."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quant.calibration.user_ws_recorder import DurableUserWsEventJournal


class MakerCalibrationCollector:
    """Discover accepted checkpoints and reconcile them without order mutation."""

    def __init__(
        self,
        *,
        checkpoint_dir: Path | str,
        user_ws: Any,
        reconciler: Any,
        state_path: Path | str,
        identity_scope: str = "maker-calibration-account-v1",
    ) -> None:
        self.checkpoint_dir = Path(checkpoint_dir).resolve()
        self.user_ws = user_ws
        self.reconciler = reconciler
        self.state_path = Path(state_path).resolve()
        self.identity_scope = str(identity_scope)

    def run_cycle(
        self,
        *,
        watch_seconds: float = 20,
        max_reconnects: int = 5,
    ) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        checkpoints, discovery_errors = self._discover()
        pending = [row for row in checkpoints if not self._complete(row)]
        by_order = {str(row["order_id"]).lower(): row for row in pending}
        journals = {
            order_id: DurableUserWsEventJournal(self._journal_path(row))
            for order_id, row in by_order.items()
        }

        def persist_event(event: Mapping[str, Any]) -> None:
            for order_id in event.get("matched_order_ids") or ():
                normalized = str(order_id).lower()
                journal = journals.get(normalized)
                checkpoint = by_order.get(normalized)
                if journal is None or checkpoint is None:
                    continue
                journal.append(
                    {
                        **dict(event),
                        "probe_id": str(checkpoint["run_id"]),
                    }
                )

        ws_capture: dict[str, Any] = {
            "status": "NO_PENDING_ORDERS" if not pending else "NOT_REQUESTED",
            "event_count": 0,
            "terminal_order_ids": [],
            "reconnects": 0,
            "credentials_persisted": False,
        }
        errors = list(discovery_errors)
        if pending and watch_seconds > 0:
            try:
                ws_capture = self.user_ws.record_orders(
                    collector_id=f"maker-collector:{int(started_at.timestamp())}",
                    condition_ids=sorted(
                        {str(row["condition_id"]) for row in pending}
                    ),
                    order_ids=sorted(by_order),
                    timeout_seconds=watch_seconds,
                    event_sink=persist_event,
                    identity_scope=self.identity_scope,
                    max_reconnects=max_reconnects,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"user_ws_cycle:{exc.__class__.__name__}:{str(exc)[:500]}"
                )
                ws_capture = {
                    "status": "SOURCE_UNAVAILABLE",
                    "event_count": 0,
                    "terminal_order_ids": [],
                    "reconnects": max_reconnects,
                    "credentials_persisted": False,
                }

        reconciliations: list[dict[str, Any]] = []
        for checkpoint in pending:
            try:
                result = self.reconciler.reconcile(
                    checkpoint_path=checkpoint["path"],
                    journal_path=self._journal_path(checkpoint),
                    output_path=self._recovery_path(checkpoint),
                    watch_seconds=0,
                    cancel_open=False,
                    max_reconnects=max_reconnects,
                )
                reconciliations.append(
                    {
                        "run_id": checkpoint["run_id"],
                        "order_id": checkpoint["order_id"],
                        "status": result.get("status"),
                        "artifact_complete": bool(result.get("artifact_complete")),
                        "actual_outcome": result.get("actual_outcome"),
                        "exchange_submit_called": False,
                        "exact_cancel_called": False,
                        "output_path": str(self._recovery_path(checkpoint)),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"reconcile:{checkpoint['run_id']}:{exc.__class__.__name__}:"
                    f"{str(exc)[:500]}"
                )

        status = (
            "PASS"
            if not errors
            else "DEGRADED"
            if reconciliations or not pending
            else "SOURCE_UNAVAILABLE"
        )
        payload = {
            "schema_version": "maker_calibration_collector_status_v1",
            "status": status,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "checkpoint_dir": str(self.checkpoint_dir),
            "checkpoint_count": len(checkpoints),
            "pending_checkpoint_count": len(pending),
            "completed_checkpoint_count": len(checkpoints) - len(pending),
            "ws_capture": ws_capture,
            "reconciliations": reconciliations,
            "errors": errors,
            "exchange_submit_called": False,
            "exact_cancel_called": False,
            "resubmit_forbidden": True,
            "credentials_persisted": False,
        }
        _write_json(self.state_path, payload)
        return payload

    def run_forever(
        self,
        *,
        watch_seconds: float = 20,
        poll_seconds: float = 10,
        max_reconnects: int = 5,
    ) -> None:
        while True:
            self.run_cycle(
                watch_seconds=watch_seconds,
                max_reconnects=max_reconnects,
            )
            time.sleep(max(0.1, poll_seconds))

    def _discover(self) -> tuple[list[dict[str, Any]], list[str]]:
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        if not self.checkpoint_dir.exists():
            return rows, errors
        for path in sorted(self.checkpoint_dir.glob("*.submission.json")):
            try:
                payload = _read_json(path)
                rows.append(
                    {
                        **payload,
                        "run_id": _required(payload, "run_id"),
                        "order_id": _required(payload, "order_id"),
                        "condition_id": _required(payload, "condition_id"),
                        "path": path,
                        "checkpoint_sha256": hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"checkpoint:{path.name}:{exc.__class__.__name__}:"
                    f"{str(exc)[:300]}"
                )
        return rows, errors

    def _complete(self, checkpoint: Mapping[str, Any]) -> bool:
        recovery = _read_json(self._recovery_path(checkpoint))
        return bool(
            recovery.get("status") == "CALIBRATABLE"
            and recovery.get("artifact_complete")
        )

    @staticmethod
    def _journal_path(checkpoint: Mapping[str, Any]) -> Path:
        path = Path(checkpoint["path"])
        run_id = str(checkpoint["run_id"])
        return path.with_name(f"{run_id}.user-ws.jsonl")

    @staticmethod
    def _recovery_path(checkpoint: Mapping[str, Any]) -> Path:
        path = Path(checkpoint["path"])
        run_id = str(checkpoint["run_id"])
        return path.with_name(f"{run_id}.recovery.json")


def _required(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"checkpoint missing {key}")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON payload is not an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
