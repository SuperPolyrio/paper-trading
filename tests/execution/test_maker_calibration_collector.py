from __future__ import annotations

import json
from pathlib import Path

from quant.maker.calibration_collector import MakerCalibrationCollector


class _UserWs:
    def __init__(self):
        self.calls = []

    def record_orders(self, **kwargs):
        self.calls.append(kwargs)
        order_id = kwargs["order_ids"][0]
        kwargs["event_sink"](
            {
                "event_key": "event-1",
                "event_type": "ORDER",
                "payload": {
                    "event_type": "ORDER",
                    "id": order_id,
                    "status": "CANCELED",
                },
                "matched_order_ids": [order_id],
            }
        )
        return {
            "status": "ALL_TERMINAL",
            "event_count": 1,
            "terminal_order_ids": [order_id],
            "reconnects": 0,
            "credentials_persisted": False,
        }


class _Reconciler:
    def __init__(self):
        self.calls = []

    def reconcile(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "status": "CALIBRATABLE",
            "artifact_complete": True,
            "actual_outcome": "NO_FILL",
            "exchange_submit_called": False,
            "exact_cancel_called": False,
        }


def _checkpoint(path: Path, *, run_id: str, order_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "order_id": order_id,
                "asset_id": f"asset-{order_id}",
                "condition_id": f"condition-{order_id}",
                "submitted_at": "2026-08-26T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def test_collector_discovers_orders_and_never_mutates_them(tmp_path: Path) -> None:
    checkpoint_dir = tmp_path / "probes"
    checkpoint_dir.mkdir()
    _checkpoint(
        checkpoint_dir / "run-1.submission.json",
        run_id="run-1",
        order_id="order-1",
    )
    user_ws = _UserWs()
    reconciler = _Reconciler()
    collector = MakerCalibrationCollector(
        checkpoint_dir=checkpoint_dir,
        user_ws=user_ws,
        reconciler=reconciler,
        state_path=tmp_path / "status.json",
    )

    result = collector.run_cycle(watch_seconds=1)

    assert result["status"] == "PASS"
    assert result["pending_checkpoint_count"] == 1
    assert result["exchange_submit_called"] is False
    assert result["exact_cancel_called"] is False
    assert result["resubmit_forbidden"] is True
    assert reconciler.calls[0]["watch_seconds"] == 0
    assert reconciler.calls[0]["cancel_open"] is False
    journal = checkpoint_dir / "run-1.user-ws.jsonl"
    assert journal.exists()
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1


def test_collector_skips_completed_and_discovers_new_checkpoint(
    tmp_path: Path,
) -> None:
    checkpoint_dir = tmp_path / "probes"
    checkpoint_dir.mkdir()
    _checkpoint(
        checkpoint_dir / "run-1.submission.json",
        run_id="run-1",
        order_id="order-1",
    )
    (checkpoint_dir / "run-1.recovery.json").write_text(
        json.dumps({"status": "CALIBRATABLE", "artifact_complete": True}),
        encoding="utf-8",
    )
    user_ws = _UserWs()
    reconciler = _Reconciler()
    collector = MakerCalibrationCollector(
        checkpoint_dir=checkpoint_dir,
        user_ws=user_ws,
        reconciler=reconciler,
        state_path=tmp_path / "status.json",
    )

    first = collector.run_cycle(watch_seconds=1)
    assert first["pending_checkpoint_count"] == 0
    assert user_ws.calls == []
    _checkpoint(
        checkpoint_dir / "run-2.submission.json",
        run_id="run-2",
        order_id="order-2",
    )
    second = collector.run_cycle(watch_seconds=1)

    assert second["pending_checkpoint_count"] == 1
    assert user_ws.calls[-1]["order_ids"] == ["order-2"]
