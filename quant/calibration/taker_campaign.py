"""Immutable, multi-run taker calibration campaign evaluation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping

from .calibration_domain import canonical_json
from .evaluate_holdout import DEFAULT_CONFIG, evaluate_probes
from .store import CalibrationStore


def evaluate_campaign(
    store: CalibrationStore,
    *,
    anchor_run_ids: Iterable[str],
    source_metadata: Mapping[str, Any] | None = None,
    config_path: Path = DEFAULT_CONFIG,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Evaluate all live probes for the one frozen model behind anchor runs.

    The anchors come from the independently generated mechanical-acceptance
    report. They only select a frozen model manifest; once selected, every
    live probe for that manifest is included so rejected or incomplete probes
    cannot disappear from the campaign evidence.
    """

    anchors = sorted({str(value) for value in anchor_run_ids if str(value)})
    if not anchors:
        raise ValueError("campaign requires at least one anchor run")
    runs = []
    for run_id in anchors:
        run = store.load_run(run_id)
        if run is None:
            raise ValueError(f"unknown campaign anchor run: {run_id}")
        if str(run.get("mode")) != "live":
            raise ValueError(f"campaign anchor is not a live run: {run_id}")
        runs.append(run)
    model_versions = {str(run.get("model_version") or "") for run in runs}
    manifest_ids = {str(run.get("manifest_id") or "") for run in runs}
    venue_regimes = {str(run.get("venue_regime_id") or "") for run in runs}
    if len(model_versions) != 1 or len(manifest_ids) != 1 or len(venue_regimes) != 1:
        raise ValueError("campaign anchors must share one model, manifest, and venue regime")
    model_version = model_versions.pop()
    manifest_id = manifest_ids.pop()
    venue_regime_id = venue_regimes.pop()
    rows = store.load_live_probes_for_model(
        model_version=model_version,
        manifest_id=manifest_id,
        venue_regime_id=venue_regime_id,
    )
    if not rows:
        raise RuntimeError("campaign model manifest has no live probes")
    source_run_ids = sorted({str(row.get("run_id") or "") for row in rows if row.get("run_id")})
    identity = {
        "model_version": model_version,
        "manifest_id": manifest_id,
        "venue_regime_id": venue_regime_id,
        "source_run_ids": source_run_ids,
        "probe_versions": [
            {
                "probe_id": str(row.get("probe_id") or ""),
                "updated_at": str(row.get("updated_at") or ""),
            }
            for row in rows
        ],
    }
    evaluation_id = "taker-campaign-" + hashlib.sha256(
        canonical_json(identity).encode("utf-8")
    ).hexdigest()[:24]
    source = {
        "kind": "model_manifest_campaign",
        "anchor_run_id": anchors[0],
        "anchor_run_ids": anchors,
        "source_run_ids": source_run_ids,
        "model_manifest_id": manifest_id,
        **dict(source_metadata or {}),
    }
    return evaluate_probes(
        rows,
        evaluation_id=evaluation_id,
        model_version=model_version,
        venue_regime_id=venue_regime_id,
        source=source,
        config_path=config_path,
    )


def verify_campaign_manifest(
    store: CalibrationStore,
    manifest: Mapping[str, Any],
    *,
    config_path: Path = DEFAULT_CONFIG,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Reload the exact stored probe set and reject any evidence drift."""

    if str(manifest.get("schema_version")) != "taker_calibration_dataset_manifest_v1":
        raise ValueError("unsupported taker campaign manifest")
    observed_hash = str(manifest.get("content_sha256") or "")
    immutable = {key: value for key, value in manifest.items() if key != "content_sha256"}
    expected_hash = hashlib.sha256(canonical_json(immutable).encode("utf-8")).hexdigest()
    if observed_hash != expected_hash:
        raise RuntimeError("campaign manifest content hash is invalid")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("campaign manifest has no entries")
    probe_ids = [str(row.get("probe_id") or "") for row in entries if isinstance(row, Mapping)]
    if len(set(probe_ids)) != len(probe_ids) or not all(probe_ids):
        raise ValueError("campaign manifest has invalid probe ids")
    rows = store.load_probes_by_ids(probe_ids)
    if {str(row.get("probe_id") or "") for row in rows} != set(probe_ids):
        raise RuntimeError("campaign source probes are no longer available")
    source = manifest.get("source")
    source = dict(source) if isinstance(source, Mapping) else {}
    source["source_run_ids"] = manifest.get("source_run_ids") or []
    payload, samples = evaluate_probes(
        rows,
        evaluation_id=str(manifest.get("evaluation_id") or ""),
        model_version=str(manifest.get("model_version") or ""),
        venue_regime_id=str(manifest.get("venue_regime_id") or ""),
        source=source,
        config_path=config_path,
    )
    actual_manifest = payload.get("dataset_manifest") or {}
    if str(actual_manifest.get("content_sha256") or "") != observed_hash:
        raise RuntimeError("campaign source evidence changed since the manifest was frozen")
    return payload, samples
