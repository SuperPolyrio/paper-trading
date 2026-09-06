"""Independent wallet-account replay from official activities and Polygon receipts.

The mirror is a validation harness. It never writes reconstructed events into the
authoritative Paper ledger and never uses an official terminal position to seed
cash, quantity, or cost basis.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from eth_abi import decode as abi_decode

from quant.adapters.polygon_rpc_client import PolygonRpcClient

from .models import (
    AccountTruthReport,
    OfficialAccountBundle,
    PaperAccountSnapshot,
    PaperPosition,
)

CTF_CONTRACT = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
COLLATERAL_CONTRACTS = frozenset(
    {
        "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",  # pUSD
        "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # legacy USDC.e
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # native USDC
    }
)
ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
ERC1155_TRANSFER_SINGLE_TOPIC = (
    "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
)
ERC1155_TRANSFER_BATCH_TOPIC = (
    "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
)
TOKEN_SCALE = Decimal(1_000_000)
ROUNDING_EPSILON = Decimal("0.000001")
CLOB_V2_ACCOUNTING_CUTOVER = datetime(2026, 4, 28, tzinfo=timezone.utc)
SUPPORTED_ACTIVITY_TYPES = frozenset(
    {
        "TRADE",
        "SPLIT",
        "MERGE",
        "REDEEM",
        "CONVERSION",
        "DEPOSIT",
        "WITHDRAWAL",
        "REWARD",
        "YIELD",
        "MAKER_REBATE",
        "TAKER_REBATE",
        "REFERRAL_REWARD",
    }
)


class ChainActivityMirrorError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfficialActivity:
    source_event_id: str
    account_address: str
    activity_type: str
    event_ts: datetime
    condition_id: str
    asset_id: str
    transaction_hash: str
    raw_payload_hash: str
    raw_payload: Mapping[str, Any]


@dataclass(frozen=True)
class ReceiptEvidence:
    transaction_hash: str
    payload: Mapping[str, Any]
    content_sha256: str
    artifact_path: str
    rpc_source: str


@dataclass(frozen=True)
class WalletTransferDelta:
    transaction_hash: str
    block_number: int
    transaction_index: int
    cash_delta: Decimal
    collateral_deltas: Mapping[str, Decimal]
    token_deltas: Mapping[str, Decimal]


@dataclass(frozen=True)
class ChainActivityMirrorResult:
    account_address: str
    official_run_id: str
    mirror_run_id: str
    snapshot: PaperAccountSnapshot
    summary: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    raw_positions: Mapping[str, Mapping[str, Any]]
    position_comparisons: tuple[Mapping[str, Any], ...]
    receipt_manifest: tuple[Mapping[str, Any], ...]


class ReceiptSource(Protocol):
    def get(self, transaction_hash: str) -> ReceiptEvidence: ...


@dataclass
class _PositionState:
    asset_id: str
    condition_id: str = ""
    quantity: Decimal = Decimal(0)
    fee_exclusive_basis: Decimal = Decimal(0)
    entry_fees: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)

    @property
    def gross_basis(self) -> Decimal:
        return self.fee_exclusive_basis + self.entry_fees


class PolygonReceiptArchive:
    """Hash-verifying receipt cache with explicit ordered RPC fallback."""

    def __init__(
        self,
        *,
        rpc_urls: Sequence[str],
        artifact_root: Path | str,
        timeout_seconds: float = 15.0,
    ) -> None:
        urls = tuple(str(value).strip() for value in rpc_urls if str(value).strip())
        if not urls:
            raise ValueError("at least one Polygon RPC URL is required")
        self.clients = tuple(
            PolygonRpcClient(rpc_url=url, timeout_seconds=timeout_seconds)
            for url in urls
        )
        self.source_labels = tuple(_safe_endpoint_label(url) for url in urls)
        self.artifact_root = Path(artifact_root)

    def get(self, transaction_hash: str) -> ReceiptEvidence:
        tx_hash = _transaction_hash(transaction_hash)
        path = self.artifact_root / f"{tx_hash[2:]}.json"
        if path.exists():
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = _mapping(envelope.get("payload"))
            _validate_receipt_payload(payload, tx_hash)
            content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            return ReceiptEvidence(
                transaction_hash=tx_hash,
                payload=payload,
                content_sha256=content_hash,
                artifact_path=str(path.resolve()),
                rpc_source=str(envelope.get("rpc_source") or "cached"),
            )
        errors: list[str] = []
        for client, source_label in zip(self.clients, self.source_labels, strict=True):
            try:
                payload = client.get_transaction_receipt(tx_hash)
                _validate_receipt_payload(payload, tx_hash)
            except Exception as exc:  # noqa: BLE001 - fallback records every source.
                errors.append(f"{source_label}:{type(exc).__name__}:{exc}")
                continue
            envelope = {
                "schema_version": "polygon-receipt-evidence-v1",
                "transaction_hash": tx_hash,
                "rpc_source": source_label,
                "payload": payload,
            }
            _atomic_json(path, envelope)
            return ReceiptEvidence(
                transaction_hash=tx_hash,
                payload=payload,
                content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                artifact_path=str(path.resolve()),
                rpc_source=source_label,
            )
        raise ChainActivityMirrorError(
            f"all Polygon RPC sources failed for {tx_hash}: {' | '.join(errors)}"
        )


class ChainActivityMirrorService:
    def __init__(
        self,
        *,
        connection_factory: Any,
        receipt_source: ReceiptSource,
        receipt_workers: int = 8,
        official_quantity_tolerance: Decimal = Decimal("0.0001"),
        official_dust_threshold: Decimal = Decimal("0.01"),
    ) -> None:
        self.connection_factory = connection_factory
        self.receipt_source = receipt_source
        self.receipt_workers = max(1, int(receipt_workers))
        self.official_quantity_tolerance = abs(
            Decimal(official_quantity_tolerance)
        )
        self.official_dust_threshold = abs(Decimal(official_dust_threshold))

    def run(self, *, official: OfficialAccountBundle) -> ChainActivityMirrorResult:
        activities = self._load_activities(
            account_address=official.account_address,
            as_of=official.source_as_of,
        )
        transaction_hashes = tuple(
            dict.fromkeys(row.transaction_hash for row in activities)
        )
        with ThreadPoolExecutor(max_workers=self.receipt_workers) as executor:
            evidence = tuple(executor.map(self.receipt_source.get, transaction_hashes))
        receipts = {row.transaction_hash: row for row in evidence}
        asset_ids: set[str] = {
            row.asset_id for row in activities if row.asset_id
        }
        for row in evidence:
            asset_ids.update(
                decode_wallet_transfers(
                    row.payload, account_address=official.account_address
                ).token_deltas
            )
        conditions = self._load_condition_ids(asset_ids)
        return replay_chain_activity(
            official=official,
            activities=activities,
            receipts=receipts,
            condition_ids=conditions,
            official_quantity_tolerance=self.official_quantity_tolerance,
            official_dust_threshold=self.official_dust_threshold,
        )

    def _load_activities(
        self, *, account_address: str, as_of: datetime
    ) -> tuple[OfficialActivity, ...]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_event_id,account_address,activity_type,event_ts,
                       condition_id,asset_id,transaction_hash,raw_payload_hash,
                       raw_payload
                FROM quant.paper_official_account_activities
                WHERE account_address=%s AND event_ts<=%s
                ORDER BY event_ts,source_event_id
                """,
                (account_address.lower(), as_of),
            )
            rows = [dict(row) for row in cur.fetchall()]
        if not rows:
            raise ChainActivityMirrorError("no persisted official activities found")
        activities: list[OfficialActivity] = []
        for row in rows:
            activity_type = str(row["activity_type"] or "").upper()
            if activity_type not in SUPPORTED_ACTIVITY_TYPES:
                raise ChainActivityMirrorError(
                    f"unsupported official activity type: {activity_type}"
                )
            tx_hash = str(row.get("transaction_hash") or "").strip().lower()
            if not tx_hash:
                raise ChainActivityMirrorError(
                    f"activity lacks transaction evidence: {row['source_event_id']}"
                )
            activities.append(
                OfficialActivity(
                    source_event_id=str(row["source_event_id"]),
                    account_address=str(row["account_address"]).lower(),
                    activity_type=activity_type,
                    event_ts=_utc(row["event_ts"]),
                    condition_id=str(row.get("condition_id") or ""),
                    asset_id=str(row.get("asset_id") or ""),
                    transaction_hash=_transaction_hash(tx_hash),
                    raw_payload_hash=str(row["raw_payload_hash"]),
                    raw_payload=dict(row.get("raw_payload") or {}),
                )
            )
        return tuple(activities)

    def _load_condition_ids(self, asset_ids: set[str]) -> dict[str, str]:
        if not asset_ids:
            return {}
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('quant.paper_market_registry_tokens') AS name"
            )
            if cur.fetchone()["name"] is None:
                return {}
            cur.execute(
                """
                SELECT asset_id,condition_id
                FROM quant.paper_market_registry_tokens
                WHERE asset_id=ANY(%s)
                """,
                (list(asset_ids),),
            )
            return {
                str(row["asset_id"]): str(row["condition_id"] or "")
                for row in cur.fetchall()
            }


def decode_wallet_transfers(
    payload: Mapping[str, Any], *, account_address: str
) -> WalletTransferDelta:
    wallet = _address(account_address)
    receipt = _receipt(payload)
    transaction_hash = _transaction_hash(receipt.get("transactionHash"))
    collateral_deltas: defaultdict[str, Decimal] = defaultdict(Decimal)
    token_deltas: defaultdict[str, Decimal] = defaultdict(Decimal)
    logs = receipt.get("logs")
    if not isinstance(logs, Sequence) or isinstance(logs, (str, bytes)):
        raise ChainActivityMirrorError("Polygon receipt logs are invalid")
    for raw_log in logs:
        log = _mapping(raw_log)
        contract = _address(log.get("address"))
        topics_value = log.get("topics")
        if not isinstance(topics_value, Sequence) or isinstance(
            topics_value, (str, bytes)
        ):
            continue
        topics = tuple(str(value).lower() for value in topics_value)
        if not topics:
            continue
        if contract in COLLATERAL_CONTRACTS and topics[0] == ERC20_TRANSFER_TOPIC:
            if len(topics) < 3:
                raise ChainActivityMirrorError("ERC20 Transfer topics are incomplete")
            sender = _topic_address(topics[1])
            receiver = _topic_address(topics[2])
            amount = _scaled_hex(log.get("data"))
            collateral_deltas[contract] += amount * (
                int(receiver == wallet) - int(sender == wallet)
            )
            continue
        if contract != CTF_CONTRACT:
            continue
        if topics[0] == ERC1155_TRANSFER_SINGLE_TOPIC:
            if len(topics) < 4:
                raise ChainActivityMirrorError(
                    "ERC1155 TransferSingle topics are incomplete"
                )
            sender = _topic_address(topics[2])
            receiver = _topic_address(topics[3])
            data = _hex_bytes(log.get("data"))
            if len(data) != 64:
                raise ChainActivityMirrorError(
                    "ERC1155 TransferSingle data must be two words"
                )
            token_id = str(int.from_bytes(data[:32], "big"))
            amount = Decimal(int.from_bytes(data[32:], "big")) / TOKEN_SCALE
            token_deltas[token_id] += amount * (
                int(receiver == wallet) - int(sender == wallet)
            )
        elif topics[0] == ERC1155_TRANSFER_BATCH_TOPIC:
            if len(topics) < 4:
                raise ChainActivityMirrorError(
                    "ERC1155 TransferBatch topics are incomplete"
                )
            sender = _topic_address(topics[2])
            receiver = _topic_address(topics[3])
            try:
                token_ids, amounts = abi_decode(
                    ["uint256[]", "uint256[]"], _hex_bytes(log.get("data"))
                )
            except Exception as exc:  # noqa: BLE001 - normalize ABI failures.
                raise ChainActivityMirrorError(
                    "ERC1155 TransferBatch data is invalid"
                ) from exc
            if len(token_ids) != len(amounts):
                raise ChainActivityMirrorError(
                    "ERC1155 TransferBatch array lengths disagree"
                )
            sign = int(receiver == wallet) - int(sender == wallet)
            for token_id, raw_amount in zip(token_ids, amounts, strict=True):
                token_deltas[str(token_id)] += (
                    Decimal(raw_amount) / TOKEN_SCALE * sign
                )
    clean_collateral = {
        key: value for key, value in sorted(collateral_deltas.items()) if value
    }
    clean_tokens = {key: value for key, value in sorted(token_deltas.items()) if value}
    return WalletTransferDelta(
        transaction_hash=transaction_hash,
        block_number=_hex_int(receipt.get("blockNumber")),
        transaction_index=_hex_int(receipt.get("transactionIndex")),
        cash_delta=sum(clean_collateral.values(), Decimal(0)),
        collateral_deltas=clean_collateral,
        token_deltas=clean_tokens,
    )


def replay_chain_activity(
    *,
    official: OfficialAccountBundle,
    activities: Sequence[OfficialActivity],
    receipts: Mapping[str, ReceiptEvidence],
    condition_ids: Mapping[str, str] | None = None,
    official_quantity_tolerance: Decimal = Decimal("0.0001"),
    official_dust_threshold: Decimal = Decimal("0.01"),
) -> ChainActivityMirrorResult:
    account = _address(official.account_address)
    tolerance = abs(Decimal(official_quantity_tolerance))
    dust_threshold = abs(Decimal(official_dust_threshold))
    condition_map = dict(condition_ids or {})
    condition_map.update(
        {row.asset_id: row.condition_id for row in official.closed_positions}
    )
    condition_map.update({row.asset_id: row.condition_id for row in official.positions})
    grouped: defaultdict[str, list[OfficialActivity]] = defaultdict(list)
    for activity in activities:
        if _address(activity.account_address) != account:
            raise ChainActivityMirrorError("activity belongs to another account")
        grouped[activity.transaction_hash].append(activity)
        if activity.asset_id and activity.condition_id:
            condition_map[activity.asset_id] = activity.condition_id
    if not grouped:
        raise ChainActivityMirrorError("activity replay has no transactions")
    ordered: list[tuple[WalletTransferDelta, OfficialActivity, ReceiptEvidence]] = []
    for tx_hash, rows in grouped.items():
        # Multiple Data API rows for one settlement transaction are ambiguous
        # without a per-row transfer map. Fail closed instead of allocating cash.
        if len(rows) != 1:
            raise ChainActivityMirrorError(
                f"transaction maps to {len(rows)} official activity rows: {tx_hash}"
            )
        evidence = receipts.get(tx_hash)
        if evidence is None:
            raise ChainActivityMirrorError(f"receipt evidence missing: {tx_hash}")
        delta = decode_wallet_transfers(evidence.payload, account_address=account)
        if delta.transaction_hash != tx_hash:
            raise ChainActivityMirrorError("receipt/activity transaction mismatch")
        if not delta.cash_delta and not delta.token_deltas:
            raise ChainActivityMirrorError(
                f"activity transaction has no wallet asset delta: {tx_hash}"
            )
        ordered.append((delta, rows[0], evidence))
    ordered.sort(
        key=lambda item: (
            item[0].block_number,
            item[0].transaction_index,
            item[1].source_event_id,
        )
    )

    states: defaultdict[str, _PositionState] = defaultdict(
        lambda: _PositionState(asset_id="")
    )
    cash_balance = Decimal(0)
    external_capital = Decimal(0)
    reward_income = Decimal(0)
    total_realized = Decimal(0)
    total_entry_fees = Decimal(0)
    total_exit_fees = Decimal(0)
    event_rows: list[Mapping[str, Any]] = []

    def state(asset_id: str) -> _PositionState:
        selected = states[asset_id]
        if not selected.asset_id:
            selected.asset_id = asset_id
            selected.condition_id = condition_map.get(asset_id, "")
        return selected

    for delta, activity, evidence in ordered:
        cash_balance += delta.cash_delta
        event_realized = Decimal(0)
        event_entry_fee = Decimal(0)
        event_exit_fee = Decimal(0)
        activity_type = activity.activity_type
        if activity_type == "TRADE":
            asset_id = activity.asset_id
            if not asset_id or set(delta.token_deltas) != {asset_id}:
                raise ChainActivityMirrorError(
                    f"trade transfer does not match activity asset: {delta.transaction_hash}"
                )
            token_delta = delta.token_deltas[asset_id]
            side = str(activity.raw_payload.get("side") or "").upper()
            price = _positive_decimal(activity.raw_payload.get("price"), "trade price")
            position = state(asset_id)
            if side == "BUY" and token_delta > 0 and delta.cash_delta < 0:
                nominal = token_delta * price
                event_entry_fee = _fee(-delta.cash_delta - nominal)
                position.quantity += token_delta
                position.fee_exclusive_basis += nominal
                position.entry_fees += event_entry_fee
                total_entry_fees += event_entry_fee
            elif side == "SELL" and token_delta < 0 and delta.cash_delta > 0:
                quantity = -token_delta
                removed_basis, removed_fees = _remove(position, quantity)
                nominal = quantity * price
                event_exit_fee = _fee(nominal - delta.cash_delta)
                event_realized = delta.cash_delta - removed_basis - removed_fees
                position.realized_pnl += event_realized
                total_realized += event_realized
                total_exit_fees += event_exit_fee
            else:
                raise ChainActivityMirrorError(
                    f"trade side conflicts with receipt deltas: {delta.transaction_hash}"
                )
        elif activity_type in {"DEPOSIT", "WITHDRAWAL"}:
            expected_sign = 1 if activity_type == "DEPOSIT" else -1
            if delta.token_deltas or delta.cash_delta * expected_sign <= 0:
                raise ChainActivityMirrorError(
                    f"{activity_type} receipt has unexpected deltas: {delta.transaction_hash}"
                )
            external_capital += delta.cash_delta
        elif activity_type == "SPLIT":
            positives = {key: value for key, value in delta.token_deltas.items() if value > 0}
            if len(positives) < 2 or any(
                value < 0 for value in delta.token_deltas.values()
            ) or delta.cash_delta >= 0:
                raise ChainActivityMirrorError(
                    f"split receipt violates complete-set shape: {delta.transaction_hash}"
                )
            total = sum(positives.values(), Decimal(0))
            for asset_id, quantity in positives.items():
                position = state(asset_id)
                position.quantity += quantity
                position.fee_exclusive_basis += -delta.cash_delta * quantity / total
        elif activity_type in {"MERGE", "REDEEM"}:
            negatives = {key: -value for key, value in delta.token_deltas.items() if value < 0}
            if not negatives or any(
                value > 0 for value in delta.token_deltas.values()
            ) or delta.cash_delta < 0:
                raise ChainActivityMirrorError(
                    f"{activity_type} receipt has unexpected deltas: {delta.transaction_hash}"
                )
            removed_by_asset: dict[str, Decimal] = {}
            for asset_id, quantity in negatives.items():
                position = state(asset_id)
                basis, fees = _remove(position, quantity)
                removed_by_asset[asset_id] = basis + fees
            removed_total = sum(removed_by_asset.values(), Decimal(0))
            event_realized = delta.cash_delta - removed_total
            total_realized += event_realized
            _allocate_realized(
                states=states,
                removed_by_asset=removed_by_asset,
                cash_delta=delta.cash_delta,
                official=official,
                allocation_mode=(
                    "BASIS_PROPORTIONAL"
                    if activity_type == "MERGE"
                    else "PAYOUT_WEIGHTED"
                ),
            )
        elif activity_type == "CONVERSION":
            negatives = {key: -value for key, value in delta.token_deltas.items() if value < 0}
            positives = {key: value for key, value in delta.token_deltas.items() if value > 0}
            if not negatives or not positives or abs(delta.cash_delta) > ROUNDING_EPSILON:
                raise ChainActivityMirrorError(
                    f"conversion receipt has unexpected deltas: {delta.transaction_hash}"
                )
            basis_pool = Decimal(0)
            fee_pool = Decimal(0)
            for asset_id, quantity in negatives.items():
                basis, fees = _remove(state(asset_id), quantity)
                basis_pool += basis
                fee_pool += fees
            positive_total = sum(positives.values(), Decimal(0))
            for asset_id, quantity in positives.items():
                position = state(asset_id)
                position.quantity += quantity
                position.fee_exclusive_basis += basis_pool * quantity / positive_total
                position.entry_fees += fee_pool * quantity / positive_total
        elif activity_type in {
            "REWARD",
            "YIELD",
            "MAKER_REBATE",
            "TAKER_REBATE",
            "REFERRAL_REWARD",
        }:
            if delta.token_deltas or delta.cash_delta <= 0:
                raise ChainActivityMirrorError(
                    f"reward receipt has unexpected deltas: {delta.transaction_hash}"
                )
            reward_income += delta.cash_delta
        else:  # pragma: no cover - guarded when loading activities.
            raise ChainActivityMirrorError(f"unsupported activity: {activity_type}")
        event_rows.append(
            {
                "source_event_id": activity.source_event_id,
                "activity_type": activity_type,
                "event_ts": activity.event_ts.isoformat(),
                "transaction_hash": delta.transaction_hash,
                "block_number": delta.block_number,
                "transaction_index": delta.transaction_index,
                "cash_delta": _decimal_text(delta.cash_delta),
                "collateral_deltas": _decimal_mapping(delta.collateral_deltas),
                "token_deltas": _decimal_mapping(delta.token_deltas),
                "entry_fee": _decimal_text(event_entry_fee),
                "exit_fee": _decimal_text(event_exit_fee),
                "realized_pnl_delta": _decimal_text(event_realized),
                "receipt_sha256": evidence.content_sha256,
                "raw_activity_sha256": activity.raw_payload_hash,
            }
        )

    raw_positions = {
        asset_id: {
            "condition_id": position.condition_id,
            "quantity": _decimal_text(position.quantity),
            "fee_exclusive_basis": _decimal_text(position.fee_exclusive_basis),
            "entry_fees": _decimal_text(position.entry_fees),
            "gross_basis": _decimal_text(position.gross_basis),
            "realized_pnl": _decimal_text(position.realized_pnl),
        }
        for asset_id, position in sorted(states.items())
        if position.quantity or position.gross_basis or position.realized_pnl
    }
    official_positions = {row.asset_id: row for row in official.positions}
    positive_chain = {
        asset_id: position
        for asset_id, position in states.items()
        if position.quantity > 0
    }
    comparisons: list[Mapping[str, Any]] = []
    for asset_id in sorted(set(official_positions) | set(positive_chain)):
        official_position = official_positions.get(asset_id)
        chain_position = positive_chain.get(asset_id)
        official_quantity = (
            official_position.size if official_position is not None else Decimal(0)
        )
        chain_quantity = (
            chain_position.quantity if chain_position is not None else Decimal(0)
        )
        difference = chain_quantity - official_quantity
        if abs(difference) <= tolerance:
            status = "MATCH"
        elif official_position is None and Decimal(0) < chain_quantity <= dust_threshold:
            status = "OFFICIAL_API_DUST_OMITTED"
        else:
            status = "MISMATCH"
        comparisons.append(
            {
                "asset_id": asset_id,
                "official_quantity": _decimal_text(official_quantity),
                "chain_quantity": _decimal_text(chain_quantity),
                "delta": _decimal_text(difference),
                "tolerance": _decimal_text(tolerance),
                "status": status,
            }
        )
    cash_delta = cash_balance - official.accounting.equity.cash_balance
    cash_matches = abs(cash_delta) <= ROUNDING_EPSILON
    positions_match = not any(row["status"] == "MISMATCH" for row in comparisons)
    asset_replay_status = "PASS" if cash_matches and positions_match else "FAIL"
    excluded_dust = {
        asset_id: position.quantity
        for asset_id, position in positive_chain.items()
        if asset_id not in official_positions and position.quantity <= dust_threshold
    }
    accounting_positions = {
        row.asset_id: row for row in official.accounting.positions
    }
    snapshot_positions: dict[str, PaperPosition] = {}
    marks_complete = True
    snapshot_states = {
        asset_id: position
        for asset_id, position in states.items()
        if position.quantity > 0 or position.realized_pnl
    }
    for asset_id, position in sorted(snapshot_states.items()):
        surface_omitted = asset_id in excluded_dust
        truth = official_positions.get(asset_id)
        accounting = accounting_positions.get(asset_id)
        mark = (
            accounting.current_price
            if accounting is not None
            else truth.current_price
            if truth is not None
            else None
        )
        if position.quantity > 0 and not surface_omitted:
            marks_complete = marks_complete and mark is not None
        snapshot_positions[asset_id] = PaperPosition(
            asset_id=asset_id,
            condition_id=position.condition_id,
            quantity=position.quantity,
            cost_basis=position.gross_basis,
            entry_fees=position.entry_fees,
            realized_pnl=position.realized_pnl,
            current_value=(
                position.quantity * mark
                if position.quantity > 0
                and mark is not None
                and not surface_omitted
                else Decimal(0)
                if position.quantity == 0
                else None
            ),
            mark_price=mark,
            redeemable=None,
            mergeable=None,
            truth_surface_omitted=surface_omitted,
        )
    marked_value = sum(
        (
            position.current_value
            for position in snapshot_positions.values()
            if position.current_value is not None
        ),
        Decimal(0),
    )
    checkpoint_payload = {
        "account_address": account,
        "official_run_id": official.run_id,
        "events": event_rows,
        "raw_positions": raw_positions,
        "cash_balance": _decimal_text(cash_balance),
    }
    checkpoint = hashlib.sha256(_canonical_bytes(checkpoint_payload)).hexdigest()
    strategy_hash = hashlib.sha256(account.encode()).hexdigest()[:16]
    open_gross_basis = sum(
        (position.gross_basis for position in positive_chain.values()), Decimal(0)
    )
    economic_identity_left = cash_balance + open_gross_basis
    economic_identity_right = external_capital + reward_income + total_realized
    economic_identity_delta = economic_identity_left - economic_identity_right
    economic_conservation_matches = (
        abs(economic_identity_delta) <= ROUNDING_EPSILON
    )
    completeness = {
        "activity_count": len(activities),
        "activity_transaction_count": len(grouped),
        "receipt_count": len(receipts),
        "receipt_coverage": _decimal_text(
            Decimal(len(receipts)) / Decimal(len(grouped))
        ),
        "terminal_cash_match": cash_matches,
        "terminal_position_surface_match": positions_match,
        "economic_conservation_match": economic_conservation_matches,
        "official_api_dust_omitted_count": len(excluded_dust),
        "full_wallet_transfer_enumeration": False,
        "history_source": "PERSISTED_DATA_API_ACTIVITY_PLUS_POLYGON_RECEIPTS",
    }
    snapshot = PaperAccountSnapshot(
        strategy_ids=(f"chain-activity-mirror:{strategy_hash}",),
        as_of=official.source_as_of,
        initial_cash=external_capital,
        cash_balance=cash_balance,
        realized_pnl=total_realized,
        positions=snapshot_positions,
        nav=(cash_balance + marked_value if marks_complete else None),
        unmodeled_cashflows=(),
        provisional_fill_count=0,
        ledger_checkpoint=checkpoint,
        snapshot_source="CHAIN_ACTIVITY_MIRROR",
        source_completeness=completeness,
    )
    mirror_identity = hashlib.sha256(
        _canonical_bytes(
            {
                "official_run_id": official.run_id,
                "ledger_checkpoint": checkpoint,
                "receipt_hashes": sorted(
                    row.content_sha256 for row in receipts.values()
                ),
            }
        )
    ).hexdigest()
    receipt_manifest = tuple(
        {
            "transaction_hash": row.transaction_hash,
            "content_sha256": row.content_sha256,
            "artifact_path": row.artifact_path,
            "rpc_source": row.rpc_source,
        }
        for row in sorted(receipts.values(), key=lambda item: item.transaction_hash)
    )
    summary = {
        "schema_version": "chain-activity-account-mirror-v1",
        "asset_replay_gate": asset_replay_status,
        "account_address": account,
        "official_run_id": official.run_id,
        "as_of": official.source_as_of.isoformat(),
        "activity_count": len(activities),
        "transaction_count": len(grouped),
        "receipt_count": len(receipts),
        "cash": {
            "chain_replayed": _decimal_text(cash_balance),
            "official": _decimal_text(official.accounting.equity.cash_balance),
            "delta": _decimal_text(cash_delta),
            "status": "MATCH" if cash_matches else "MISMATCH",
        },
        "positions": {
            "chain_positive_count": len(positive_chain),
            "official_count": len(official_positions),
            "compared_count": len(comparisons),
            "mismatch_count": sum(row["status"] == "MISMATCH" for row in comparisons),
            "official_api_dust_omitted_count": len(excluded_dust),
        },
        "economics": {
            "external_capital": _decimal_text(external_capital),
            "reward_income": _decimal_text(reward_income),
            "entry_fees": _decimal_text(total_entry_fees),
            "exit_fees": _decimal_text(total_exit_fees),
            "realized_pnl": _decimal_text(total_realized),
            "open_gross_basis": _decimal_text(open_gross_basis),
            "conservation_left_cash_plus_open_basis": _decimal_text(
                economic_identity_left
            ),
            "conservation_right_capital_plus_income_plus_realized": _decimal_text(
                economic_identity_right
            ),
            "conservation_delta": _decimal_text(economic_identity_delta),
            "conservation_status": (
                "PASS" if economic_conservation_matches else "FAIL"
            ),
        },
        "economic_replay_gate": (
            "PASS" if economic_conservation_matches else "FAIL"
        ),
        "source_completeness": completeness,
        "interpretation": (
            "Receipt replay independently validates terminal cash and outcome-token "
            "balances. It does not enumerate unrelated wallet transfers and does not "
            "by itself promote a Paper strategy to live-equivalent PnL."
        ),
    }
    return ChainActivityMirrorResult(
        account_address=account,
        official_run_id=official.run_id,
        mirror_run_id=f"chain-account-mirror:{mirror_identity[:32]}",
        snapshot=snapshot,
        summary=summary,
        events=tuple(event_rows),
        raw_positions=raw_positions,
        position_comparisons=tuple(comparisons),
        receipt_manifest=receipt_manifest,
    )


def write_chain_activity_mirror_report(
    *,
    output_root: Path | str,
    result: ChainActivityMirrorResult,
    official: OfficialAccountBundle,
    account_truth: AccountTruthReport,
    account_truth_paths: Mapping[str, str],
) -> dict[str, str]:
    directory = (
        Path(output_root)
        / account_truth.as_of.date().isoformat()
        / result.mirror_run_id.replace(":", "-")
    )
    directory.mkdir(parents=True, exist_ok=True)
    pnl_contract = dict(account_truth.summary.get("pnl_truth_contract") or {})
    difference_analysis = analyze_chain_account_differences(
        official=official,
        result=result,
        account_truth=account_truth,
    )
    asset_pass = result.summary["asset_replay_gate"] == "PASS"
    economic_pass = result.summary["economic_replay_gate"] == "PASS"
    pnl_pass = pnl_contract.get("status") in {"PASS", "REFERENCE_REPLAY_PASS"}
    overall_status = (
        "PASS"
        if asset_pass and economic_pass and pnl_pass
        else "PARTIAL"
        if asset_pass and economic_pass
        else "FAIL"
    )
    summary = {
        **dict(result.summary),
        "mirror_run_id": result.mirror_run_id,
        "overall_status": overall_status,
        "account_truth_gate": account_truth.status.value,
        "account_truth_reconciliation_id": account_truth.reconciliation_id,
        "pnl_truth_contract": pnl_contract,
        "difference_analysis": difference_analysis["summary"],
        "account_truth_paths": dict(account_truth_paths),
        "ledger_overwritten": False,
        "live_submission_performed": False,
    }
    paths = {
        "summary": directory / "summary.json",
        "events": directory / "events.jsonl",
        "positions": directory / "raw-positions.json",
        "position_comparisons": directory / "position-comparisons.jsonl",
        "receipts": directory / "receipt-manifest.json",
        "difference_analysis": directory / "difference-analysis.json",
    }
    _atomic_json(paths["summary"], summary)
    _atomic_text(
        paths["events"],
        "".join(_canonical_bytes(row).decode() + "\n" for row in result.events),
    )
    _atomic_json(paths["positions"], result.raw_positions)
    _atomic_text(
        paths["position_comparisons"],
        "".join(
            _canonical_bytes(row).decode() + "\n"
            for row in result.position_comparisons
        ),
    )
    _atomic_json(paths["receipts"], result.receipt_manifest)
    _atomic_json(paths["difference_analysis"], difference_analysis)
    latest = Path(output_root) / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for path in paths.values():
        _atomic_text(latest / path.name, path.read_text(encoding="utf-8"))
    return {key: str(path.resolve()) for key, path in paths.items()}


def analyze_chain_account_differences(
    *,
    official: OfficialAccountBundle,
    result: ChainActivityMirrorResult,
    account_truth: AccountTruthReport,
) -> dict[str, Any]:
    """Attribute every mismatch without waiving the official equality gate."""

    events_by_asset: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    conversion_destinations: set[str] = set()
    for event in result.events:
        token_deltas = _mapping(event.get("token_deltas"))
        for asset_id, raw_delta in token_deltas.items():
            events_by_asset[str(asset_id)].append(event)
            if (
                str(event.get("activity_type") or "").upper() == "CONVERSION"
                and Decimal(str(raw_delta)) > 0
            ):
                conversion_destinations.add(str(asset_id))
    legacy_assets = {
        asset_id
        for asset_id, events in events_by_asset.items()
        if min(_event_time(row) for row in events) < CLOB_V2_ACCOUNTING_CUTOVER
    }
    current_positions = {row.asset_id: row for row in official.positions}
    closed_positions = {row.asset_id: row for row in official.closed_positions}
    resolved_unredeemed = {
        row.asset_id for row in official.positions if row.redeemable
    }
    rows: list[dict[str, Any]] = []
    for item in account_truth.mismatches:
        asset_id = item.comparison_key
        if item.retryable:
            category = "CROSS_ENDPOINT_MARK_TIMING"
            confidence = "DIRECT_SOURCE_CONFLICT"
        elif item.comparison_type == "OFFICIAL_VS_PAPER_ACCOUNT":
            category = "ACCOUNT_AGGREGATE_OF_COMPONENT_DIFFERENCES"
            confidence = "DERIVED"
        elif asset_id in legacy_assets:
            category = "LEGACY_PRE_V2_DATA_API_ACCOUNTING"
            confidence = "COHORT_SUPPORTED_NOT_WAIVED"
        elif asset_id in conversion_destinations:
            category = "NEG_RISK_CONVERSION_BASIS_POLICY"
            confidence = "RECEIPT_AND_OPERATION_SUPPORTED_NOT_WAIVED"
        elif asset_id in resolved_unredeemed and item.field_name == "realized_pnl":
            category = "RESOLVED_UNREDEEMED_REALIZATION_TIMING"
            confidence = "LIFECYCLE_SUPPORTED_NOT_WAIVED"
        else:
            category = "UNEXPLAINED"
            confidence = "REQUIRES_INVESTIGATION"
        events = events_by_asset.get(asset_id, [])
        official_row = current_positions.get(asset_id) or closed_positions.get(
            asset_id
        )
        rows.append(
            {
                "mismatch_id": item.mismatch_id,
                "comparison_type": item.comparison_type,
                "comparison_key": item.comparison_key,
                "field_name": item.field_name,
                "mismatch_type": item.mismatch_type.value,
                "official_value": item.official_value,
                "chain_replay_value": item.paper_value,
                "delta": item.delta,
                "retryable": item.retryable,
                "category": category,
                "confidence": confidence,
                "blocks_official_equivalence": not item.retryable,
                "market_slug": getattr(official_row, "slug", ""),
                "activity_types": sorted(
                    {
                        str(row.get("activity_type") or "")
                        for row in events
                    }
                ),
                "first_activity_at": (
                    min(_event_time(row) for row in events).isoformat()
                    if events
                    else None
                ),
                "last_activity_at": (
                    max(_event_time(row) for row in events).isoformat()
                    if events
                    else None
                ),
                "transaction_hashes": sorted(
                    {str(row.get("transaction_hash") or "") for row in events}
                ),
            }
        )
    category_counts: defaultdict[str, int] = defaultdict(int)
    material_category_counts: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        category_counts[row["category"]] += 1
        if row["blocks_official_equivalence"]:
            material_category_counts[row["category"]] += 1
    unexplained = [
        row
        for row in rows
        if row["blocks_official_equivalence"] and row["category"] == "UNEXPLAINED"
    ]
    excluded_assets = legacy_assets | conversion_destinations | resolved_unredeemed
    modern_assets = (
        set(current_positions) | set(closed_positions)
    ) - excluded_assets
    modern_comparison_rows = [
        row
        for row in account_truth.comparison_rows
        if row.get("comparison_type")
        in {
            "OFFICIAL_VS_PAPER_POSITION",
            "OFFICIAL_VS_PAPER_CLOSED_POSITION",
        }
        and str(row.get("comparison_key") or "") in modern_assets
    ]
    modern_mismatches = [
        row
        for row in rows
        if row["blocks_official_equivalence"]
        and row["comparison_key"] in modern_assets
    ]
    return {
        "schema_version": "chain-account-difference-analysis-v1",
        "official_run_id": official.run_id,
        "mirror_run_id": result.mirror_run_id,
        "official_account_truth_gate": account_truth.status.value,
        "semantic_attribution_gate": "PASS" if not unexplained else "FAIL",
        "summary": {
            "mismatch_count": len(rows),
            "material_mismatch_count": sum(
                bool(row["blocks_official_equivalence"]) for row in rows
            ),
            "unexplained_material_mismatch_count": len(unexplained),
            "category_counts": dict(sorted(category_counts.items())),
            "material_category_counts": dict(
                sorted(material_category_counts.items())
            ),
            "legacy_asset_count": len(legacy_assets),
            "conversion_destination_count": len(conversion_destinations),
            "resolved_unredeemed_asset_count": len(resolved_unredeemed),
            "modern_directly_comparable_asset_count": len(modern_assets),
            "modern_direct_field_comparison_count": len(modern_comparison_rows),
            "modern_direct_field_mismatch_count": len(modern_mismatches),
            "modern_direct_field_gate": (
                "PASS" if modern_comparison_rows and not modern_mismatches else "FAIL"
            ),
            "interpretation": (
                "PASS means every observed difference has an evidence-backed "
                "category. It does not waive any mismatch or establish official "
                "PnL-curve equivalence."
            ),
        },
        "rows": rows,
    }


def _remove(position: _PositionState, quantity: Decimal) -> tuple[Decimal, Decimal]:
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise ChainActivityMirrorError("removed quantity must be positive")
    if quantity - position.quantity > ROUNDING_EPSILON:
        raise ChainActivityMirrorError(
            f"receipt removes unavailable {position.asset_id}: "
            f"{quantity}>{position.quantity}"
        )
    quantity = min(quantity, position.quantity)
    if not position.quantity:
        return Decimal(0), Decimal(0)
    ratio = quantity / position.quantity
    removed_basis = position.fee_exclusive_basis * ratio
    removed_fees = position.entry_fees * ratio
    position.quantity -= quantity
    position.fee_exclusive_basis -= removed_basis
    position.entry_fees -= removed_fees
    if abs(position.quantity) <= ROUNDING_EPSILON:
        position.quantity = Decimal(0)
        position.fee_exclusive_basis = Decimal(0)
        position.entry_fees = Decimal(0)
    return removed_basis, removed_fees


def _allocate_realized(
    *,
    states: Mapping[str, _PositionState],
    removed_by_asset: Mapping[str, Decimal],
    cash_delta: Decimal,
    official: OfficialAccountBundle,
    allocation_mode: str,
) -> None:
    if allocation_mode == "BASIS_PROPORTIONAL":
        weights = dict(removed_by_asset)
    elif allocation_mode == "PAYOUT_WEIGHTED":
        prices = {row.asset_id: row.current_price for row in official.positions}
        prices.update(
            {row.asset_id: row.current_price for row in official.closed_positions}
        )
        weights = {
            asset_id: max(prices.get(asset_id, Decimal(0)), Decimal(0))
            for asset_id in removed_by_asset
        }
    else:
        raise ChainActivityMirrorError(
            f"unsupported realized allocation mode: {allocation_mode}"
        )
    total_weight = sum(weights.values(), Decimal(0))
    if not total_weight:
        weights = {asset_id: Decimal(1) for asset_id in removed_by_asset}
        total_weight = Decimal(len(weights))
    for asset_id, removed_basis in removed_by_asset.items():
        allocated_cash = cash_delta * weights[asset_id] / total_weight
        states[asset_id].realized_pnl += allocated_cash - removed_basis


def _receipt(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    result = payload.get("result") if "result" in payload else payload
    receipt = _mapping(result)
    status = receipt.get("status")
    if status not in {1, "1", "0x1", "0X1"}:
        raise ChainActivityMirrorError("Polygon receipt is absent or unsuccessful")
    return receipt


def _validate_receipt_payload(payload: Mapping[str, Any], tx_hash: str) -> None:
    receipt = _receipt(payload)
    if _transaction_hash(receipt.get("transactionHash")) != tx_hash:
        raise ChainActivityMirrorError("Polygon receipt transaction hash mismatch")


def _fee(value: Decimal) -> Decimal:
    value = Decimal(value)
    if value < -ROUNDING_EPSILON:
        raise ChainActivityMirrorError(f"derived fee is negative: {value}")
    return max(value, Decimal(0))


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ChainActivityMirrorError(f"{field_name} must be positive Decimal")
    return parsed


def _transaction_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text.startswith("0x") or len(text) != 66:
        raise ChainActivityMirrorError("transaction hash must be 32-byte hex")
    try:
        bytes.fromhex(text[2:])
    except ValueError as exc:
        raise ChainActivityMirrorError("transaction hash is not hex") from exc
    return text


def _address(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text.startswith("0x") or len(text) != 42:
        raise ChainActivityMirrorError("address must be 20-byte hex")
    try:
        bytes.fromhex(text[2:])
    except ValueError as exc:
        raise ChainActivityMirrorError("address is not hex") from exc
    return text


def _topic_address(topic: str) -> str:
    text = str(topic).lower()
    if not text.startswith("0x") or len(text) != 66:
        raise ChainActivityMirrorError("indexed address topic is invalid")
    return _address("0x" + text[-40:])


def _hex_bytes(value: Any) -> bytes:
    text = str(value or "")
    if not text.startswith("0x") or len(text) % 2:
        raise ChainActivityMirrorError("event data is invalid hex")
    try:
        return bytes.fromhex(text[2:])
    except ValueError as exc:
        raise ChainActivityMirrorError("event data is invalid hex") from exc


def _scaled_hex(value: Any) -> Decimal:
    data = _hex_bytes(value)
    if len(data) != 32:
        raise ChainActivityMirrorError("ERC20 Transfer data must be one word")
    return Decimal(int.from_bytes(data, "big")) / TOKEN_SCALE


def _hex_int(value: Any) -> int:
    try:
        return int(str(value), 16)
    except (TypeError, ValueError) as exc:
        raise ChainActivityMirrorError("receipt index is invalid") from exc


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        selected = value
    else:
        selected = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if selected.tzinfo is None:
        raise ChainActivityMirrorError("activity timestamp must be timezone-aware")
    return selected.astimezone(timezone.utc)


def _event_time(row: Mapping[str, Any]) -> datetime:
    return _utc(row.get("event_ts"))


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChainActivityMirrorError("expected a mapping")
    return value


def _decimal_text(value: Decimal) -> str:
    return format(Decimal(value), "f")


def _decimal_mapping(value: Mapping[str, Decimal]) -> dict[str, str]:
    return {str(key): _decimal_text(item) for key, item in sorted(value.items())}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _safe_endpoint_label(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.hostname or 'unknown'}"


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


__all__ = [
    "analyze_chain_account_differences",
    "ChainActivityMirrorError",
    "ChainActivityMirrorResult",
    "ChainActivityMirrorService",
    "OfficialActivity",
    "PolygonReceiptArchive",
    "ReceiptEvidence",
    "WalletTransferDelta",
    "decode_wallet_transfers",
    "replay_chain_activity",
    "write_chain_activity_mirror_report",
]
