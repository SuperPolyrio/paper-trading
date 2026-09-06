from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quant.simulator.account_truth.execution_closure import (
    verify_account_truth_execution_closure,
)


def test_live_nonzero_account_delta_closes_both_gates(tmp_path: Path) -> None:
    root = _bundle(tmp_path)

    report = verify_account_truth_execution_closure(root)

    assert report["status"] == "PASS"
    assert report["checks"]["transaction_bearing_nonzero_delta"] is True
    assert report["checks"]["execution_run_bound_to_paper_strategy"] is True
    assert report["live_submission_performed"] is False


def test_zero_delta_cannot_close_account_truth(tmp_path: Path) -> None:
    root = _bundle(tmp_path, cash_delta="0", size_delta="0")

    report = verify_account_truth_execution_closure(root)

    assert report["status"] == "FAIL"
    assert report["checks"]["transaction_bearing_nonzero_delta"] is False


def test_tampered_source_cannot_close_account_truth(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    (root / "official/positions.json").write_text("tampered", encoding="utf-8")

    report = verify_account_truth_execution_closure(root)

    assert report["status"] == "FAIL"
    assert report["checks"]["operation_manifest_sha256_pass"] is False


def _bundle(
    tmp_path: Path,
    *,
    cash_delta: str = "0.31",
    size_delta: str = "-1",
) -> Path:
    root = tmp_path / "live-sell-calrun-1"
    official = root / "official"
    official.mkdir(parents=True)
    strategy_id = "taker-calibration-calrun-1"
    truth = {
        "account_truth_gate": "PASS",
        "official_source_status": "PASS",
        "comparison_scope": "CALIBRATION_DELTA",
        "as_of": "2026-08-23T14:20:14+00:00",
        "reconciliation_id": "truth-1",
        "strategy_ids": [strategy_id],
        "summary": {
            "baseline_id": "baseline-1",
            "baseline_as_of": "2026-08-23T14:17:54+00:00",
            "material_mismatch_count": 0,
            "mismatch_count": 0,
            "official_cash_delta": cash_delta,
            "paper_cash_delta": cash_delta,
            "paper_provisional_fill_count": 0,
        },
    }
    _write(official / "account-truth-summary.json", truth)
    items = [
        {
            "field_name": "size",
            "comparison_key": "asset-1",
            "official_value": size_delta,
            "paper_value": size_delta,
            "delta": "0",
            "tolerance": "0.000001",
            "status": "MATCH",
        },
        {
            "field_name": "cash_balance",
            "comparison_key": "account",
            "official_value": cash_delta,
            "paper_value": cash_delta,
            "delta": "0",
            "tolerance": "0.00001",
            "status": "MATCH",
        },
    ]
    (official / "account-truth-items.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in items),
        encoding="utf-8",
    )
    _write(official / "positions.json", {"asset": "asset-1"})
    trade = {
        "run_id": "calrun-1",
        "trade_time": {"utc": "2026-08-23T14:18:25+00:00"},
        "account_truth": {},
    }
    _write(official / "live-vs-paper-trade-record.json", trade)
    _write(root / "before.json", {"scenario_id": "live-sell-calrun-1"})
    _write(root / "real.json", {"evidence_mode": "LIVE"})
    _write(root / "paper.json", {})
    _write(root / "rules.json", {})

    truth_hash = _sha(official / "account-truth-summary.json")
    trade["account_truth"] = {"content_sha256": truth_hash}
    _write(official / "live-vs-paper-trade-record.json", trade)
    verified = [
        truth_hash,
        _sha(official / "account-truth-items.jsonl"),
        _sha(official / "positions.json"),
        _sha(official / "live-vs-paper-trade-record.json"),
    ]
    reconciliation = {
        "status": "PASS",
        "evidence_mode": "LIVE",
        "external_truth_gate": "PASS",
        "source_file_verification": {
            "status": "PASS",
            "verified_sha256": verified,
        },
    }
    _write(root / "reconciliation.json", reconciliation)
    files = {
        str(path.relative_to(root)): _sha(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    _write(
        root / "manifest.json",
        {
            "schema_version": "paper-operation-evidence-manifest-v1",
            "scenario_id": "live-sell-calrun-1",
            "operation_type": "SELL",
            "status": "PASS",
            "files": files,
        },
    )
    return root


def _write(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
