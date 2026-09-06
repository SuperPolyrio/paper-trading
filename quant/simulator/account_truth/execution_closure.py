"""Bind live execution fidelity to a non-zero official account delta."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant.simulator.operation_fidelity import verify_operation_evidence_bundle


def verify_account_truth_execution_closure(
    evidence_directory: Path | str,
) -> dict[str, Any]:
    """Verify one immutable LIVE operation bundle as a two-gate closure."""

    root = Path(evidence_directory).resolve()
    manifest = _read_json(root / "manifest.json")
    reconciliation = _read_json(root / "reconciliation.json")
    account_truth = _read_json(root / "official/account-truth-summary.json")
    trade_record = _read_json(root / "official/live-vs-paper-trade-record.json")
    account_items = _read_jsonl(root / "official/account-truth-items.jsonl")
    bundle_verification = verify_operation_evidence_bundle(root)

    manifest_files = _mapping(manifest.get("files"))
    truth_file_hash = str(
        manifest_files.get("official/account-truth-summary.json") or ""
    )
    source_verification = _mapping(reconciliation.get("source_file_verification"))
    verified_hashes = {
        str(item).lower()
        for item in source_verification.get("verified_sha256", ())
    }
    truth_link = _mapping(trade_record.get("account_truth"))
    summary = _mapping(account_truth.get("summary"))
    strategy_ids = tuple(str(item) for item in account_truth.get("strategy_ids") or ())
    run_id = str(trade_record.get("run_id") or "")
    scenario_id = str(manifest.get("scenario_id") or "")

    item_pairs_match = bool(account_items) and all(
        str(row.get("status") or "") == "MATCH"
        and _within_tolerance(
            row.get("official_value"),
            row.get("paper_value"),
            row.get("tolerance", "0"),
        )
        for row in account_items
    )
    nonzero_items = [
        row
        for row in account_items
        if _nonzero(row.get("official_value"))
        and _nonzero(row.get("paper_value"))
    ]
    official_cash_delta = summary.get("official_cash_delta")
    paper_cash_delta = summary.get("paper_cash_delta")
    nonzero_account_delta = bool(nonzero_items) and (
        _nonzero(official_cash_delta)
        or _nonzero(paper_cash_delta)
        or any(str(row.get("field_name") or "") == "size" for row in nonzero_items)
    )

    baseline_as_of = _datetime(summary.get("baseline_as_of"))
    trade_at = _datetime(_mapping(trade_record.get("trade_time")).get("utc"))
    truth_as_of = _datetime(account_truth.get("as_of"))

    checks = {
        "operation_manifest_sha256_pass": bundle_verification.get("status") == "PASS",
        "operation_manifest_status_pass": manifest.get("status") == "PASS",
        "operation_is_live_pass": reconciliation.get("status") == "PASS"
        and reconciliation.get("evidence_mode") == "LIVE",
        "execution_external_truth_pass": reconciliation.get("external_truth_gate")
        == "PASS",
        "execution_source_files_verified": source_verification.get("status") == "PASS",
        "account_truth_file_bound_to_manifest": bool(truth_file_hash)
        and truth_file_hash == _sha256(root / "official/account-truth-summary.json"),
        "account_truth_file_bound_to_trade_record": bool(truth_file_hash)
        and truth_link.get("content_sha256") == truth_file_hash,
        "account_truth_hash_in_execution_sources": bool(truth_file_hash)
        and truth_file_hash.lower() in verified_hashes,
        "account_truth_pass": account_truth.get("account_truth_gate") == "PASS",
        "account_truth_source_pass": account_truth.get("official_source_status")
        == "PASS",
        "calibration_delta_scope": account_truth.get("comparison_scope")
        == "CALIBRATION_DELTA",
        "account_truth_has_no_material_mismatch": int(
            summary.get("material_mismatch_count") or 0
        )
        == 0
        and int(summary.get("mismatch_count") or 0) == 0,
        "account_delta_items_match": item_pairs_match,
        "transaction_bearing_nonzero_delta": nonzero_account_delta,
        "official_and_paper_cash_delta_match": _within_tolerance(
            official_cash_delta,
            paper_cash_delta,
            "0.00001",
        ),
        "execution_run_bound_to_paper_strategy": bool(run_id)
        and any(run_id in strategy_id for strategy_id in strategy_ids),
        "execution_run_bound_to_scenario": bool(run_id) and run_id in scenario_id,
        "baseline_precedes_trade": bool(
            baseline_as_of and trade_at and baseline_as_of <= trade_at
        ),
        "official_truth_follows_trade": bool(
            trade_at and truth_as_of and trade_at <= truth_as_of
        ),
        "paper_finality_is_confirmed": int(
            summary.get("paper_provisional_fill_count") or 0
        )
        == 0,
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    evidence = {
        "evidence_directory": str(root),
        "scenario_id": scenario_id,
        "operation_type": manifest.get("operation_type"),
        "run_id": run_id,
        "strategy_ids": list(strategy_ids),
        "baseline_id": summary.get("baseline_id"),
        "baseline_as_of": summary.get("baseline_as_of"),
        "trade_at": _mapping(trade_record.get("trade_time")).get("utc"),
        "account_truth_as_of": account_truth.get("as_of"),
        "official_cash_delta": official_cash_delta,
        "paper_cash_delta": paper_cash_delta,
        "nonzero_item_count": len(nonzero_items),
        "account_truth_reconciliation_id": account_truth.get("reconciliation_id"),
        "operation_reconciliation_sha256": manifest_files.get("reconciliation.json"),
        "account_truth_summary_sha256": truth_file_hash,
        "manifest_sha256": _sha256(root / "manifest.json"),
    }
    stable = {
        "schema_version": "account-truth-execution-closure-v1",
        "status": status,
        "checks": checks,
        "evidence": evidence,
        "bundle_mismatches": bundle_verification.get("mismatches") or [],
    }
    return {
        **stable,
        "content_sha256": hashlib.sha256(
            json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "live_submission_performed": False,
    }


def write_account_truth_execution_closure(
    output_path: Path | str,
    report: Mapping[str, Any],
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path.resolve()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError(f"expected JSON object row: {path}")
        rows.append(value)
    return rows


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _nonzero(value: Any) -> bool:
    number = _decimal(value)
    return number is not None and number != 0


def _within_tolerance(left: Any, right: Any, tolerance: Any) -> bool:
    left_number = _decimal(left)
    right_number = _decimal(right)
    tolerance_number = _decimal(tolerance)
    return bool(
        left_number is not None
        and right_number is not None
        and tolerance_number is not None
        and abs(left_number - right_number) <= abs(tolerance_number)
    )


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
