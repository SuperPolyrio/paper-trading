"""Deterministic JSON/JSONL/Markdown account truth reports."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import AccountTruthGateStatus, AccountTruthReport, OfficialAccountBundle


def write_account_truth_report(
    *,
    output_root: Path | str,
    official: OfficialAccountBundle,
    report: AccountTruthReport,
    execution_gate_paths: Sequence[Path | str] = (),
) -> dict[str, str]:
    day_directory = Path(output_root) / official.source_as_of.date().isoformat()
    directory = day_directory / report.reconciliation_id.replace(":", "-")
    directory.mkdir(parents=True, exist_ok=True)
    execution = load_execution_gates(execution_gate_paths)
    combined_status = combined_gate_status(report.status, execution)
    manifest = dict(official.fetch_manifest)
    manifest.update(
        {
            "run_id": official.run_id,
            "account_truth_reconciliation_id": report.reconciliation_id,
        }
    )
    summary = {
        "schema_version": "account-truth-summary-v1",
        "reconciliation_id": report.reconciliation_id,
        "official_run_id": report.official_run_id,
        "account_address": report.account_address,
        "strategy_ids": list(report.strategy_ids),
        "as_of": report.as_of.isoformat(),
        "generated_at": report.generated_at.isoformat(),
        "account_truth_gate": report.status.value,
        "execution_fidelity_gate": execution,
        "combined_gate": combined_status,
        "official_source_status": report.official_source_status,
        "comparison_scope": report.comparison_scope,
        "pnl_truth_contract": _jsonable(
            report.summary.get("pnl_truth_contract", {})
        ),
        "content_sha256": report.content_sha256,
        "summary": _jsonable(report.summary),
        "comparison_item_count": len(report.comparison_rows),
    }
    mismatch_items = [_item_payload(item) for item in report.mismatches]
    items = _reconciliation_items(report)
    account_return = {
        "schema_version": "account-truth-return-view-v1",
        "as_of": report.as_of.isoformat(),
        "comparison_scope": report.comparison_scope,
        "comparison_basis": (
            "DELTA_SINCE_IMMUTABLE_BASELINE"
            if report.comparison_scope == "CALIBRATION_DELTA"
            else "AS_OF_WHOLE_ACCOUNT"
        ),
        "current_totals_comparable": report.comparison_scope == "WHOLE_ACCOUNT",
        "official": {
            "cash_balance": format(official.accounting.equity.cash_balance, "f"),
            "positions_value": format(official.accounting.equity.positions_value, "f"),
            "equity": format(official.accounting.equity.equity, "f"),
            "open_position_realized_pnl": format(
                sum((row.realized_pnl for row in official.positions), Decimal(0)),
                "f",
            ),
            "closed_position_realized_pnl": format(
                sum(
                    (row.realized_pnl for row in official.closed_positions),
                    Decimal(0),
                ),
                "f",
            ),
        },
        "paper": {
            "cash_balance": report.summary.get("paper_cash_balance"),
            "nav": report.summary.get("paper_nav"),
            "realized_pnl": report.summary.get("paper_realized_pnl"),
            "ledger_checkpoint": report.summary.get("paper_ledger_checkpoint"),
            "provisional_fill_count": report.summary.get(
                "paper_provisional_fill_count"
            ),
            "unmodeled_cashflow_count": report.summary.get(
                "paper_unmodeled_cashflow_count"
            ),
        },
        "account_delta": {
            "baseline_id": report.summary.get("baseline_id"),
            "baseline_as_of": report.summary.get("baseline_as_of"),
            "official_cash_delta": report.summary.get("official_cash_delta"),
            "paper_cash_delta": report.summary.get("paper_cash_delta"),
            "compared_asset_count": report.summary.get("compared_delta_asset_count"),
        },
        "account_truth_gate": report.status.value,
        "combined_gate": combined_status,
        "interpretation": (
            "Delta scopes compare changes since the immutable baseline; their "
            "strategy-level current totals are not whole-wallet totals. Official "
            "account truth is an independent result-layer check, while execution "
            "fidelity remains a separate gate."
        ),
    }
    paths = {
        "manifest": directory / "official-fetch-manifest.json",
        "summary": directory / "reconciliation-summary.json",
        "items": directory / "reconciliation-items.jsonl",
        "mismatches": directory / "mismatch-report.md",
        "account_return": directory / "account-return-report.json",
    }
    _atomic_text(paths["manifest"], _pretty_json(manifest))
    _atomic_text(paths["summary"], _pretty_json(summary))
    _atomic_text(
        paths["items"],
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in items),
    )
    _atomic_text(paths["mismatches"], _markdown(summary, mismatch_items))
    _atomic_text(paths["account_return"], _pretty_json(account_return))
    latest_directory = Path(output_root) / "latest"
    latest_directory.mkdir(parents=True, exist_ok=True)
    for path in paths.values():
        _atomic_text(latest_directory / path.name, path.read_text(encoding="utf-8"))
    if report.comparison_scope != "OFFICIAL_ONLY":
        comparable_directory = Path(output_root) / "latest-comparable"
        comparable_directory.mkdir(parents=True, exist_ok=True)
        for path in paths.values():
            _atomic_text(
                comparable_directory / path.name,
                path.read_text(encoding="utf-8"),
            )
    return {key: str(path.resolve()) for key, path in paths.items()}


def load_execution_gates(paths: Sequence[Path | str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            rows.append(
                {
                    "path": str(path),
                    "status": "MISSING",
                    "promotion_allowed": False,
                }
            )
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema_version = str(payload.get("schema_version") or "")
        status = str(payload.get("status") or "UNKNOWN")
        if schema_version == "calibration_reconciliation_run_v1":
            probe_count = int(payload.get("probe_count") or 0)
            calibratable = int(payload.get("calibratable_probe_count") or 0)
            gate_passed = (
                status == "PASS" and probe_count > 0 and calibratable == probe_count
            )
            evidence_scope = "RUN_RECONCILIATION"
            promotion_allowed = None
        else:
            gate_passed = status == "PASS" and bool(payload.get("promotion_allowed"))
            evidence_scope = "MODEL_PROMOTION"
            promotion_allowed = bool(payload.get("promotion_allowed"))
        rows.append(
            {
                "path": str(path.resolve()),
                "status": status,
                "gate_passed": gate_passed,
                "evidence_scope": evidence_scope,
                "promotion_allowed": promotion_allowed,
                "schema_version": schema_version,
            }
        )
    if not rows:
        status = "INSUFFICIENT_EVIDENCE"
    elif all(bool(row.get("gate_passed")) for row in rows):
        status = "PASS"
    else:
        status = "FAIL_EXECUTION_FIDELITY"
    return {"status": status, "sources": rows}


def combined_gate_status(
    account_status: AccountTruthGateStatus, execution: Mapping[str, Any]
) -> str:
    execution_status = str(execution.get("status") or "INSUFFICIENT_EVIDENCE")
    account_pass = account_status in {
        AccountTruthGateStatus.PASS,
        AccountTruthGateStatus.PASS_WITH_TIMING_LAG,
    }
    execution_pass = execution_status == "PASS"
    if account_pass and execution_pass:
        return "PASS"
    if (
        account_status is AccountTruthGateStatus.INSUFFICIENT_EVIDENCE
        or execution_status == "INSUFFICIENT_EVIDENCE"
    ):
        return "INSUFFICIENT_EVIDENCE"
    if not account_pass and not execution_pass:
        return "FAIL_BOTH"
    if not account_pass:
        return "FAIL_ACCOUNT_TRUTH"
    return "FAIL_EXECUTION_FIDELITY"


def _item_payload(item: Any) -> dict[str, Any]:
    return {
        "mismatch_id": item.mismatch_id,
        "mismatch_type": item.mismatch_type.value,
        "comparison_type": item.comparison_type,
        "comparison_key": item.comparison_key,
        "field_name": item.field_name,
        "official_value": item.official_value,
        "paper_value": item.paper_value,
        "delta": item.delta,
        "tolerance": item.tolerance,
        "reason": item.reason,
        "severity": item.severity,
        "retryable": item.retryable,
        "evidence": _jsonable(item.evidence),
    }


def _reconciliation_items(report: AccountTruthReport) -> list[dict[str, Any]]:
    mismatch_by_field = {
        (item.comparison_type, item.comparison_key, item.field_name): item
        for item in report.mismatches
    }
    represented: set[str] = set()
    rows: list[dict[str, Any]] = []
    for comparison in report.comparison_rows:
        key = (
            str(comparison.get("comparison_type") or ""),
            str(comparison.get("comparison_key") or ""),
            str(comparison.get("field_name") or ""),
        )
        mismatch = mismatch_by_field.get(key)
        row = {"record_type": "FIELD_COMPARISON", **_jsonable(comparison)}
        if mismatch is not None:
            represented.add(mismatch.mismatch_id)
            row.update(
                {
                    "status": mismatch.mismatch_type.value,
                    "mismatch_id": mismatch.mismatch_id,
                    "mismatch_type": mismatch.mismatch_type.value,
                    "retryable": mismatch.retryable,
                    "severity": mismatch.severity,
                    "reason": mismatch.reason,
                    "evidence": _jsonable(mismatch.evidence),
                }
            )
        else:
            row.update(
                {
                    "status": "MATCH",
                    "mismatch_id": None,
                    "mismatch_type": None,
                    "retryable": False,
                    "severity": "INFO",
                    "reason": "within configured tolerance",
                }
            )
        rows.append(row)
    for mismatch in report.mismatches:
        if mismatch.mismatch_id not in represented:
            rows.append({"record_type": "EVENT_MISMATCH", **_item_payload(mismatch)})
    return rows


def _markdown(summary: Mapping[str, Any], items: list[Mapping[str, Any]]) -> str:
    lines = [
        "# Account Truth Reconciliation",
        "",
        f"- As of: `{summary['as_of']}`",
        f"- Account truth: `{summary['account_truth_gate']}`",
        f"- Execution fidelity: `{summary['execution_fidelity_gate']['status']}`",
        f"- Combined gate: `{summary['combined_gate']}`",
        f"- Official source: `{summary['official_source_status']}`",
        f"- Mismatches: `{len(items)}`",
        "",
        "## Mismatches",
        "",
    ]
    if not items:
        lines.append("No mismatches.")
    for item in items:
        lines.extend(
            (
                f"### {item['mismatch_type']} / {item['comparison_key']}",
                "",
                f"- Field: `{item['field_name']}`",
                f"- Official / paper: `{item['official_value']}` / `{item['paper_value']}`",
                f"- Delta / tolerance: `{item['delta']}` / `{item['tolerance']}`",
                f"- Retryable: `{item['retryable']}`",
                f"- Reason: {item['reason']}",
                "",
            )
        )
    lines.extend(
        (
            "## Evidence",
            "",
            "- Raw source hashes and artifact paths: `official-fetch-manifest.json`",
            "- Every compared field, including MATCH rows: `reconciliation-items.jsonl`",
            "- Official and paper account totals: `account-return-report.json`",
            "",
            "## Safety",
            "",
            "This report is read-only. It never overwrites the paper ledger.",
            "",
        )
    )
    return "\n".join(lines)


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _pretty_json(value: Any) -> str:
    return json.dumps(_jsonable(value), indent=2, sort_keys=True) + "\n"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "value"):
        return _jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value
