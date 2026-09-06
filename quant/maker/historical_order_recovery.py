"""Read-only recovery of authenticated historical Maker order outcomes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant.calibration.calibration_domain import payload_hash
from quant.calibration.order_rest_reconciler import reconcile_order_lifecycle
from quant.maker.live_probe_runner import (
    actual_outcome,
    maker_finality_label,
    maker_outcome_observation,
)
from quant.maker.own_order_truth import (
    OwnOrderFilledTruthReconciler,
    trade_transaction_hashes,
)


class HistoricalMakerOrderRecovery:
    """Recover own-order labels without submitting or cancelling any order."""

    def __init__(
        self,
        *,
        adapter: Any,
        maker_address: str,
        output_dir: Path | str,
        onchain_truth_reconciler: OwnOrderFilledTruthReconciler | None = None,
        local_evidence_roots: Iterable[Path | str] = (),
    ) -> None:
        self.adapter = adapter
        self.maker_address = str(maker_address).lower()
        self.output_dir = Path(output_dir)
        self.onchain_truth_reconciler = (
            onchain_truth_reconciler or OwnOrderFilledTruthReconciler()
        )
        self.local_evidence_roots = tuple(Path(path) for path in local_evidence_roots)

    def recover(
        self,
        *,
        after: datetime,
        before: datetime,
        max_orders: int = 500,
    ) -> dict[str, Any]:
        start = _utc(after)
        end = _utc(before)
        if end <= start:
            raise ValueError("historical recovery end must be after start")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        trades = self.adapter.get_authenticated_trades(
            maker_address=self.maker_address,
            after=int(start.timestamp()),
            before=int(end.timestamp()),
        )
        raw_path = self.output_dir / "authenticated-trades.json"
        _write_json(raw_path, {"trades": trades})
        local = discover_local_maker_orders(self.local_evidence_roots)
        seeds = _trade_order_seeds(trades, maker_address=self.maker_address)
        for order_id, row in local.items():
            seeds.setdefault(order_id, {}).update(
                {key: value for key, value in row.items() if value not in (None, "")}
            )
        ordered_seeds = sorted(
            seeds.items(),
            key=lambda item: str(item[1].get("submitted_at") or ""),
            reverse=True,
        )[: max(0, int(max_orders))]
        results: list[dict[str, Any]] = []
        for order_id, seed in ordered_seeds:
            results.append(
                self._recover_one(
                    order_id=order_id,
                    seed=seed,
                    window_start=start,
                    window_end=end,
                )
            )
        order_dir = self.output_dir / "orders"
        order_dir.mkdir(parents=True, exist_ok=True)
        manifest: list[dict[str, Any]] = []
        for row in results:
            path = order_dir / f"{_safe_order_name(row['order_id'])}.json"
            _write_json(path, row)
            manifest.append(
                {
                    "order_id": row["order_id"],
                    "path": str(path),
                    "sha256": _sha256(path),
                    "actual_outcome": row.get("actual_outcome"),
                    "historical_truth_eligible": row.get(
                        "historical_truth_eligible"
                    ),
                    "prospective_calibration_eligible": row.get(
                        "prospective_calibration_eligible"
                    ),
                }
            )
        counts = _counts(results, "actual_outcome")
        report = {
            "schema_version": "historical_maker_order_recovery_v1",
            "status": "PASS_READ_ONLY",
            "maker_address": self.maker_address,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "exchange_submit_called": False,
            "exchange_cancel_called": False,
            "authenticated_trade_count": len(trades),
            "discovered_order_count": len(seeds),
            "recovered_order_count": len(results),
            "outcome_counts": counts,
            "historical_truth_eligible_count": sum(
                bool(row.get("historical_truth_eligible")) for row in results
            ),
            "prospective_calibration_eligible_count": sum(
                bool(row.get("prospective_calibration_eligible")) for row in results
            ),
            "raw_trades": {
                "path": str(raw_path),
                "sha256": _sha256(raw_path),
            },
            "manifest": manifest,
            "limitations": [
                "orders without original_size cannot be classified PARTIAL versus FULL",
                "orders without a pre-submit prediction snapshot cannot calibrate a forecast",
                "public chain fills do not provide the missing NO_FILL denominator",
            ],
        }
        _write_json(self.output_dir / "summary.json", report)
        return report

    def _recover_one(
        self,
        *,
        order_id: str,
        seed: Mapping[str, Any],
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        asset_id = str(seed.get("asset_id") or "")
        condition_id = str(seed.get("condition_id") or seed.get("market") or "")
        base = {
            "schema_version": "historical_maker_order_truth_v1",
            "order_id": str(order_id),
            "asset_id": asset_id,
            "condition_id": condition_id,
            "source_evidence": dict(seed),
            "exchange_submit_called": False,
            "exchange_cancel_called": False,
            "recovered_at": datetime.now(timezone.utc).isoformat(),
        }
        if not asset_id or not condition_id:
            return {
                **base,
                "status": "INSUFFICIENT_ORDER_IDENTITY",
                "actual_outcome": "UNKNOWN",
                "historical_truth_eligible": False,
                "prospective_calibration_eligible": False,
            }
        submitted_at = _timestamp_or_none(seed.get("submitted_at")) or window_start
        rest = self.adapter.get_order_reconciliation_snapshot(
            order_id=str(order_id),
            condition_id=condition_id,
            asset_id=asset_id,
            after=int((submitted_at - timedelta(minutes=2)).timestamp()),
            before=int(window_end.timestamp()) + 120,
        )
        local_rest = (
            seed.get("rest_reconciliation")
            if isinstance(seed.get("rest_reconciliation"), Mapping)
            else {}
        )
        if not rest.get("order") and local_rest.get("order"):
            rest = {
                **dict(local_rest),
                "current_lookup": rest,
                "recovery_source": "LOCAL_HASHED_PROBE_ARTIFACT",
            }
        local_events = _load_local_user_ws_events(seed)
        truth = reconcile_order_lifecycle(
            order_id=str(order_id),
            user_ws_events=local_events,
            rest_order=rest.get("order"),
            rest_trades=rest.get("trades") or (),
        )
        order = rest.get("order") if isinstance(rest.get("order"), Mapping) else {}
        original_size = _first_decimal(
            order.get("original_size"),
            seed.get("original_size"),
            (seed.get("intent") or {}).get("size")
            if isinstance(seed.get("intent"), Mapping)
            else None,
            seed.get("size"),
        )
        matched_size = _decimal(truth.get("actual_matched_size"))
        open_order = any(
            str(row.get("id") or row.get("orderID") or row.get("order_id") or "").lower()
            == str(order_id).lower()
            for row in rest.get("open_orders") or ()
            if isinstance(row, Mapping)
        )
        terminal = not open_order and bool(
            order or rest.get("order_lookup_error") == "HTTP_404_ORDER_NOT_FOUND"
        )
        venue_acceptance_observed = _venue_acceptance_observed(
            order=order,
            user_ws_events=local_events,
            order_id=str(order_id),
        )
        outcome = (
            actual_outcome(matched_size, original_size)
            if original_size is not None and terminal
            else "OPEN_PARTIAL"
            if matched_size > 0 and open_order
            else "UNKNOWN"
        )
        try:
            onchain = self.onchain_truth_reconciler.reconcile(
                order_id=str(order_id),
                asset_id=asset_id,
                expected_matched_size=matched_size,
                transaction_hashes=trade_transaction_hashes(
                    rest.get("trades") or ()
                ),
                window_start=submitted_at - timedelta(minutes=2),
                window_end=window_end + timedelta(minutes=2),
            )
        except Exception as exc:  # noqa: BLE001
            onchain = {
                "status": "SOURCE_UNAVAILABLE",
                "error": f"{exc.__class__.__name__}:{str(exc)[:500]}",
            }
        finality = maker_finality_label(
            matched_size=matched_size,
            orderfilled=onchain,
        )
        outcome_observation = (
            maker_outcome_observation(
                outcome=outcome,
                matched_size=matched_size,
                requested_size=original_size,
                resting_seconds=_decimal(seed.get("resting_seconds")),
                capture=(
                    seed.get("user_ws_capture")
                    if isinstance(seed.get("user_ws_capture"), Mapping)
                    else {}
                ),
            )
            if original_size is not None and outcome in {"NO_FILL", "PARTIAL", "FULL"}
            else None
        )
        prediction_snapshot = (
            seed.get("prediction_snapshot")
            if isinstance(seed.get("prediction_snapshot"), Mapping)
            else None
        )
        prediction_hash = str(seed.get("prediction_snapshot_hash") or "")
        prediction_hash_valid = bool(
            prediction_snapshot
            and prediction_hash
            and prediction_hash
            == payload_hash(prediction_snapshot, prefix="maker-prediction-")
        )
        truth_eligible = bool(
            terminal
            and venue_acceptance_observed
            and original_size is not None
            and outcome in {"NO_FILL", "PARTIAL", "FULL"}
            and (matched_size <= 0 or finality["label"] == "CONFIRMED")
            and (
                matched_size <= 0
                or str(truth.get("liquidity_role_truth") or "").upper() == "MAKER"
            )
        )
        prospective = bool(
            truth_eligible
            and prediction_hash_valid
            and prediction_snapshot.get("book_checkpoint_id")
            and prediction_snapshot.get("frozen_at")
            and _timestamp_or_none(prediction_snapshot.get("frozen_at"))
            <= submitted_at
        )
        return {
            **base,
            "status": "RECOVERED" if truth_eligible else "RECOVERED_INCOMPLETE",
            "submitted_at": submitted_at.isoformat(),
            "rest_reconciliation": rest,
            "truth": truth,
            "onchain_orderfilled": onchain,
            "finality_label": finality,
            "outcome_observation": outcome_observation,
            "order_terminal": terminal,
            "venue_acceptance_observed": venue_acceptance_observed,
            "original_size": (
                format(original_size, "f") if original_size is not None else None
            ),
            "actual_matched_size": format(matched_size, "f"),
            "actual_outcome": outcome,
            "prediction_snapshot": prediction_snapshot,
            "prediction_snapshot_hash": prediction_hash or None,
            "prediction_snapshot_hash_valid": prediction_hash_valid,
            "historical_truth_eligible": truth_eligible,
            "prospective_calibration_eligible": prospective,
        }


def discover_local_maker_orders(
    roots: Iterable[Path | str],
) -> dict[str, dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    for root_value in roots:
        root = Path(root_value)
        paths = [root] if root.is_file() else sorted(root.glob("*.json")) if root.is_dir() else []
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            order_id = _local_order_id(payload)
            if not order_id:
                continue
            row = discovered.setdefault(order_id, {})
            candidate = (
                payload.get("candidate")
                if isinstance(payload.get("candidate"), Mapping)
                else {}
            )
            capture = (
                payload.get("user_ws_capture")
                if isinstance(payload.get("user_ws_capture"), Mapping)
                else {}
            )
            row.update(
                {
                    "asset_id": payload.get("asset_id") or row.get("asset_id"),
                    "condition_id": payload.get("condition_id")
                    or candidate.get("condition_id")
                    or row.get("condition_id"),
                    "submitted_at": payload.get("submitted_at")
                    or capture.get("submitted_at")
                    or row.get("submitted_at"),
                    "size": payload.get("size") or row.get("size"),
                    "resting_seconds": payload.get("resting_seconds")
                    or row.get("resting_seconds"),
                    "intent": payload.get("intent") or row.get("intent"),
                    "prediction_snapshot": payload.get("prediction_snapshot")
                    or row.get("prediction_snapshot"),
                    "prediction_snapshot_hash": payload.get(
                        "prediction_snapshot_hash"
                    )
                    or row.get("prediction_snapshot_hash"),
                    "rest_reconciliation": payload.get("rest_reconciliation")
                    or row.get("rest_reconciliation"),
                    "truth": payload.get("truth") or row.get("truth"),
                    "onchain_orderfilled": payload.get("onchain_orderfilled")
                    or row.get("onchain_orderfilled"),
                    "actual_outcome": payload.get("actual_outcome")
                    or row.get("actual_outcome"),
                    "artifact_complete": payload.get("artifact_complete")
                    if payload.get("artifact_complete") is not None
                    else row.get("artifact_complete"),
                    "user_ws_capture": payload.get("user_ws_capture")
                    or row.get("user_ws_capture"),
                    "journal_path": capture.get("journal_path")
                    or row.get("journal_path"),
                    "local_evidence_path": str(path),
                    "local_evidence_sha256": _sha256(path),
                }
            )
    return discovered


def _trade_order_seeds(
    trades: Iterable[Mapping[str, Any]], *, maker_address: str
) -> dict[str, dict[str, Any]]:
    expected = str(maker_address).lower()
    seeds: dict[str, dict[str, Any]] = {}
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        if str(trade.get("trader_side") or "").upper() != "MAKER":
            continue
        maker_orders = trade.get("maker_orders")
        candidates = maker_orders if isinstance(maker_orders, list) else []
        for maker_order in candidates:
            if not isinstance(maker_order, Mapping):
                continue
            address = str(
                maker_order.get("maker_address") or trade.get("maker_address") or ""
            ).lower()
            if address and address != expected:
                continue
            order_id = str(maker_order.get("order_id") or "").lower()
            if not order_id:
                continue
            seed = seeds.setdefault(order_id, {})
            seed.update(
                {
                    "asset_id": maker_order.get("asset_id") or trade.get("asset_id"),
                    "condition_id": trade.get("market"),
                    "market": trade.get("market"),
                    "submitted_at": _trade_time(trade),
                    "source_trade_ids": sorted(
                        set(seed.get("source_trade_ids") or ())
                        | {str(trade.get("id") or "")}
                    ),
                }
            )
    return seeds


def _load_local_user_ws_events(seed: Mapping[str, Any]) -> list[dict[str, Any]]:
    capture = seed.get("user_ws_capture")
    if isinstance(capture, Mapping):
        events = capture.get("events")
        if isinstance(events, list):
            direct = [
                dict(row.get("payload") or {})
                for row in events
                if isinstance(row, Mapping)
                and isinstance(row.get("payload"), Mapping)
            ]
            if direct:
                return direct
    path_value = seed.get("journal_path")
    if not path_value:
        return []
    path = Path(str(path_value))
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, Mapping) and isinstance(row.get("payload"), Mapping):
            rows.append(dict(row["payload"]))
    return rows


def _venue_acceptance_observed(
    *,
    order: Mapping[str, Any],
    user_ws_events: Iterable[Mapping[str, Any]],
    order_id: str,
) -> bool:
    expected = str(order_id).lower()
    accepted_statuses = {"LIVE", "MATCHED", "CANCELED"}
    order_status = str(order.get("status") or "").upper()
    order_identity = str(
        order.get("id") or order.get("orderID") or order.get("order_id") or ""
    ).lower()
    if order_identity == expected and order_status in accepted_statuses:
        return True
    return any(
        str(event.get("id") or event.get("order_id") or "").lower() == expected
        and str(event.get("status") or "").upper() in accepted_statuses
        for event in user_ws_events
        if isinstance(event, Mapping)
    )


def _local_order_id(payload: Mapping[str, Any]) -> str:
    submission = payload.get("submission")
    submission_id = (
        submission.get("orderID") or submission.get("order_id")
        if isinstance(submission, Mapping)
        else None
    )
    return str(payload.get("order_id") or submission_id or "").lower()


def _trade_time(trade: Mapping[str, Any]) -> str | None:
    value = trade.get("match_time_nano") or trade.get("match_time")
    if value in (None, ""):
        return None
    try:
        raw = Decimal(str(value))
        seconds = raw / Decimal(1000000000) if raw > Decimal(1000000000000) else raw
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc).isoformat()
    except (InvalidOperation, OSError, OverflowError, ValueError):
        return None


def _first_decimal(*values: Any) -> Decimal | None:
    for value in values:
        if value in (None, ""):
            continue
        parsed = _decimal(value)
        if parsed > 0:
            return parsed
    return None


def _decimal(value: Any) -> Decimal:
    try:
        return max(Decimal(0), Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _timestamp_or_none(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _counts(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "UNKNOWN")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _safe_order_name(order_id: Any) -> str:
    text = "".join(char for char in str(order_id).lower() if char.isalnum())
    return text[:96] or hashlib.sha256(str(order_id).encode()).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
