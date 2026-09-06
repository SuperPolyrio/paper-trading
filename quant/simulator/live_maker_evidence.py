"""Build strict LIVE Maker NO_FILL evidence from an official probe artifact."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.execution.models.fee_rebate import (
    RebateRecord,
    RebateState,
    transition_rebate,
)
from quant.simulator.operation_fidelity import load_operation_evidence


def validate_live_maker_probe(probe: Mapping[str, Any]) -> str:
    """Return the authenticated terminal outcome or fail closed."""

    outcome = str(probe.get("actual_outcome") or "").upper()
    if outcome == "NO_FILL":
        _validate_no_fill_probe(probe)
        return outcome
    if outcome in {"PARTIAL", "FULL"}:
        return _validate_fill_probe(probe)
    raise ValueError("probe has no authenticated terminal Maker outcome")


def build_live_maker_no_fill_evidence(
    *, probe_path: Path | str, output_root: Path | str
) -> dict[str, Any]:
    source = Path(probe_path).resolve()
    probe = _read_json(source)
    _validate_no_fill_probe(probe)
    run_id = str(probe["run_id"])
    order_id = str(probe["order_id"])
    asset_id = str(probe["asset_id"])
    root = Path(output_root).resolve() / f"live-maker-no-fill-{run_id}"
    _require(not root.exists(), f"immutable evidence output exists: {root}")
    official = root / "official"
    official.mkdir(parents=True)

    probe_hash = _copy(source, official / "probe.json")
    user_ws = _mapping(probe.get("user_ws_capture"))
    order_trade = {
        "schema_version": "live-maker-order-trade-source-v1",
        "order_id": order_id,
        "submission": dict(_mapping(probe.get("submission"))),
        "cancellation": dict(_mapping(probe.get("cancellation"))),
        "rest_reconciliation": dict(_mapping(probe.get("rest_reconciliation"))),
        "truth": dict(_mapping(probe.get("truth"))),
    }
    balances = {
        "schema_version": "live-maker-account-balance-source-v1",
        "asset_id": asset_id,
        "before": dict(_mapping(probe.get("account_before"))),
        "after": dict(_mapping(probe.get("account_after"))),
        "delta": dict(_mapping(probe.get("account_delta"))),
    }
    _write_json(official / "user-ws.json", user_ws)
    _write_json(official / "order-trades.json", order_trade)
    _write_json(official / "account-balances.json", balances)
    user_ws_hash = _sha256(official / "user-ws.json")
    order_trade_hash = _sha256(official / "order-trades.json")
    balances_hash = _sha256(official / "account-balances.json")
    evidence_files = {
        "official/probe.json": probe_hash,
        "official/user-ws.json": user_ws_hash,
        "official/order-trades.json": order_trade_hash,
        "official/account-balances.json": balances_hash,
    }

    account_before = _mapping(probe.get("account_before"))
    account_after = _mapping(probe.get("account_after"))
    before_cash = _nested_decimal(account_before, "collateral", "balance")
    after_cash = _nested_decimal(account_after, "collateral", "balance")
    before_tokens = _nested_decimal(account_before, "conditional", "balance")
    after_tokens = _nested_decimal(account_after, "conditional", "balance")
    scenario_id = f"live-maker-no-fill-{run_id}"
    before = {
        "schema_version": "paper-operation-before-v1",
        "scenario_id": scenario_id,
        "operation_type": "MAKER",
        "cash_balance": format(before_cash, "f"),
        "positions": {asset_id: format(before_tokens, "f")},
        "order_id": order_id,
        "model_prediction": dict(
            _mapping(
                _mapping(probe.get("model_predictions")).get("PROBABILISTIC_QUEUE")
            )
        ),
    }
    terminal = {
        "cash_balance": format(after_cash, "f"),
        "positions": {asset_id: format(after_tokens, "f")},
    }
    event = {
        "event_key": f"maker-order:{order_id}",
        "event_type": "MAKER_ORDER",
        "state": "CANCELED",
        "order_type": str(_mapping(probe.get("intent")).get("order_type") or "GTC"),
        "liquidity_role": "MAKER",
        "execution_class": "NO_FILL",
        "filled_size": "0",
        "average_price": "0",
        "filled_notional": "0",
        "cash_delta": "0",
        "fee": "0",
        "realized_pnl_delta": "0",
        "token_deltas": {},
    }
    real = {
        "schema_version": "paper-operation-truth-v1",
        "scenario_id": scenario_id,
        "operation_type": "MAKER",
        "evidence_mode": "LIVE",
        "events": [event],
        "after": terminal,
        "source_manifest": {
            "probe_sha256": probe_hash,
            "user_ws_payload_hash": user_ws_hash,
            "order_trade_ids_sha256": order_trade_hash,
            "account_balance_payload_hash": balances_hash,
            "evidence_files": evidence_files,
        },
    }
    paper = {
        "schema_version": "paper-operation-paper-v1",
        "scenario_id": scenario_id,
        "operation_type": "MAKER",
        "events": [event],
        "after": {
            "cash_balance": format(before_cash, "f"),
            "positions": {asset_id: format(before_tokens, "f")},
        },
        "duplicate_replay_ignored": _duplicate_no_fill_is_idempotent(event),
    }
    rules = {
        "schema_version": "paper-operation-rules-v1",
        "tolerance": "0.000001",
        "live_capabilities": ["MAKER_NO_FILL"],
        "required_live_evidence": [
            "source_manifest.user_ws_payload_hash",
            "source_manifest.order_trade_ids_sha256",
            "source_manifest.account_balance_payload_hash",
        ],
    }
    for name, payload in (
        ("before", before),
        ("real", real),
        ("paper", paper),
        ("rules", rules),
    ):
        _write_json(root / f"{name}.json", payload)
    bundle = load_operation_evidence(root)
    _require(
        bundle.reconciliation.get("status") == "PASS",
        "Maker NO_FILL reconciliation failed",
    )
    _write_json(root / "reconciliation.json", bundle.reconciliation)
    files = {
        str(path.relative_to(root)): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    _write_json(
        root / "manifest.json",
        {
            "schema_version": "paper-operation-evidence-manifest-v1",
            "scenario_id": scenario_id,
            "operation_type": "MAKER",
            "status": "PASS",
            "live_capabilities": ["MAKER_NO_FILL"],
            "files": files,
        },
    )
    return {
        "status": "PASS",
        "operation_type": "MAKER",
        "live_capabilities": ["MAKER_NO_FILL"],
        "directory": str(root),
        "run_id": run_id,
        "order_id": order_id,
        "manifest_sha256": _sha256(root / "manifest.json"),
    }


def build_live_maker_fill_evidence(
    *, probe_path: Path | str, output_root: Path | str
) -> dict[str, Any]:
    source = Path(probe_path).resolve()
    probe = _read_json(source)
    outcome = _validate_fill_probe(probe)
    run_id = str(probe["run_id"])
    order_id = str(probe["order_id"])
    asset_id = str(probe["asset_id"])
    root = Path(output_root).resolve() / f"live-maker-{outcome.lower()}-{run_id}"
    _require(not root.exists(), f"immutable evidence output exists: {root}")
    official = root / "official"
    official.mkdir(parents=True)

    probe_hash = _copy(source, official / "probe.json")
    user_ws = _mapping(probe.get("user_ws_capture"))
    order_trade = {
        "schema_version": "live-maker-order-trade-source-v1",
        "order_id": order_id,
        "submission": dict(_mapping(probe.get("submission"))),
        "rest_reconciliation": dict(_mapping(probe.get("rest_reconciliation"))),
        "truth": dict(_mapping(probe.get("truth"))),
    }
    balances = {
        "schema_version": "live-maker-position-source-v1",
        "asset_id": asset_id,
        "before": dict(_mapping(probe.get("account_before"))),
        "after": dict(_mapping(probe.get("account_after"))),
        "delta": dict(_mapping(probe.get("account_delta"))),
    }
    orderfilled = dict(_mapping(probe.get("onchain_orderfilled")))
    _write_json(official / "user-ws.json", user_ws)
    _write_json(official / "order-trades.json", order_trade)
    _write_json(official / "positions.json", balances)
    _write_json(official / "orderfilled.json", orderfilled)
    user_ws_hash = _sha256(official / "user-ws.json")
    order_trade_hash = _sha256(official / "order-trades.json")
    positions_hash = _sha256(official / "positions.json")
    orderfilled_hash = _sha256(official / "orderfilled.json")
    evidence_files = {
        "official/probe.json": probe_hash,
        "official/user-ws.json": user_ws_hash,
        "official/order-trades.json": order_trade_hash,
        "official/positions.json": positions_hash,
        "official/orderfilled.json": orderfilled_hash,
    }

    truth = _mapping(probe.get("truth"))
    intent = _mapping(probe.get("intent"))
    side = str(intent.get("side") or "").upper()
    filled_size = _decimal(truth.get("actual_matched_size"))
    average_price = _decimal(truth.get("actual_avg_price"))
    filled_notional = _decimal(truth.get("actual_quote_amount"))
    fee = _decimal(truth.get("actual_fee"))
    cash_delta = -filled_notional - fee if side == "BUY" else filled_notional - fee
    token_delta = filled_size if side == "BUY" else -filled_size
    account_before = _mapping(probe.get("account_before"))
    account_after = _mapping(probe.get("account_after"))
    before_cash = _nested_decimal(account_before, "collateral", "balance")
    after_cash = _nested_decimal(account_after, "collateral", "balance")
    before_tokens = _nested_decimal(account_before, "conditional", "balance")
    after_tokens = _nested_decimal(account_after, "conditional", "balance")
    tolerance = Decimal("0.000001")
    _require(
        abs((after_cash - before_cash) - cash_delta) <= tolerance,
        "official cash delta does not match Maker fills",
    )
    _require(
        abs((after_tokens - before_tokens) - token_delta) <= tolerance,
        "official token delta does not match Maker fills",
    )

    scenario_id = f"live-maker-{outcome.lower()}-{run_id}"
    before = {
        "schema_version": "paper-operation-before-v1",
        "scenario_id": scenario_id,
        "operation_type": "MAKER",
        "cash_balance": format(before_cash, "f"),
        "positions": {asset_id: format(before_tokens, "f")},
        "order_id": order_id,
    }
    after = {
        "cash_balance": format(after_cash, "f"),
        "positions": {asset_id: format(after_tokens, "f")},
    }
    event = {
        "event_key": f"maker-fill:{order_id}:{outcome.lower()}",
        "event_type": side,
        "state": outcome,
        "order_type": str(intent.get("order_type") or "GTC"),
        "liquidity_role": "MAKER",
        "execution_class": outcome,
        "filled_size": format(filled_size, "f"),
        "average_price": format(average_price, "f"),
        "filled_notional": format(filled_notional, "f"),
        "cash_delta": format(cash_delta, "f"),
        "fee": format(fee, "f"),
        "realized_pnl_delta": "0",
        "token_deltas": {asset_id: format(token_delta, "f")},
    }
    source_manifest = {
        "probe_sha256": probe_hash,
        "user_ws_payload_hash": user_ws_hash,
        "order_trade_ids_sha256": order_trade_hash,
        "positions_payload_hash": positions_hash,
        "orderfilled_payload_hash": orderfilled_hash,
        "evidence_files": evidence_files,
    }
    real = {
        "schema_version": "paper-operation-truth-v1",
        "scenario_id": scenario_id,
        "operation_type": "MAKER",
        "evidence_mode": "LIVE",
        "events": [event],
        "after": after,
        "source_manifest": source_manifest,
    }
    paper_after = {
        "cash_balance": format(before_cash + cash_delta, "f"),
        "positions": {asset_id: format(before_tokens + token_delta, "f")},
    }
    paper = {
        "schema_version": "paper-operation-paper-v1",
        "scenario_id": scenario_id,
        "operation_type": "MAKER",
        "events": [event],
        "after": paper_after,
        "duplicate_replay_ignored": _duplicate_event_is_idempotent(event),
    }
    capability = f"MAKER_{outcome}"
    rules = {
        "schema_version": "paper-operation-rules-v1",
        "tolerance": format(tolerance, "f"),
        "live_capabilities": [capability],
        "required_live_evidence": [
            "source_manifest.user_ws_payload_hash",
            "source_manifest.order_trade_ids_sha256",
            "source_manifest.positions_payload_hash",
            "source_manifest.orderfilled_payload_hash",
        ],
    }
    return _write_bundle(
        root=root,
        scenario_id=scenario_id,
        operation_type="MAKER",
        capability=capability,
        before=before,
        real=real,
        paper=paper,
        rules=rules,
        extra={"run_id": run_id, "order_id": order_id},
    )


def build_live_maker_rebate_evidence(
    *, payout_path: Path | str, output_root: Path | str
) -> dict[str, Any]:
    source = Path(payout_path).resolve()
    payout = _read_json(source)
    source_event_id, amount = _validate_rebate_payout(payout)
    root = (
        Path(output_root).resolve() / f"live-maker-rebate-{_safe_name(source_event_id)}"
    )
    _require(not root.exists(), f"immutable evidence output exists: {root}")
    official = root / "official"
    official.mkdir(parents=True)
    payout_hash = _copy(source, official / "maker-rebate-payout.json")
    evidence_files = {"official/maker-rebate-payout.json": payout_hash}

    record = RebateRecord(
        rebate_id=source_event_id,
        rebate_type="MAKER_REBATE",
        schedule_version=str(payout.get("rule_version") or "official"),
        amount=amount,
        state=RebateState.ESTIMATED,
        estimated_at=_now(),
    )
    accrued = transition_rebate(record, RebateState.ACCRUED, at=_now())
    received = transition_rebate(accrued, RebateState.RECEIVED, at=_now())
    scenario_id = f"live-maker-rebate-{_safe_name(source_event_id)}"
    event = {
        "event_key": f"maker-rebate:{source_event_id}",
        "event_type": "MAKER_REBATE",
        "state": "RECEIVED",
        "cash_delta": format(amount, "f"),
        "fee": "0",
        "realized_pnl_delta": "0",
        "token_deltas": {},
    }
    before = {
        "schema_version": "paper-operation-before-v1",
        "scenario_id": scenario_id,
        "operation_type": "MAKER",
        "confirmed_rebate_cash": "0",
    }
    after = {"confirmed_rebate_cash": format(amount, "f")}
    real = {
        "schema_version": "paper-operation-truth-v1",
        "scenario_id": scenario_id,
        "operation_type": "MAKER",
        "evidence_mode": "LIVE",
        "events": [event],
        "after": after,
        "source_manifest": {
            "rebate_payout_source_id": source_event_id,
            "rebate_payout_payload_hash": payout_hash,
            "evidence_files": evidence_files,
        },
    }
    paper = {
        "schema_version": "paper-operation-paper-v1",
        "scenario_id": scenario_id,
        "operation_type": "MAKER",
        "events": [event],
        "after": {"confirmed_rebate_cash": format(received.confirmed_cash, "f")},
        "duplicate_replay_ignored": _duplicate_event_is_idempotent(event),
    }
    rules = {
        "schema_version": "paper-operation-rules-v1",
        "tolerance": "0.000001",
        "live_capabilities": ["MAKER_REBATE_RECEIVED"],
        "required_live_evidence": [
            "source_manifest.rebate_payout_source_id",
            "source_manifest.rebate_payout_payload_hash",
        ],
    }
    return _write_bundle(
        root=root,
        scenario_id=scenario_id,
        operation_type="MAKER",
        capability="MAKER_REBATE_RECEIVED",
        before=before,
        real=real,
        paper=paper,
        rules=rules,
        extra={"source_event_id": source_event_id, "amount": format(amount, "f")},
    )


def _validate_no_fill_probe(probe: Mapping[str, Any]) -> None:
    _require(probe.get("mode") == "LIVE", "probe is not LIVE")
    _require(probe.get("status") == "CALIBRATABLE", "probe is not calibratable")
    _require(probe.get("actual_outcome") == "NO_FILL", "probe is not NO_FILL")
    _require(bool(probe.get("artifact_complete")), "probe artifact is incomplete")
    _require(
        bool(probe.get("exchange_submit_called")), "exchange submit was not called"
    )
    _require(bool(probe.get("cancel_acknowledged")), "cancel was not acknowledged")
    _require(not bool(probe.get("order_still_open")), "maker order is still open")
    order_id = str(probe.get("order_id") or "").lower()
    _require(order_id.startswith("0x"), "official order ID is missing")
    _validate_post_only_audit(probe)

    truth = _mapping(probe.get("truth"))
    _require(_decimal(truth.get("actual_matched_size")) == 0, "truth contains a fill")
    _require(
        int(truth.get("matched_trade_count") or 0) == 0, "truth contains matched trades"
    )
    _require(bool(truth.get("rest_order_reconciled")), "REST order was not reconciled")
    rest = _mapping(probe.get("rest_reconciliation"))
    order = _mapping(rest.get("order"))
    _require(str(order.get("id") or "").lower() == order_id, "REST order ID mismatch")
    _require(
        str(order.get("status") or "").upper() == "CANCELED",
        "REST order is not canceled",
    )
    _require(_decimal(order.get("size_matched")) == 0, "REST order contains a fill")
    _require(not list(rest.get("trades") or ()), "REST returned maker trades")

    capture = _mapping(probe.get("user_ws_capture"))
    statuses = {
        str(_mapping(row.get("payload")).get("status") or "").upper()
        for row in capture.get("events") or ()
        if isinstance(row, Mapping)
        and str(_mapping(row.get("payload")).get("id") or "").lower() == order_id
    }
    _require({"LIVE", "CANCELED"} <= statuses, "User WS lacks LIVE/CANCELED lifecycle")
    delta = _mapping(probe.get("account_delta"))
    _require(
        _decimal(delta.get("collateral")) == 0, "collateral changed in NO_FILL probe"
    )
    _require(
        _decimal(delta.get("conditional")) == 0,
        "token balance changed in NO_FILL probe",
    )


def _validate_fill_probe(probe: Mapping[str, Any]) -> str:
    _require(probe.get("mode") == "LIVE", "probe is not LIVE")
    _require(probe.get("status") == "CALIBRATABLE", "probe is not calibratable")
    outcome = str(probe.get("actual_outcome") or "").upper()
    _require(outcome in {"PARTIAL", "FULL"}, "probe is not PARTIAL/FULL")
    _require(bool(probe.get("artifact_complete")), "probe artifact is incomplete")
    _require(
        bool(probe.get("exchange_submit_called")), "exchange submit was not called"
    )
    _require(not bool(probe.get("order_still_open")), "maker order is still open")
    _validate_post_only_audit(probe)
    truth = _mapping(probe.get("truth"))
    _require(
        _decimal(truth.get("actual_matched_size")) > 0, "truth has no matched size"
    )
    _require(_decimal(truth.get("actual_avg_price")) > 0, "truth has no average price")
    _require(
        _decimal(truth.get("actual_quote_amount")) > 0, "truth has no quote amount"
    )
    _require(
        str(truth.get("ledger_truth") or "").upper() == "CONFIRMED",
        "Maker trade is not confirmed",
    )
    _require(
        bool(truth.get("rest_trade_reconciled")), "REST trades were not reconciled"
    )
    _require(int(truth.get("matched_trade_count") or 0) > 0, "no official trade rows")
    _require(
        str(truth.get("liquidity_role_truth") or "").upper() == "MAKER",
        "official trades do not prove the probe order was the maker",
    )
    order_id = str(probe.get("order_id") or "").lower()
    onchain = _mapping(probe.get("onchain_orderfilled"))
    _require(
        str(onchain.get("status") or "").upper() == "CONFIRMED_MATCH",
        "Maker fill lacks matching canonical OrderFilled evidence",
    )
    _require(
        str(onchain.get("order_id") or "").lower() == order_id,
        "OrderFilled order ID mismatch",
    )
    _require(
        _decimal(onchain.get("matched_size"))
        == _decimal(truth.get("actual_matched_size")),
        "OrderFilled size differs from authenticated Maker fills",
    )
    _require(
        bool(onchain.get("transaction_coverage_complete")),
        "OrderFilled transaction coverage is incomplete",
    )
    receipt_truth = _mapping(onchain.get("receipt_truth"))
    _require(
        str(receipt_truth.get("status") or "").upper() == "CONFIRMED_SUCCESS"
        and bool(receipt_truth.get("complete")),
        "Maker fill lacks successful Polygon transaction receipts",
    )
    receipt_hashes = {
        str(_mapping(row).get("transaction_hash") or "").lower()
        for row in receipt_truth.get("receipts") or ()
        if isinstance(row, Mapping)
    }
    rest = _mapping(probe.get("rest_reconciliation"))
    rest_hashes = {
        str(
            _mapping(row).get("transaction_hash")
            or _mapping(row).get("tx_hash")
            or ""
        ).lower()
        for row in rest.get("trades") or ()
        if isinstance(row, Mapping)
        and (_mapping(row).get("transaction_hash") or _mapping(row).get("tx_hash"))
    }
    _require(
        bool(rest_hashes) and rest_hashes <= receipt_hashes,
        "Polygon receipt coverage does not include every REST Maker trade",
    )
    market = _mapping(probe.get("market_snapshot"))
    if truth.get("actual_fee") in (None, ""):
        _require(bool(market.get("fee_taker_only")), "Maker fee is unknown")
    _require(
        _decimal(truth.get("actual_fee")) == 0,
        "non-zero Maker fee requires a fee-aware evidence path",
    )
    side = str(_mapping(probe.get("intent")).get("side") or "").upper()
    _require(side in {"BUY", "SELL"}, "Maker side is invalid")
    return outcome


def _validate_post_only_audit(probe: Mapping[str, Any]) -> None:
    intent = _mapping(probe.get("intent"))
    audit = _mapping(probe.get("signed_order_audit"))
    _require(bool(intent.get("post_only")), "probe intent is not post-only")
    _require(
        bool(audit.get("post_only")), "signed submission did not carry postOnly=true"
    )
    _require(
        str(audit.get("order_type") or "").upper() in {"GTC", "GTD"},
        "post-only submission is not GTC/GTD",
    )
    _require(
        bool(audit.get("exchange_submit_called")),
        "signed post-only audit does not prove exchange submission",
    )


def _validate_rebate_payout(payout: Mapping[str, Any]) -> tuple[str, Decimal]:
    reward_type = str(
        payout.get("activity_type")
        or payout.get("reward_type")
        or payout.get("type")
        or ""
    ).upper()
    _require(reward_type == "MAKER_REBATE", "official payout is not MAKER_REBATE")
    state = str(payout.get("state") or payout.get("status") or "").upper()
    _require(state in {"RECEIVED", "CONFIRMED", "PAID"}, "Maker rebate is not received")
    source_event_id = str(
        payout.get("source_event_id") or payout.get("id") or ""
    ).strip()
    _require(bool(source_event_id), "Maker rebate source event ID is missing")
    amount = _decimal(
        payout.get("amount") or payout.get("usdcSize") or payout.get("amount_usd")
    )
    _require(amount > 0, "Maker rebate amount is not positive")
    transaction_hash = str(
        payout.get("transaction_hash") or payout.get("transactionHash") or ""
    )
    _require(
        transaction_hash.startswith("0x"), "Maker rebate transaction hash is missing"
    )
    return source_event_id, amount


def _duplicate_no_fill_is_idempotent(event: Mapping[str, Any]) -> bool:
    applied: set[str] = set()
    state = {"cash_delta": Decimal(0), "token_deltas": {}}
    for _ in range(2):
        key = str(event["event_key"])
        if key in applied:
            continue
        applied.add(key)
        state["cash_delta"] += _decimal(event.get("cash_delta"))
    return len(applied) == 1 and state["cash_delta"] == 0


def _duplicate_event_is_idempotent(event: Mapping[str, Any]) -> bool:
    applied: set[str] = set()
    cash = Decimal(0)
    positions: dict[str, Decimal] = {}
    for _ in range(2):
        key = str(event["event_key"])
        if key in applied:
            continue
        applied.add(key)
        cash += _decimal(event.get("cash_delta"))
        for asset_id, delta in _mapping(event.get("token_deltas")).items():
            positions[str(asset_id)] = positions.get(
                str(asset_id), Decimal(0)
            ) + _decimal(delta)
    return (
        len(applied) == 1
        and cash == _decimal(event.get("cash_delta"))
        and all(
            value == _decimal(_mapping(event.get("token_deltas")).get(asset_id))
            for asset_id, value in positions.items()
        )
    )


def _write_bundle(
    *,
    root: Path,
    scenario_id: str,
    operation_type: str,
    capability: str,
    before: Mapping[str, Any],
    real: Mapping[str, Any],
    paper: Mapping[str, Any],
    rules: Mapping[str, Any],
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    for name, payload in (
        ("before", before),
        ("real", real),
        ("paper", paper),
        ("rules", rules),
    ):
        _write_json(root / f"{name}.json", payload)
    bundle = load_operation_evidence(root)
    _require(
        bundle.reconciliation.get("status") == "PASS",
        f"{capability} reconciliation failed",
    )
    _write_json(root / "reconciliation.json", bundle.reconciliation)
    files = {
        str(path.relative_to(root)): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    _write_json(
        root / "manifest.json",
        {
            "schema_version": "paper-operation-evidence-manifest-v1",
            "scenario_id": scenario_id,
            "operation_type": operation_type,
            "status": "PASS",
            "live_capabilities": [capability],
            "files": files,
        },
    )
    return {
        "status": "PASS",
        "operation_type": operation_type,
        "live_capabilities": [capability],
        "directory": str(root),
        "manifest_sha256": _sha256(root / "manifest.json"),
        **dict(extra),
    }


def _nested_decimal(payload: Mapping[str, Any], section: str, field: str) -> Decimal:
    return _decimal(_mapping(payload.get(section)).get(field))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), f"JSON object required: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, default=str)
        + "\n",
        encoding="utf-8",
    )


def _copy(source: Path, destination: Path) -> str:
    shutil.copyfile(source, destination)
    return _sha256(destination)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _safe_name(value: str) -> str:
    return (
        "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in value
        ).strip("-")
        or "event"
    )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
