"""Reconcile stored calibration evidence without placing or retrying orders."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from quant.simulator.economics.fee_rounding import round_fee

from .accounting_reconciler import reconcile_accounting
from .calibration_domain import ProbeState, artifact_bitmap, payload_hash
from .order_rest_reconciler import reconcile_order_lifecycle
from .self_trade_filter import SelfIdentity, filter_self_evidence
from .store import CalibrationStore


def reconcile_run(store: CalibrationStore, run_id: str) -> dict[str, Any]:
    run = store.load_run(run_id)
    if run is None:
        raise ValueError(f"unknown calibration run: {run_id}")
    probes = store.load_probes(run_id)
    if str(run.get("mode")) == "no-submit":
        valid = [
            row
            for row in probes
            if str(row.get("probe_state")) == ProbeState.SIGNED.value
            and bool((row.get("artifact_bitmap") or {}).get("complete"))
            and not bool(row.get("exchange_submit_called"))
        ]
        return {
            "schema_version": "calibration_reconciliation_run_v1",
            "run_id": run_id,
            "mode": "no-submit",
            "status": "PASS" if probes and len(valid) == len(probes) else "FAIL",
            "probe_count": len(probes),
            "reconciled_probe_count": len(valid),
            "calibratable_probe_count": 0,
            "exchange_submit_count": sum(bool(row.get("exchange_submit_called")) for row in probes),
            "note": "no-submit probes verify artifacts but never become live calibration samples",
        }

    rows: list[dict[str, Any]] = []
    for probe in probes:
        reconciled = reconcile_probe(probe, store.load_events(str(probe["probe_id"])))
        pnl = store.apply_probe_pnl(reconciled)
        reconciled["reconciliation"] = {
            **dict(reconciled.get("reconciliation") or {}),
            "pnl": pnl,
        }
        persisted = store.upsert_probe(reconciled)
        store.append_events([_reconciliation_event(persisted)])
        rows.append(persisted)
    state_counts = _counts(str(row.get("probe_state") or "UNKNOWN") for row in rows)
    report = {
        "schema_version": "calibration_reconciliation_run_v1",
        "run_id": run_id,
        "mode": "live",
        "status": "PASS" if rows and state_counts == {ProbeState.CALIBRATABLE.value: len(rows)} else "PENDING",
        "probe_count": len(rows),
        "calibratable_probe_count": state_counts.get(ProbeState.CALIBRATABLE.value, 0),
        "state_counts": state_counts,
    }
    if report["status"] == "PASS":
        store.finish_run(run_id, status="PROBE_COMPLETE", report=report)
    return report


def reconcile_probe(
    probe: Mapping[str, Any],
    stored_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    row = dict(probe)
    lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), Mapping) else {}
    order_id = str(lifecycle.get("order_id") or lifecycle.get("orderID") or "")
    user_events = [_event_payload(event) for event in stored_events if _is_user_event(event)]
    rest_order = lifecycle.get("rest_order") if isinstance(lifecycle.get("rest_order"), Mapping) else None
    rest_trades = lifecycle.get("rest_trades") if isinstance(lifecycle.get("rest_trades"), list) else []
    order_truth = reconcile_order_lifecycle(
        order_id=order_id,
        user_ws_events=user_events,
        rest_order=rest_order,
        rest_trades=rest_trades,
    )

    signed = row.get("signed_order_audit") if isinstance(row.get("signed_order_audit"), Mapping) else {}
    identity = SelfIdentity.build(
        addresses=(str(signed.get("maker") or ""), str(signed.get("signer") or "")),
        order_hashes=(str(signed.get("order_hash") or ""), order_id),
        trade_ids=_values(lifecycle.get("trade_ids")),
        transaction_hashes=_values(lifecycle.get("transaction_hashes")),
        maker_order_ids=_values(lifecycle.get("maker_order_ids")),
    )
    onchain_rows = lifecycle.get("onchain_events") if isinstance(lifecycle.get("onchain_events"), list) else []
    self_evidence = filter_self_evidence(onchain_rows, identity)
    if lifecycle.get("account_before") and lifecycle.get("account_after"):
        lifecycle = dict(lifecycle)
        lifecycle["accounting"] = accounting_payload_for_probe(
            side=str(row.get("side") or ""),
            asset_id=str(row.get("asset_id") or ""),
            before=lifecycle["account_before"],
            after=lifecycle["account_after"],
            row=row,
            truth=order_truth,
        )
        row["lifecycle"] = lifecycle
    accounting = _accounting_result(lifecycle.get("accounting"))
    reconciliation = {
        "schema_version": "taker_probe_reconciliation_v1",
        "order": order_truth,
        "orderfilled_ex_self": self_evidence,
        "accounting": accounting,
        **_prediction_errors(row, order_truth),
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
    }
    present = set((row.get("artifact_bitmap") or {}).get("present") or ())
    if lifecycle.get("http_request_audit"):
        present.add("HTTP_REQUEST_PRESENT")
    if lifecycle.get("http_response"):
        present.add("HTTP_RESPONSE_PRESENT")
    if order_id:
        present.add("ORDER_ID_PRESENT")
    user_order_evidence_source = None
    if any(_event_kind(event) == "ORDER" for event in user_events):
        present.add("USER_ORDER_EVENT_PRESENT")
        user_order_evidence_source = "user_ws_order"
    elif (
        str(row.get("order_type") or "").upper() in {"FAK", "FOK"}
        and order_truth["rest_order_reconciled"]
        and order_truth["execution_truth"] == "MATCHED"
        and any(_event_kind(event) == "TRADE" for event in user_events)
    ):
        # Immediate market fills may emit only TRADE lifecycle messages on user WS.
        present.add("USER_ORDER_EVENT_PRESENT")
        user_order_evidence_source = "user_ws_trade_plus_rest_order"
    if any(_event_kind(event) == "TRADE" for event in user_events) or rest_trades:
        present.add("USER_TRADE_EVENT_PRESENT")
    if order_truth["rest_order_reconciled"]:
        present.add("REST_ORDER_RECONCILED")
    if order_truth["rest_trade_reconciled"]:
        present.add("REST_TRADE_RECONCILED")
    if order_truth["final_trade_status_present"]:
        present.add("FINAL_TRADE_STATUS_PRESENT")
    if self_evidence["state"] == "VERIFIED":
        present.add("SELF_TRADE_FILTERED")
    if accounting["accounting_reconciled"]:
        present.add("ACCOUNTING_RECONCILED")
    reconciliation["user_order_evidence_source"] = user_order_evidence_source
    bitmap = artifact_bitmap(present, live=True)

    if self_evidence["self_trade_contaminated"]:
        state = ProbeState.SELF_TRADE_CONTAMINATED
    elif accounting["status"] == "FAIL":
        state = ProbeState.ACCOUNTING_MISMATCH
    elif bitmap["complete"] and not order_truth["errors"]:
        state = ProbeState.CALIBRATABLE
    else:
        state = ProbeState.DATA_INCOMPLETE
    row.update(
        probe_state=state.value,
        artifact_bitmap=bitmap,
        reconciliation=reconciliation,
        errors=sorted(
            set(
                [
                    str(item)
                    for item in row.get("errors") or ()
                    if not str(item).startswith("missing_artifact:")
                ]
                + [str(item) for item in order_truth["errors"]]
                + [f"missing_artifact:{item}" for item in bitmap["missing"]]
            )
        ),
    )
    return row


def _accounting_result(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, Mapping) else {}
    required = {"expected_cash", "actual_cash", "expected_positions", "actual_positions"}
    if not required.issubset(payload):
        return {
            "schema_version": "calibration_accounting_reconciliation_v1",
            "status": "MISSING",
            "accounting_reconciled": False,
            "missing": sorted(required - set(payload)),
        }
    return reconcile_accounting(
        expected_cash=payload["expected_cash"],
        actual_cash=payload["actual_cash"],
        expected_positions=payload["expected_positions"],
        actual_positions=payload["actual_positions"],
        tolerance=payload.get("tolerance", "0.00001"),
    )


def _prediction_errors(row: Mapping[str, Any], truth: Mapping[str, Any]) -> dict[str, Any]:
    prediction = row.get("prediction") if isinstance(row.get("prediction"), Mapping) else {}
    predicted_class = _fill_class(prediction.get("status"), prediction.get("filled_size"))
    actual_class = _actual_fill_class(row, truth)
    predicted_price = _decimal(prediction.get("avg_fill_price"))
    actual_price = _decimal(truth.get("actual_avg_price"))
    tick = _decimal((row.get("market_snapshot") or {}).get("tick_size"), Decimal("0"))
    predicted_size = _decimal(prediction.get("filled_size"))
    actual_size = _decimal(truth.get("actual_matched_size"))
    predicted_fee = _decimal(prediction.get("total_fee"))
    actual_fee, actual_fee_source = actual_fee_for_probe(row, truth)
    return {
        "order_type": row.get("order_type"),
        "predicted_class": predicted_class,
        "actual_class": actual_class,
        "price_error_ticks": (
            format(abs(actual_price - predicted_price) / tick, "f")
            if tick > 0 and actual_price > 0 and predicted_price > 0
            else None
        ),
        "filled_size_relative_error": (
            format(abs(actual_size - predicted_size) / actual_size, "f") if actual_size > 0 else None
        ),
        "depth_survival_ratio": (
            format(actual_size / predicted_size, "f") if predicted_size > 0 else None
        ),
        "predicted_fee": format(predicted_fee, "f"),
        "actual_fee": format(actual_fee, "f"),
        "actual_fee_source": actual_fee_source,
        "fee_error": format(abs(actual_fee - predicted_fee), "f"),
    }


def _actual_fill_class(row: Mapping[str, Any], truth: Mapping[str, Any]) -> str:
    execution_truth = str(truth.get("execution_truth") or "").upper()
    actual_size = _decimal(truth.get("actual_matched_size"))
    actual_price = _decimal(truth.get("actual_avg_price"))
    if execution_truth in {"REJECTED", "HTTP_REJECTED"}:
        return "REJECT"
    if actual_size <= 0:
        return "NO_FILL" if execution_truth != "UNKNOWN" else "UNKNOWN"
    requested = _effective_requested_amount(row)
    unit = str(row.get("amount_unit") or "SHARES").upper()
    actual_amount = (
        _decimal(truth.get("actual_quote_amount")).quantize(Decimal("0.000001"))
        if unit == "QUOTE" and truth.get("actual_quote_amount") not in (None, "")
        else actual_size * actual_price
        if unit == "QUOTE"
        else actual_size
    )
    tolerance = max(Decimal("0.000001"), requested * Decimal("0.00000001"))
    return "FULL" if actual_amount + tolerance >= requested else "PARTIAL"


def _effective_requested_amount(row: Mapping[str, Any]) -> Decimal:
    signed = (
        row.get("signed_order_audit")
        if isinstance(row.get("signed_order_audit"), Mapping)
        else {}
    )
    maker_amount = _decimal(signed.get("maker_amount")) / Decimal("1000000")
    if maker_amount > 0:
        return maker_amount
    return _decimal(row.get("amount"))


def actual_fee_for_probe(row: Mapping[str, Any], truth: Mapping[str, Any]) -> tuple[Decimal, str]:
    if truth.get("actual_fee") not in (None, ""):
        return _decimal(truth["actual_fee"]), "trade_explicit"
    market = row.get("market_snapshot") if isinstance(row.get("market_snapshot"), Mapping) else {}
    rate = max(Decimal("0"), _decimal(market.get("fee_rate")))
    exponent = max(Decimal("0"), _decimal(market.get("fee_exponent")))
    observations = (
        truth.get("trade_observations")
        if isinstance(truth.get("trade_observations"), list)
        else []
    )
    total = Decimal("0")
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        size = _decimal(observation.get("size"))
        price = _decimal(observation.get("price"))
        base = max(Decimal("0"), price * (Decimal("1") - price))
        try:
            fee = size * rate * (base**exponent)
        except Exception:
            fee = Decimal(str(float(size) * float(rate) * (float(base) ** float(exponent))))
        total += round_fee(fee)
    return total, "market_fee_curve_reconstructed"


def accounting_payload_for_probe(
    *,
    side: str,
    asset_id: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    row: Mapping[str, Any],
    truth: Mapping[str, Any],
) -> dict[str, Any]:
    before_cash = _decimal((before.get("collateral") or {}).get("balance"))
    after_cash = _decimal((after.get("collateral") or {}).get("balance"))
    before_position = _decimal((before.get("conditional") or {}).get("balance"))
    after_position = _decimal((after.get("conditional") or {}).get("balance"))
    size = _decimal(truth.get("actual_matched_size"))
    price = _decimal(truth.get("actual_avg_price"))
    fee, fee_source = actual_fee_for_probe(row, truth)
    quote = _decimal(truth.get("actual_quote_amount"), size * price).quantize(
        Decimal("0.000001")
    ).normalize()
    if side.upper() == "BUY":
        expected_cash = before_cash - quote - fee
        expected_position = before_position + size
    else:
        expected_cash = before_cash + quote - fee
        expected_position = before_position - size
    return {
        "expected_cash": str(expected_cash),
        "actual_cash": str(after_cash),
        "expected_positions": {str(asset_id): str(expected_position)},
        "actual_positions": {str(asset_id): str(after_position)},
        "fee": format(fee, "f"),
        "fee_source": fee_source,
        "tolerance": "0.00001",
    }


def _fill_class(status: Any, size: Any) -> str:
    value = str(status or "").upper()
    amount = _decimal(size)
    if value in {"FILLED", "FULL", "MATCHED", "CONFIRMED"} and amount > 0:
        return "FULL"
    if value in {"PARTIAL", "PARTIAL_FILLED"} and amount > 0:
        return "PARTIAL"
    if value in {"REJECTED", "HTTP_REJECTED"}:
        return "REJECT"
    if value in {"FAILED", "NO_FILL", "UNMATCHED", "CANCELED", "CANCELLED"} or amount == 0:
        return "NO_FILL"
    return "UNKNOWN"


def _event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    return {**dict(payload), "event_type": event.get("event_type"), "_source": event.get("source")}


def _is_user_event(event: Mapping[str, Any]) -> bool:
    return str(event.get("source") or "").startswith("polymarket-user-ws")


def _event_kind(event: Mapping[str, Any]) -> str:
    return str(event.get("event_type") or event.get("type") or "").upper()


def _values(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value)
    return (str(value),) if value not in (None, "") else ()


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else default
    except Exception:
        return default


def _reconciliation_event(row: Mapping[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    identity = {
        "probe_id": row["probe_id"],
        "state": row["probe_state"],
        "artifact_mask": (row.get("artifact_bitmap") or {}).get("mask"),
    }
    return {
        "event_key": payload_hash(identity, prefix="reconcile-"),
        "probe_id": row["probe_id"],
        "event_type": "RECONCILIATION",
        "source": "taker-calibration-reconciler",
        "event_ts": now,
        "payload": identity,
    }


def _counts(values: Sequence[str] | Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = reconcile_run(CalibrationStore(), args.run_id)
    except Exception as exc:
        payload = {"status": "FAIL", "run_id": args.run_id, "reason": f"{exc.__class__.__name__}:{exc}"}
    rendered = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered, encoding="utf-8")
    print(rendered, end="", flush=True)
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
