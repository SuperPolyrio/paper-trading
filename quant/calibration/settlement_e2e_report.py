"""Render an auditable historical settlement/redeem E2E evidence package."""

from __future__ import annotations

import csv
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


def build_settlement_e2e_report(settlement: Mapping[str, Any]) -> dict[str, Any]:
    payload = settlement.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    evidence = payload.get("redemption_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    expected = _decimal(settlement.get("expected_real_payout"))
    observed = _decimal(settlement.get("observed_real_payout"))
    paper_payout = _decimal(payload.get("paper_payout"), default=expected)
    real_realized_pnl = _decimal(settlement.get("real_realized_pnl_delta"))
    paper_realized_pnl = _decimal(settlement.get("paper_realized_pnl_delta"))
    token_after = _decimal(evidence.get("post_check_token_balance"), default=None)
    receipt_status = evidence.get("receipt_status")
    tx_hash = str(evidence.get("transaction_hash") or "")
    checks = {
        "cash_reconciliation_pass": str(
            settlement.get("cash_reconciliation_status") or ""
        ) == "PASS",
        "observed_payout_matches_expected": observed is not None and observed == expected,
        "paper_payout_matches_real": observed is not None and paper_payout == observed,
        "realized_pnl_matches_paper": (
            real_realized_pnl is not None
            and paper_realized_pnl is not None
            and real_realized_pnl == paper_realized_pnl
        ),
        "polygon_receipt_success": receipt_status == 1 and tx_hash.startswith("0x"),
        "token_burn_confirmed": token_after == Decimal("0"),
    }
    status = "LIVE_REDEEM_CONFIRMED" if all(checks.values()) else "EVIDENCE_INCOMPLETE"
    return {
        "schema_version": "settlement_redeem_e2e_v2",
        "mode": "HISTORICAL_RECONCILIATION",
        "status": status,
        "submit_called": False,
        "historical_execution_observed": True,
        "settlement_key": settlement.get("settlement_key"),
        "account_id": settlement.get("account_id"),
        "asset_id": settlement.get("asset_id"),
        "winning_asset_id": settlement.get("winning_asset_id"),
        "resolved_at": settlement.get("resolved_at"),
        "transaction": {
            "hash": tx_hash or None,
            "block_number": evidence.get("block_number"),
            "receipt_status": receipt_status,
            "source": evidence.get("source"),
            "activity_type": evidence.get("activity_type"),
        },
        "conditional_token": {
            "before": evidence.get("size"),
            "after": evidence.get("post_check_token_balance"),
        },
        "cashflow": {
            "expected_real_payout": _render(expected),
            "observed_real_payout": _render(observed),
            "paper_payout": _render(paper_payout),
            "payout_error": _render(observed - expected if observed is not None else None),
        },
        "pnl": {
            "real_realized_pnl_delta": _render(real_realized_pnl),
            "paper_realized_pnl_delta": _render(paper_realized_pnl),
        },
        "checks": checks,
        "scope": {
            "what_this_proves": [
                "A resolved winning calibration position produced the recorded real payout.",
                "The observed payout, paper payout, and cash reconciliation agree.",
                "The historical Polygon receipt succeeded and the held conditional token was burned.",
            ],
            "what_this_does_not_do": [
                "This exporter does not submit a redeem transaction.",
                "Wallet-wide USDC balances before and after the historical transaction were not retained, so only the verified redemption cashflow is compared.",
            ],
        },
    }


def write_settlement_e2e_report(output: Path, report: Mapping[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "balance_before_after.json", report)
    cashflow = report.get("cashflow") if isinstance(report.get("cashflow"), Mapping) else {}
    pnl = report.get("pnl") if isinstance(report.get("pnl"), Mapping) else {}
    token = report.get("conditional_token") if isinstance(report.get("conditional_token"), Mapping) else {}
    checks = report.get("checks") if isinstance(report.get("checks"), Mapping) else {}
    rows = [
        {
            "field": "payout",
            "paper": cashflow.get("paper_payout"),
            "real": cashflow.get("observed_real_payout"),
            "difference": cashflow.get("payout_error"),
            "status": _status(checks.get("paper_payout_matches_real")),
        },
        {
            "field": "realized_pnl_delta",
            "paper": pnl.get("paper_realized_pnl_delta"),
            "real": pnl.get("real_realized_pnl_delta"),
            "difference": _difference(
                pnl.get("real_realized_pnl_delta"), pnl.get("paper_realized_pnl_delta")
            ),
            "status": _status(
                checks.get("realized_pnl_matches_paper")
            ),
        },
        {
            "field": "conditional_token_after_redeem",
            "paper": "0",
            "real": token.get("after"),
            "difference": _difference(token.get("after"), "0"),
            "status": _status(checks.get("token_burn_confirmed")),
        },
    ]
    with (output / "paper_vs_real.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["field", "paper", "real", "difference", "status"],
        )
        writer.writeheader()
        writer.writerows(rows)
    transaction = report.get("transaction") if isinstance(report.get("transaction"), Mapping) else {}
    lines = [
        "# Redeem E2E",
        "",
        f"- Settlement: `{report.get('settlement_key')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Status: **{report.get('status')}**",
        f"- Historical transaction: `{transaction.get('hash')}`",
        f"- Polygon receipt status: `{transaction.get('receipt_status')}`",
        f"- Observed payout: `{cashflow.get('observed_real_payout')}` USDC",
        f"- Paper payout: `{cashflow.get('paper_payout')}` USDC",
        "- This report was exported from previously recorded evidence; it submitted no transaction.",
        "",
        "## Checks",
        "",
    ]
    lines.extend(f"- `{key}`: `{value}`" for key, value in checks.items())
    (output / "redeem_e2e.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _decimal(value: Any, *, default: Decimal | None = Decimal("0")) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _render(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _difference(left: Any, right: Any) -> str | None:
    lhs = _decimal(left, default=None)
    rhs = _decimal(right, default=None)
    return _render(lhs - rhs) if lhs is not None and rhs is not None else None


def _status(value: Any) -> str:
    return "PASS" if value is True else "FAIL"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
