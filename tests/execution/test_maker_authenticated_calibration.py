from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from quant.maker.authenticated_calibration import (
    authenticated_holdout_row,
    build_authenticated_calibration,
)
from quant.maker.evaluate_holdout import evaluate

ORDER_ID = "0x" + "ab" * 32


def test_imports_authenticated_no_fill_and_keeps_promotion_blocked(
    tmp_path: Path,
) -> None:
    root = _bundle(tmp_path, _probe("NO_FILL"))

    row = authenticated_holdout_row(root / "official" / "probe.json")
    result = build_authenticated_calibration(
        evidence_roots=[root.parent],
        output_dir=tmp_path / "output",
        config_path=Path("configs/calibration/maker_core_v1.yaml"),
    )

    assert row["evidence_authority"] == "AUTHENTICATED_OWN_ORDER"
    assert row["prediction_precedes_submission"] is True
    assert row["actual_outcome"] == "NO_FILL"
    assert row["outcome_observation"]["label"] == "CENSORED_AT_30S"
    assert row["outcome_observation"]["first_fill_right_censored"] is True
    assert result["status"] == "BLOCKED"
    assert result["accepted_count"] == 1
    assert result["outcome_counts"] == {"NO_FILL": 1}


def test_rejects_timeout_as_no_fill(tmp_path: Path) -> None:
    payload = _probe("NO_FILL")
    payload["user_ws_capture"]["status"] = "TIMEOUT"
    payload["user_ws_capture"]["terminal_observed"] = False
    payload["user_ws_capture"]["events"] = payload["user_ws_capture"]["events"][:1]
    root = _bundle(tmp_path, payload)

    with pytest.raises(ValueError, match="LIVE/CANCELED"):
        authenticated_holdout_row(root / "official" / "probe.json")


def test_rejects_tampered_official_evidence(tmp_path: Path) -> None:
    root = _bundle(tmp_path, _probe("NO_FILL"))
    path = root / "official" / "user-ws.json"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA256 mismatch"):
        authenticated_holdout_row(root / "official" / "probe.json")


def test_rejects_prediction_recorded_after_submission(tmp_path: Path) -> None:
    payload = _probe("NO_FILL")
    payload["started_at"] = "2026-08-01T00:00:02+00:00"
    root = _bundle(tmp_path, payload)

    with pytest.raises(ValueError, match="before submit"):
        authenticated_holdout_row(root / "official" / "probe.json")


def test_imports_authenticated_full_fill(tmp_path: Path) -> None:
    root = _bundle(tmp_path, _probe("FULL"))

    row = authenticated_holdout_row(root / "official" / "probe.json")

    assert row["actual_outcome"] == "FULL"
    assert row["actual_filled_size"] == "5"
    assert row["actual_time_to_first_fill_seconds"] == "10.0"
    assert row["actual_time_to_full_fill_seconds"] == "20.0"
    assert row["official_trade_count"] == 1
    assert row["official_orderfilled_status"] == "CONFIRMED_MATCH"


def test_copied_evidence_bundle_is_deduplicated_by_content(tmp_path: Path) -> None:
    root = _bundle(tmp_path, _probe("NO_FILL"))
    shutil.copytree(root, tmp_path / "evidence-copy")

    result = build_authenticated_calibration(
        evidence_roots=[root.parent, tmp_path / "evidence-copy"],
        output_dir=tmp_path / "output",
        config_path=Path("configs/calibration/maker_core_v1.yaml"),
    )

    assert result["accepted_count"] == 1
    assert result["rejected_count"] == 0


def test_rejects_full_fill_without_orderfilled_truth(tmp_path: Path) -> None:
    payload = _probe("FULL")
    payload["onchain_orderfilled"]["status"] = "PENDING_CHAIN_INDEX"
    root = _bundle(tmp_path, payload)

    with pytest.raises(ValueError, match="OrderFilled evidence"):
        authenticated_holdout_row(root / "official" / "probe.json")


def test_rejects_full_fill_without_successful_polygon_receipt(
    tmp_path: Path,
) -> None:
    payload = _probe("FULL")
    payload["onchain_orderfilled"]["receipt_truth"] = {
        "status": "SOURCE_UNAVAILABLE",
        "complete": False,
        "receipts": [],
    }
    root = _bundle(tmp_path, payload)

    with pytest.raises(ValueError, match="Polygon transaction receipts"):
        authenticated_holdout_row(root / "official" / "probe.json")


def test_raw_row_cannot_satisfy_authenticated_evidence_gate() -> None:
    config = {
        "model_version": "maker-test",
        "holdout_gates": {
            "minimum_samples": 1,
            "independent_events": 1,
            "utc_days": 1,
            "max_ece": 1,
            "max_false_positive_fill_upper_95": 1,
            "require_brier_better_than_naive": False,
            "require_authenticated_evidence": True,
        },
    }
    report = evaluate(
        [
            {
                "event_id": "event-1",
                "utc_day": "2026-08-01",
                "artifact_complete": True,
                "p_no_fill": 1,
                "actual_outcome": "NO_FILL",
            }
        ],
        config=config,
    )

    assert report["status"] == "BLOCKED"
    assert report["checks"]["authenticated_own_order_evidence_100pct"] is False


def _probe(outcome: str) -> dict:
    started = "2026-08-01T00:00:00+00:00"
    submitted = "2026-08-01T00:00:01+00:00"
    no_fill = outcome == "NO_FILL"
    matched = "0" if no_fill else "5"
    tx_hash = "0x" + "34" * 32
    truth = {
        "schema_version": "order_rest_reconciliation_v1",
        "order_id": ORDER_ID,
        "actual_matched_size": matched,
        "actual_avg_price": None if no_fill else "0.4",
        "actual_quote_amount": "0" if no_fill else "2",
        "actual_fee": None if no_fill else "0",
        "matched_trade_count": 0 if no_fill else 1,
        "rest_order_reconciled": True,
        "rest_trade_reconciled": not no_fill,
        "ledger_truth": "PENDING" if no_fill else "CONFIRMED",
        "liquidity_role_truth": None if no_fill else "MAKER",
        "first_match_at": None if no_fill else "2026-08-01T00:00:10+00:00",
        "last_match_at": None if no_fill else "2026-08-01T00:00:20+00:00",
        "reconciled_at": "2026-08-01T00:00:30+00:00",
    }
    statuses = ["LIVE", "CANCELED" if no_fill else "MATCHED"]
    events = [{"payload": {"id": ORDER_ID, "status": status}} for status in statuses]
    rest_order = {
        "id": ORDER_ID,
        "status": "CANCELED" if no_fill else "MATCHED",
        "original_size": "5",
        "size_matched": matched,
    }
    return {
        "schema_version": "maker_post_only_live_probe_v1",
        "run_id": "maker-calrun-test",
        "mode": "LIVE",
        "status": "CALIBRATABLE",
        "started_at": started,
        "completed_at": "2026-08-01T00:00:31+00:00",
        "actual_outcome": outcome,
        "artifact_complete": True,
        "exchange_submit_called": True,
        "cancel_acknowledged": True,
        "order_still_open": False,
        "order_id": ORDER_ID,
        "asset_id": "123",
        "market_id": "market-1",
        "placement": "AT_BEST",
        "resting_seconds": 30,
        "candidate": {
            "condition_id": "condition-1",
            "source_category": "politics",
        },
        "intent": {
            "order_type": "GTC",
            "post_only": True,
            "side": "BUY",
            "size": "5",
            "limit_price": "0.4",
        },
        "signed_order_audit": {
            "order_type": "GTC",
            "post_only": True,
            "exchange_submit_called": True,
        },
        "model_predictions": {
            "PROBABILISTIC_QUEUE": {
                "p_no_fill": "1" if no_fill else "0",
                "p_partial": "0",
                "p_full": "0" if no_fill else "1",
                "expected_filled_size": matched,
                "expected_time_to_first_fill_seconds": None if no_fill else "10",
                "expected_time_to_full_fill_seconds": None if no_fill else "20",
            }
        },
        "submission": {"success": True, "orderID": ORDER_ID},
        "cancellation": {"canceled": [ORDER_ID]},
        "truth": truth,
        "rest_reconciliation": {
            "order": rest_order,
            "trades": (
                []
                if no_fill
                else [
                    {
                        "id": "trade-1",
                        "status": "CONFIRMED",
                        "transaction_hash": tx_hash,
                    }
                ]
            ),
        },
        "user_ws_capture": {
            "status": "TERMINAL",
            "terminal_observed": True,
            "order_id": ORDER_ID,
            "submitted_at": submitted,
            "events": events,
        },
        "account_before": {
            "collateral": {"balance": "10"},
            "conditional": {"balance": "2"},
        },
        "account_after": {
            "collateral": {"balance": "10" if no_fill else "8"},
            "conditional": {"balance": "2" if no_fill else "7"},
        },
        "account_delta": {
            "collateral": "0" if no_fill else "-2",
            "conditional": "0" if no_fill else "5",
        },
        "onchain_orderfilled": {
            "schema_version": "maker_own_orderfilled_truth_v1",
            "status": "NOT_REQUIRED_NO_FILL" if no_fill else "CONFIRMED_MATCH",
            "order_id": ORDER_ID,
            "asset_id": "123",
            "expected_matched_size": matched,
            "matched_size": matched,
            "matched_rows": [],
            "expected_transaction_hashes": [],
            "matched_transaction_hashes": [],
            "transaction_coverage_complete": True,
            "receipt_truth": (
                {
                    "status": "NOT_REQUIRED_NO_FILL",
                    "complete": True,
                    "receipts": [],
                }
                if no_fill
                else {
                    "status": "CONFIRMED_SUCCESS",
                    "complete": True,
                    "receipts": [
                        {
                            "transaction_hash": tx_hash,
                            "status": "0x1",
                            "block_number": "0x10",
                            "payload_sha256": "1" * 64,
                        }
                    ],
                }
            ),
        },
        "market_snapshot": {"fee_taker_only": True},
    }


def _bundle(tmp_path: Path, probe: dict) -> Path:
    root = tmp_path / "evidence" / "live-maker-test"
    official = root / "official"
    official.mkdir(parents=True)
    source_payloads = {
        "official/probe.json": probe,
        "official/user-ws.json": probe["user_ws_capture"],
        "official/order-trades.json": {
            "order_id": ORDER_ID,
            "truth": probe["truth"],
            "rest_reconciliation": probe["rest_reconciliation"],
        },
        "official/account-balances.json": {
            "before": probe["account_before"],
            "after": probe["account_after"],
        },
        "official/orderfilled.json": probe["onchain_orderfilled"],
    }
    for relative, payload in source_payloads.items():
        _write(root / relative, payload)
    source_hashes = {relative: _sha(root / relative) for relative in source_payloads}
    _write(
        root / "real.json",
        {
            "operation_type": "MAKER",
            "evidence_mode": "LIVE",
            "source_manifest": {"evidence_files": source_hashes},
        },
    )
    _write(root / "before.json", {})
    _write(root / "paper.json", {})
    _write(root / "rules.json", {})
    _write(root / "reconciliation.json", {"status": "PASS"})
    files = {
        str(path.relative_to(root)): _sha(path) for path in sorted(root.rglob("*.json"))
    }
    _write(
        root / "manifest.json",
        {
            "status": "PASS",
            "operation_type": "MAKER",
            "files": files,
        },
    )
    return root


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
