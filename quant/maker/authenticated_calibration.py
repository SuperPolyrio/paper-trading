"""Build fail-closed Maker calibration rows from authenticated own-order evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from quant.calibration.calibration_domain import payload_hash
from quant.maker.evaluate_holdout import evaluate, write_report
from quant.maker.live_probe_runner import maker_holdout_row, maker_outcome_observation
from quant.simulator.live_maker_evidence import validate_live_maker_probe

AUTHORITY = "AUTHENTICATED_OWN_ORDER"
MODEL_NAME = "PROBABILISTIC_QUEUE"


def build_authenticated_calibration(
    *,
    evidence_roots: Iterable[Path | str],
    output_dir: Path | str,
    config_path: Path | str,
    model_name: str = MODEL_NAME,
    canonical_report_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Validate immutable LIVE evidence and produce a promotion-safe report."""

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    rejections: list[dict[str, str]] = []
    seen_orders: dict[str, str] = {}
    for probe_path in discover_probe_paths(evidence_roots):
        try:
            row = authenticated_holdout_row(probe_path, model_name=model_name)
            order_id = str(row["order_id"]).lower()
            stable_row = dict(row)
            stable_row.pop("source_bundle", None)
            stable_row.pop("source_manifest_sha256", None)
            row_hash = payload_hash(stable_row, prefix="maker-auth-row-")
            prior_hash = seen_orders.get(order_id)
            if prior_hash is not None:
                if prior_hash != row_hash:
                    raise ValueError("conflicting duplicate official order evidence")
                continue
            seen_orders[order_id] = row_hash
            row["row_hash"] = row_hash
            rows.append(row)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            rejections.append(
                {
                    "source": str(probe_path),
                    "reason": f"{exc.__class__.__name__}:{str(exc)[:500]}",
                }
            )

    rows.sort(key=lambda row: (str(row.get("started_at") or ""), str(row["order_id"])))
    dataset_path = output / "authenticated_holdout.jsonl"
    dataset_path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    rejection_path = output / "rejections.json"
    _write_json(
        rejection_path,
        {
            "schema_version": "maker_authenticated_rejections_v1",
            "count": len(rejections),
            "rows": rejections,
        },
    )
    manifest = {
        "schema_version": "maker_authenticated_calibration_manifest_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "authority": AUTHORITY,
        "model_name": model_name,
        "accepted_count": len(rows),
        "rejected_count": len(rejections),
        "unique_order_count": len(seen_orders),
        "dataset_sha256": _sha256(dataset_path),
        "rejections_sha256": _sha256(rejection_path),
        "outcome_counts": _counts(rows, "actual_outcome"),
        "market_count": len({str(row.get("market_id") or "") for row in rows}),
        "event_count": len({str(row.get("event_id") or "") for row in rows}),
        "utc_day_count": len({str(row.get("utc_day") or "") for row in rows}),
        "claims": {
            "authenticated_own_order_truth": bool(rows),
            "timeout_is_no_fill": False,
            "public_l2_proves_fifo": False,
            "maker_live_calibrated": False,
        },
    }
    manifest_path = output / "manifest.json"
    _write_json(manifest_path, manifest)

    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(config, Mapping):
        raise TypeError("Maker calibration config must be a YAML object")
    report = evaluate(rows, config=config)
    report["authenticated_dataset"] = {
        "path": str(dataset_path),
        "sha256": manifest["dataset_sha256"],
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "rejected_count": len(rejections),
    }
    report["calibration_conclusion"] = (
        "LIVE_MAKER_CALIBRATED"
        if report["status"] == "PASS"
        else "AUTHENTICATED_EVIDENCE_INSUFFICIENT"
    )
    write_report(output / "evaluation", report)
    if canonical_report_dir is not None:
        write_report(Path(canonical_report_dir).resolve(), report)
    return {
        "schema_version": "maker_authenticated_calibration_run_v1",
        "status": report["status"],
        "calibration_conclusion": report["calibration_conclusion"],
        "accepted_count": len(rows),
        "rejected_count": len(rejections),
        "outcome_counts": manifest["outcome_counts"],
        "dataset_path": str(dataset_path),
        "manifest_path": str(manifest_path),
        "evaluation_path": str(output / "evaluation" / "evaluation.json"),
        "canonical_evaluation_path": (
            str(Path(canonical_report_dir).resolve() / "evaluation.json")
            if canonical_report_dir is not None
            else None
        ),
    }


def discover_probe_paths(roots: Iterable[Path | str]) -> list[Path]:
    """Find canonical probe files without treating fixtures as LIVE evidence."""

    found: set[Path] = set()
    for value in roots:
        root = Path(value).resolve()
        if root.is_file():
            found.add(root)
            continue
        if not root.exists():
            continue
        found.update(path.resolve() for path in root.rglob("official/probe.json"))
    return sorted(found)


def authenticated_holdout_row(
    probe_path: Path | str,
    *,
    model_name: str = MODEL_NAME,
) -> dict[str, Any]:
    """Convert one immutable evidence bundle into an authenticated holdout row."""

    source = Path(probe_path).resolve()
    bundle_root = _bundle_root(source)
    verified_files = _verify_bundle(bundle_root, source)
    probe = _read_json(source)
    outcome = validate_live_maker_probe(probe)
    order_id = str(probe.get("order_id") or "").lower()
    _require(_valid_order_id(order_id), "official order ID is invalid")
    _require_order_identity(probe, order_id)

    predictions = _mapping(probe.get("model_predictions"))
    prediction = _mapping(predictions.get(model_name))
    _require(bool(prediction), f"prediction model is missing: {model_name}")
    calibration = _mapping(probe.get("maker_probability_calibration"))
    forecast = _mapping(probe.get("maker_trade_forecast"))
    prediction_proof = _prediction_proof(probe, predictions)
    row = maker_holdout_row(probe)
    truth = _mapping(probe.get("truth"))
    candidate = _mapping(probe.get("candidate"))
    rest = _mapping(probe.get("rest_reconciliation"))
    rest_order = _mapping(rest.get("order"))
    capture = _mapping(probe.get("user_ws_capture"))
    statuses = sorted(_user_ws_statuses(capture, order_id))
    onchain = _mapping(probe.get("onchain_orderfilled"))
    observation = _mapping(row.get("outcome_observation"))
    if not observation:
        observation = maker_outcome_observation(
            outcome=outcome,
            matched_size=Decimal(str(truth.get("actual_matched_size") or 0)),
            requested_size=Decimal(str(_mapping(probe.get("intent")).get("size") or 0)),
            resting_seconds=Decimal(str(probe.get("resting_seconds") or 0)),
            capture=capture,
        )
    row.update(
        {
            "schema_version": "maker_authenticated_holdout_row_v1",
            "evidence_authority": AUTHORITY,
            "evidence_validated": True,
            "artifact_complete": True,
            "actual_outcome": outcome,
            "model_name": model_name,
            "prediction_model_version": calibration.get("model_version"),
            "prediction_artifact_hash": calibration.get("artifact_hash"),
            "prediction_domain_status": prediction.get("domain_status"),
            "prediction_input_ready": forecast.get("status") == "READY",
            "p_no_fill": prediction.get("p_no_fill"),
            "p_partial": prediction.get("p_partial"),
            "p_full": prediction.get("p_full"),
            "expected_filled_size": prediction.get("expected_filled_size"),
            "expected_time_to_first_fill_seconds": prediction.get(
                "expected_time_to_first_fill_seconds"
            ),
            "expected_time_to_full_fill_seconds": prediction.get(
                "expected_time_to_full_fill_seconds"
            ),
            "category": candidate.get("source_category") or candidate.get("category"),
            "condition_id": candidate.get("condition_id"),
            "side": _mapping(probe.get("intent")).get("side"),
            "limit_price": _mapping(probe.get("intent")).get("limit_price"),
            "resting_seconds": probe.get("resting_seconds"),
            "official_order_status": rest_order.get("status"),
            "official_user_ws_statuses": statuses,
            "official_trade_count": int(truth.get("matched_trade_count") or 0),
            "official_truth_status": (
                "TERMINAL_NO_FILL" if outcome == "NO_FILL" else "CONFIRMED_ONCHAIN"
            ),
            "official_orderfilled_status": onchain.get("status"),
            "official_orderfilled_size": onchain.get("matched_size"),
            "official_transaction_coverage_complete": onchain.get(
                "transaction_coverage_complete"
            ),
            "official_receipt_status": _mapping(onchain.get("receipt_truth")).get(
                "status"
            ),
            "official_receipt_count": len(
                _mapping(onchain.get("receipt_truth")).get("receipts") or ()
            ),
            "truth_reconciled_at": truth.get("reconciled_at"),
            "prediction_frozen_at": prediction_proof["frozen_at"],
            "prediction_snapshot_hash": prediction_proof["hash"],
            "prediction_ordering_proof": prediction_proof["ordering_proof"],
            "prediction_precedes_submission": True,
            "source_probe_sha256": _sha256(source),
            "source_manifest_sha256": _sha256(bundle_root / "manifest.json"),
            "source_evidence_hashes": verified_files,
            "source_bundle": str(bundle_root),
            "cohort_role": "PROSPECTIVE_HOLDOUT",
            "outcome_observation": dict(observation),
        }
    )
    return row


def _verify_bundle(bundle_root: Path, probe_path: Path) -> dict[str, str]:
    manifest = _read_json(bundle_root / "manifest.json")
    _require(manifest.get("status") == "PASS", "evidence manifest is not PASS")
    _require(manifest.get("operation_type") == "MAKER", "evidence is not Maker")
    files = _mapping(manifest.get("files"))
    relative_probe = str(probe_path.relative_to(bundle_root))
    _require(relative_probe in files, "probe is absent from immutable manifest")
    verified: dict[str, str] = {}
    for relative, expected in sorted(files.items()):
        path = (bundle_root / str(relative)).resolve()
        _require(
            path.is_relative_to(bundle_root), "manifest path escapes evidence root"
        )
        _require(path.is_file(), f"manifest file is missing: {relative}")
        actual = _sha256(path)
        _require(actual == str(expected), f"manifest SHA256 mismatch: {relative}")
        verified[str(relative)] = actual

    real = _read_json(bundle_root / "real.json")
    _require(real.get("evidence_mode") == "LIVE", "real evidence is not LIVE")
    source_files = _mapping(_mapping(real.get("source_manifest")).get("evidence_files"))
    _require(bool(source_files), "real evidence has no source file hashes")
    for relative, expected in source_files.items():
        _require(
            verified.get(str(relative)) == str(expected),
            f"real source hash mismatch: {relative}",
        )
    return {key: verified[key] for key in sorted(source_files)}


def _prediction_proof(
    probe: Mapping[str, Any],
    predictions: Mapping[str, Any],
) -> dict[str, str]:
    snapshot = _mapping(probe.get("prediction_snapshot"))
    submitted_at = _mapping(probe.get("user_ws_capture")).get("submitted_at")
    truth_at = _mapping(probe.get("truth")).get("reconciled_at") or probe.get(
        "completed_at"
    )
    if snapshot:
        frozen_at = snapshot.get("frozen_at")
        expected = str(probe.get("prediction_snapshot_hash") or "")
        actual = payload_hash(snapshot, prefix="maker-prediction-")
        _require(
            bool(expected) and expected == actual, "prediction snapshot hash mismatch"
        )
        _require(
            _mapping(snapshot.get("model_predictions")) == predictions,
            "prediction snapshot differs from evaluated predictions",
        )
        ordering_proof = "HASH_FROZEN_BEFORE_SUBMIT"
        snapshot_hash = actual
    else:
        _require(
            probe.get("schema_version") == "maker_post_only_live_probe_v1",
            "legacy prediction ordering is not recognized",
        )
        frozen_at = probe.get("started_at")
        snapshot_hash = payload_hash(predictions, prefix="maker-legacy-prediction-")
        ordering_proof = "LEGACY_RUNNER_CONTROL_FLOW"
    _require(
        _before(frozen_at, submitted_at), "prediction was not frozen before submit"
    )
    _require(
        _before(submitted_at, truth_at, allow_equal=True), "truth predates submission"
    )
    return {
        "frozen_at": str(frozen_at),
        "hash": snapshot_hash,
        "ordering_proof": ordering_proof,
    }


def _require_order_identity(probe: Mapping[str, Any], order_id: str) -> None:
    submission = _mapping(probe.get("submission"))
    submitted_id = str(
        submission.get("orderID") or submission.get("order_id") or ""
    ).lower()
    _require(submitted_id == order_id, "submission order ID mismatch")
    capture_id = str(
        _mapping(probe.get("user_ws_capture")).get("order_id") or ""
    ).lower()
    _require(capture_id == order_id, "User WS order ID mismatch")
    rest_order = _mapping(_mapping(probe.get("rest_reconciliation")).get("order"))
    _require(
        str(rest_order.get("id") or "").lower() == order_id, "REST order ID mismatch"
    )
    truth = _mapping(probe.get("truth"))
    _require(
        str(truth.get("order_id") or "").lower() == order_id,
        "truth order ID mismatch",
    )
    actual_size = Decimal(str(truth.get("actual_matched_size") or 0))
    rest_size = Decimal(str(rest_order.get("size_matched") or 0))
    _require(actual_size == rest_size, "REST and reconciled matched size differ")
    intent_size = Decimal(str(_mapping(probe.get("intent")).get("size") or 0))
    if rest_order.get("original_size") not in (None, ""):
        _require(
            Decimal(str(rest_order["original_size"])) == intent_size,
            "REST and submitted order size differ",
        )
    if actual_size > 0:
        side = str(_mapping(probe.get("intent")).get("side") or "").upper()
        quote = Decimal(str(truth.get("actual_quote_amount") or 0))
        fee = Decimal(str(truth.get("actual_fee") or 0))
        delta = _mapping(probe.get("account_delta"))
        collateral_delta = Decimal(str(delta.get("collateral") or 0))
        conditional_delta = Decimal(str(delta.get("conditional") or 0))
        expected_cash = -quote - fee if side == "BUY" else quote - fee
        expected_tokens = actual_size if side == "BUY" else -actual_size
        tolerance = Decimal("0.000001")
        _require(
            abs(collateral_delta - expected_cash) <= tolerance,
            "official account cash delta differs from Maker fills",
        )
        _require(
            abs(conditional_delta - expected_tokens) <= tolerance,
            "official token delta differs from Maker fills",
        )
    statuses = _user_ws_statuses(_mapping(probe.get("user_ws_capture")), order_id)
    _require("LIVE" in statuses, "User WS lacks venue acceptance evidence")


def _user_ws_statuses(capture: Mapping[str, Any], order_id: str) -> set[str]:
    return {
        str(_mapping(row.get("payload")).get("status") or "").upper()
        for row in capture.get("events") or ()
        if isinstance(row, Mapping)
        and str(_mapping(row.get("payload")).get("id") or "").lower() == order_id
    }


def _bundle_root(probe_path: Path) -> Path:
    _require(probe_path.name == "probe.json", "canonical official/probe.json required")
    _require(probe_path.parent.name == "official", "probe is outside official evidence")
    root = probe_path.parent.parent.resolve()
    _require((root / "manifest.json").is_file(), "immutable evidence manifest missing")
    return root


def _before(left: Any, right: Any, *, allow_equal: bool = False) -> bool:
    if left in (None, "") or right in (None, ""):
        return False
    first = datetime.fromisoformat(str(left).replace("Z", "+00:00"))
    second = datetime.fromisoformat(str(right).replace("Z", "+00:00"))
    return first <= second if allow_equal else first < second


def _valid_order_id(value: str) -> bool:
    if len(value) != 66 or not value.startswith("0x"):
        return False
    try:
        int(value, 0)
    except ValueError:
        return False
    return True


def _counts(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "UNKNOWN")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), f"JSON object required: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
