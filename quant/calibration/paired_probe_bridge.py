"""Bridge persisted live-calibration truth into the paper paired-probe audit."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from quant.paper.live_shadow_store import LiveShadowStore
from quant.paper.paired_probe import (
    RECORD_ONLY,
    attach_live_lifecycle,
    evaluate_probe_status,
)


_ACTUAL_TO_TERMINAL = {
    "FULL": "FILLED",
    "PARTIAL": "PARTIAL_FILLED",
    "NO_FILL": "NO_FILL",
    "REJECT": "REJECTED",
}


def sync_calibration_probe_to_paired(
    calibration_probe: Mapping[str, Any],
    *,
    store: LiveShadowStore,
) -> dict[str, Any]:
    """Idempotently attach an externally submitted live probe to its paper A leg."""

    paired_id = str(calibration_probe.get("paired_probe_id") or "").strip()
    if not paired_id:
        raise ValueError("calibration probe has no paired_probe_id")
    if not bool(calibration_probe.get("exchange_submit_called")):
        raise ValueError("calibration probe did not cross the live submit boundary")
    paired = store.load_paired_probe(paired_id)
    if paired is None:
        raise ValueError(f"paired probe is missing: {paired_id}")
    _validate_identity(calibration_probe, paired)

    previous_mode = str(paired.get("mode") or "")
    promoted = dict(paired)
    promoted["mode"] = RECORD_ONLY
    previous_evidence = _mapping(promoted.get("orderfilled_ex_self"))
    expected_transactions = {
        str(value).lower()
        for value in _mapping(calibration_probe.get("lifecycle")).get(
            "transaction_hashes"
        ) or []
        if value not in (None, "")
    }
    evidence_requires_recheck = not bool(
        previous_evidence.get("source_coverage_complete")
    ) or (
        bool(expected_transactions)
        and not bool(previous_evidence.get("transaction_confirmation_complete"))
    )
    if previous_mode != RECORD_ONLY:
        promoted["live_lifecycle"] = {
            "state": "AWAITING_IMPORT",
            "terminal_status": None,
            "event_count": 0,
            "events": [],
            "probe_id": paired_id,
            "client_order_id": paired.get("client_order_id"),
            "network_submit_called_by_probe": False,
            "reason": "awaiting externally submitted live calibration lifecycle",
        }
        promoted["orderfilled_ex_self"] = {
            "state": "PENDING",
            "window_start": paired.get("decision_ts"),
            "window_end": None,
            "source_watermark": None,
            "source_coverage_complete": False,
            "transaction_confirmation_complete": False,
            "raw_count": 0,
            "deduped_count": 0,
            "duplicate_count": 0,
            "excluded_self_count": 0,
            "ex_self_count": 0,
            "events": [],
            "used_for_actual_live_outcome": False,
            "prediction": "PENDING",
            "reason": "awaiting delayed canonical OrderFilled ex-self join",
        }
    elif evidence_requires_recheck:
        pending_evidence = dict(previous_evidence)
        pending_evidence.setdefault("source_watermark", None)
        pending_evidence.setdefault("source_coverage_complete", False)
        pending_evidence.setdefault("transaction_confirmation_complete", False)
        if not bool(previous_evidence.get("source_coverage_complete")):
            pending_evidence["state"] = (
                "SOURCE_LAG"
                if previous_evidence.get("source_watermark")
                else "PENDING"
            )
        elif expected_transactions:
            pending_evidence["state"] = "PENDING_TRANSACTION_CONFIRMATION"
        promoted["orderfilled_ex_self"] = pending_evidence

    live_event = calibration_probe_live_event(calibration_probe, paired_id=paired_id)
    updated = attach_live_lifecycle(
        promoted,
        [live_event],
        source="live-calibration-runner",
    )
    audit = dict(updated.get("audit") or {})
    audit["external_live"] = calibration_probe_audit(calibration_probe)
    audit["exchange_submit_called"] = False
    audit["order_submitter_present"] = False
    updated["audit"] = audit
    updated["status"] = evaluate_probe_status(updated)
    return store.upsert_paired_probe(updated)


def calibration_probe_live_event(
    calibration_probe: Mapping[str, Any],
    *,
    paired_id: str,
) -> dict[str, Any]:
    reconciliation = _mapping(calibration_probe.get("reconciliation"))
    order_truth = _mapping(reconciliation.get("order"))
    lifecycle = _mapping(calibration_probe.get("lifecycle"))
    timestamps = _mapping(calibration_probe.get("timestamps"))
    actual_class = str(reconciliation.get("actual_class") or "").upper()
    terminal = _ACTUAL_TO_TERMINAL.get(actual_class) or _terminal_from_lifecycle(
        lifecycle
    )
    if terminal is None:
        raise ValueError("calibration probe has no terminal live truth")
    event_time = (
        order_truth.get("first_match_at")
        or order_truth.get("last_match_at")
        or timestamps.get("trade_confirmed_ts")
        or timestamps.get("trade_mined_ts")
        or timestamps.get("http_response_completed_ts")
        or calibration_probe.get("updated_at")
        or datetime.now(timezone.utc)
    )
    signed = _mapping(calibration_probe.get("signed_order_audit"))
    return {
        "probe_id": paired_id,
        "asset_id": str(calibration_probe.get("asset_id") or ""),
        "order_hash": (
            lifecycle.get("order_id") or signed.get("order_hash")
        ),
        "status": terminal,
        "event_time": event_time,
        "accepted_at": timestamps.get("http_response_completed_ts"),
        "filled_size": order_truth.get("actual_matched_size"),
        "avg_fill_price": order_truth.get("actual_avg_price"),
        "fee": reconciliation.get("actual_fee"),
        "actual_class": actual_class or None,
        "calibration_probe_id": calibration_probe.get("probe_id"),
        "calibration_run_id": calibration_probe.get("run_id"),
    }


def calibration_probe_audit(
    calibration_probe: Mapping[str, Any],
) -> dict[str, Any]:
    signed = _mapping(calibration_probe.get("signed_order_audit"))
    lifecycle = _mapping(calibration_probe.get("lifecycle"))
    reconciliation = _mapping(calibration_probe.get("reconciliation"))
    order_truth = _mapping(reconciliation.get("order"))
    addresses = {
        str(value).lower()
        for value in (
            signed.get("maker"),
            signed.get("signer"),
            _mapping(lifecycle.get("rest_order")).get("maker_address"),
        )
        if value not in (None, "")
    }
    order_hashes = {
        str(value).lower()
        for value in (
            signed.get("order_hash"),
            lifecycle.get("order_id"),
            order_truth.get("order_id"),
        )
        if value not in (None, "")
    }
    return {
        "schema_version": "paired_external_live_audit_v1",
        "calibration_probe_id": calibration_probe.get("probe_id"),
        "calibration_run_id": calibration_probe.get("run_id"),
        "probe_state": calibration_probe.get("probe_state"),
        "exchange_submit_called": bool(
            calibration_probe.get("exchange_submit_called")
        ),
        "actual_class": reconciliation.get("actual_class"),
        "own_addresses": sorted(addresses),
        "own_order_hashes": sorted(order_hashes),
        "transaction_hashes": sorted(
            str(value).lower()
            for value in lifecycle.get("transaction_hashes") or []
            if value not in (None, "")
        ),
    }


def _validate_identity(
    calibration_probe: Mapping[str, Any],
    paired: Mapping[str, Any],
) -> None:
    mismatches = [
        key
        for key in ("asset_id", "market_id", "condition_id")
        if str(calibration_probe.get(key) or "") != str(paired.get(key) or "")
    ]
    calibration_prediction = _mapping(calibration_probe.get("prediction"))
    paper_prediction = _mapping(paired.get("paper_prediction"))
    if (
        calibration_prediction.get("audit_key")
        and paper_prediction.get("audit_key")
        and str(calibration_prediction["audit_key"])
        != str(paper_prediction["audit_key"])
    ):
        mismatches.append("paper_prediction.audit_key")
    if mismatches:
        raise ValueError("paired calibration identity mismatch: " + ",".join(mismatches))


def _terminal_from_lifecycle(lifecycle: Mapping[str, Any]) -> str | None:
    state = str(lifecycle.get("state") or "").upper()
    if state in {"CONFIRMED", "MATCHED", "FILLED"}:
        return "FILLED"
    if state in {"CANCELED", "CANCELLED", "UNMATCHED", "NO_FILL"}:
        return "NO_FILL"
    if state in {"REJECTED", "HTTP_REJECTED", "FAILED"}:
        return "REJECTED"
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
