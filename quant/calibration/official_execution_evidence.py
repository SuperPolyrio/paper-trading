"""Build a read-only view of real execution and account-economics evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any


REWARD_ACTIVITY_TYPES = {
    "MAKER_REBATE",
    "TAKER_REBATE",
    "REWARD",
    "YIELD",
    "REFERRAL_REWARD",
}


def build_official_execution_evidence(
    connection_factory: Any,
    *,
    campaign_manifest: Mapping[str, Any],
    campaign_promotion: Mapping[str, Any],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    now = generated_at or datetime.now(timezone.utc)
    with connection_factory(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT activity_type,event_ts,transaction_hash,amount
            FROM quant.paper_official_account_activities
            ORDER BY event_ts,source_event_id
            """
        )
        activities = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT probe_id,probe_state,side,order_type,condition_id,decision_ts,
                   lifecycle,reconciliation,risk_snapshot,exchange_submit_called
            FROM quant.paper_calibration_probes
            ORDER BY decision_ts,probe_id
            """
        )
        probes = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT settlement_key,expected_real_payout,observed_real_payout,
                   cash_reconciliation_status,payload
            FROM quant.paper_calibration_pnl_settlements
            ORDER BY resolved_at,settlement_key
            """
        )
        settlements = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT * FROM quant.paper_official_account_sync_runs
            ORDER BY started_at DESC LIMIT 1
            """
        )
        latest_sync = cur.fetchone()

    official_trade_hashes = {
        normalized
        for row in activities
        if row["activity_type"] == "TRADE"
        and (normalized := _tx_hash(row.get("transaction_hash")))
    }
    calibratable = [row for row in probes if row["probe_state"] == "CALIBRATABLE"]
    calibratable_hashes = set().union(
        *(_probe_transaction_hashes(row) for row in calibratable)
    ) if calibratable else set()
    linked_hashes = official_trade_hashes & calibratable_hashes
    calibratable_without_transaction = sum(
        not _probe_transaction_hashes(row) for row in calibratable
    )
    cohort_counts = Counter(
        f"{str(row.get('side') or 'UNKNOWN').upper()}_"
        f"{str(row.get('order_type') or 'UNKNOWN').upper()}"
        for row in calibratable
    )
    condition_count = len(
        {str(row.get("condition_id") or "") for row in calibratable if row.get("condition_id")}
    )
    utc_day_count = len(
        {
            _utc_date(row.get("decision_ts"))
            for row in calibratable
            if row.get("decision_ts") is not None
        }
    )

    settlement_statuses = Counter(
        str(row.get("cash_reconciliation_status") or "UNKNOWN")
        for row in settlements
    )
    official_activity_reconciled = sum(
        str(
            (row.get("payload") or {})
            .get("redemption_evidence", {})
            .get("evidence_level")
            or ""
        ).startswith("official_activity")
        for row in settlements
    )
    chain_receipt_reconciled = sum(
        str((row.get("payload") or {}).get("redemption_evidence", {}).get("receipt_status"))
        == "1"
        for row in settlements
    )
    zero_payout_reconciled = sum(
        Decimal(str(row.get("expected_real_payout") or 0)) == 0
        and row.get("cash_reconciliation_status") == "PASS"
        for row in settlements
    )
    reward_counts = Counter(
        str(row["activity_type"])
        for row in activities
        if row["activity_type"] in REWARD_ACTIVITY_TYPES
    )

    payload: dict[str, Any] = {
        "schema_version": "official_execution_evidence_v1",
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "status": "PASS" if campaign_promotion.get("promotion_allowed") else "COLLECTING",
        "taker": {
            "campaign_status": campaign_manifest.get("status"),
            "promotion_allowed": bool(campaign_promotion.get("promotion_allowed")),
            "probe_count": int(campaign_manifest.get("probe_count") or len(probes)),
            "calibratable_count": len(calibratable),
            "holdout_count": int((campaign_manifest.get("fit") or {}).get("holdout_count") or 0),
            "condition_count": condition_count,
            "utc_day_count": utc_day_count,
            "cohort_counts": dict(sorted(cohort_counts.items())),
            "official_trade_activity_count": sum(
                row["activity_type"] == "TRADE" for row in activities
            ),
            "official_trade_transaction_count": len(official_trade_hashes),
            "official_transactions_linked_to_calibratable_probe": len(linked_hashes),
            "official_transactions_without_strict_probe_artifact": len(
                official_trade_hashes - linked_hashes
            ),
            "calibratable_probes_without_transaction": calibratable_without_transaction,
            "interpretation": (
                "Official TRADE rows prove wallet activity. They become strict simulator "
                "calibration samples only when a pre-submit paper prediction, market "
                "snapshot, lifecycle, and reconciliation artifact are linked."
            ),
        },
        "settlement": {
            "status": "PASS" if settlements and settlement_statuses == {"PASS": len(settlements)} else "BLOCKED",
            "count": len(settlements),
            "status_counts": dict(sorted(settlement_statuses.items())),
            "zero_payout_truth_reconciled": zero_payout_reconciled,
            "official_activity_reconciled": official_activity_reconciled,
            "chain_receipt_reconciled": chain_receipt_reconciled,
            "pending_count": settlement_statuses.get("PENDING_REDEMPTION", 0),
        },
        "rewards": {
            "status": "NO_OFFICIAL_EVIDENCE" if not reward_counts else "OFFICIAL_EVIDENCE_PRESENT",
            "activity_counts": dict(sorted(reward_counts.items())),
            "can_force_payout_with_one_trade": False,
            "interpretation": (
                "A live trade can create eligibility, but daily program thresholds and "
                "venue payout timing determine whether official reward evidence appears."
            ),
        },
        "latest_official_sync": _jsonable(dict(latest_sync)) if latest_sync else None,
        "next_actions": [
            "Continue small representative taker holdout probes; do not relabel unrelated wallet trades as calibration samples.",
            "Use post-only maker probes only when the order genuinely rests and later receives an official trade outcome.",
            "Keep reward synchronization running and calibrate only after a real official payout appears.",
        ],
    }
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return _jsonable(payload)


def render_official_execution_evidence_markdown(report: Mapping[str, Any]) -> str:
    taker = report["taker"]
    settlement = report["settlement"]
    rewards = report["rewards"]
    return "\n".join(
        [
            "# Official Execution Evidence",
            "",
            f"- Generated: `{report['generated_at']}`",
            f"- Overall: `{report['status']}`",
            "",
            "## Taker",
            "",
            f"- Strict calibratable probes: `{taker['calibratable_count']}`",
            f"- Independent holdout: `{taker['holdout_count']}`",
            f"- Conditions / UTC days: `{taker['condition_count']}` / `{taker['utc_day_count']}`",
            f"- Official TRADE transactions: `{taker['official_trade_transaction_count']}`",
            f"- Linked to strict probe artifacts: `{taker['official_transactions_linked_to_calibratable_probe']}`",
            f"- Unlinked wallet transactions: `{taker['official_transactions_without_strict_probe_artifact']}`",
            "",
            "## Settlement",
            "",
            f"- Status: `{settlement['status']}`",
            f"- PASS / pending: `{settlement['status_counts'].get('PASS', 0)}` / `{settlement['pending_count']}`",
            f"- Official activity reconciled: `{settlement['official_activity_reconciled']}`",
            f"- Chain receipt reconciled: `{settlement['chain_receipt_reconciled']}`",
            "",
            "## Rewards",
            "",
            f"- Status: `{rewards['status']}`",
            f"- Official activity counts: `{json.dumps(rewards['activity_counts'], sort_keys=True)}`",
            "- A trade can create eligibility but cannot force a venue payout.",
            "",
            "## Next Actions",
            "",
            *(f"- {item}" for item in report["next_actions"]),
            "",
        ]
    )


def write_official_execution_evidence(
    output_prefix: Path, report: Mapping[str, Any]
) -> tuple[Path, Path]:
    json_path = output_prefix.with_suffix(".json")
    md_path = output_prefix.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_official_execution_evidence_markdown(report), encoding="utf-8")
    return json_path, md_path


def _probe_transaction_hashes(row: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                walk(child_value, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif key.lower() in {
            "transactionhash",
            "transaction_hash",
            "transactionhashes",
            "transaction_hashes",
            "txhash",
            "tx_hash",
        }:
            normalized = _tx_hash(value)
            if normalized:
                found.add(normalized)

    walk(row.get("lifecycle") or {})
    walk(row.get("reconciliation") or {})
    return found


def _tx_hash(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if len(text) == 66 and text.startswith("0x") else None


def _utc_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date().isoformat()
    return str(value)[:10]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, Decimal)):
        return str(value)
    return value
