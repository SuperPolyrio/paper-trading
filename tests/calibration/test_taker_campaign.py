import csv
from datetime import datetime, timezone

import pytest

from quant.calibration.evaluate_holdout import evaluate_probes, write_artifacts
from quant.calibration.taker_campaign import verify_campaign_manifest


def _probe(
    probe_id: str, *, condition: str, day: int, state: str = "CALIBRATABLE"
) -> dict:
    return {
        "probe_id": probe_id,
        "run_id": f"run-{probe_id}",
        "model_version": "model-v1",
        "manifest_id": "manifest-v1",
        "venue_regime_id": "venue-v1",
        "probe_state": state,
        "exchange_submit_called": True,
        "condition_id": condition,
        "market_id": f"market-{condition}",
        "decision_ts": datetime(2026, 8, day, 12, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, day, 13, tzinfo=timezone.utc),
        "artifact_bitmap": {"complete": state == "CALIBRATABLE"},
        "market_snapshot": {"category": "politics"},
        "reconciliation": {
            "predicted_class": "FULL",
            "actual_class": "FULL",
            "order_type": "FOK",
            "price_error_ticks": "0",
            "filled_size_relative_error": "0",
            "fee_error": "0",
        },
        "timestamps": {},
    }


def _evaluate(rows: list[dict]):
    return evaluate_probes(
        rows,
        evaluation_id="campaign-test",
        model_version="model-v1",
        venue_regime_id="venue-v1",
        source={
            "kind": "model_manifest_campaign",
            "anchor_run_id": "run-a",
            "source_run_ids": [row["run_id"] for row in rows],
            "model_manifest_id": "manifest-v1",
        },
    )


def test_campaign_manifest_records_every_probe_and_group_split() -> None:
    rows = [
        _probe("a", condition="same-condition", day=1),
        _probe("b", condition="same-condition", day=1),
        _probe("c", condition="other-condition", day=2, state="DATA_INCOMPLETE"),
    ]

    payload, _ = _evaluate(rows)
    manifest = payload["dataset_manifest"]
    entries = {row["probe_id"]: row for row in manifest["entries"]}

    assert len(entries) == 3
    assert entries["a"]["split"] == entries["b"]["split"]
    assert entries["a"]["group_key"] == entries["b"]["group_key"]
    assert entries["c"]["split"] == "excluded"
    assert len(manifest["content_sha256"]) == 64


def test_holdout_group_artifact_keeps_event_and_day(tmp_path) -> None:
    rows = [
        _probe(f"p-{day}", condition=f"condition-{day}", day=day)
        for day in range(1, 21)
    ]
    payload, samples = _evaluate(rows)

    write_artifacts(tmp_path, payload, samples)

    with (tmp_path / "holdout_groups.csv").open(encoding="utf-8") as handle:
        groups = list(csv.DictReader(handle))
    assert groups
    assert all("UNKNOWN_DAY" not in row["group_key"] for row in groups)
    assert all(row["group_key"].startswith("condition-") for row in groups)


def test_campaign_manifest_verification_reloads_exact_evidence() -> None:
    rows = [_probe("a", condition="condition-a", day=1)]
    payload, _ = _evaluate(rows)

    class Store:
        def load_probes_by_ids(self, probe_ids):
            assert probe_ids == ["a"]
            return rows

    refreshed, _ = verify_campaign_manifest(Store(), payload["dataset_manifest"])

    assert (
        refreshed["dataset_manifest"]["content_sha256"]
        == payload["dataset_manifest"]["content_sha256"]
    )


def test_campaign_manifest_verification_rejects_changed_source_evidence() -> None:
    rows = [_probe("a", condition="condition-a", day=1)]
    payload, _ = _evaluate(rows)
    changed = _probe("a", condition="condition-a", day=1)
    changed["reconciliation"]["actual_class"] = "NO_FILL"

    class Store:
        def load_probes_by_ids(self, _probe_ids):
            return [changed]

    with pytest.raises(RuntimeError, match="changed since the manifest was frozen"):
        verify_campaign_manifest(Store(), payload["dataset_manifest"])
