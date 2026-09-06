"""Remove all known probe-owned identities from delayed public evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .calibration_domain import payload_hash


@dataclass(frozen=True)
class SelfIdentity:
    addresses: frozenset[str] = frozenset()
    order_hashes: frozenset[str] = frozenset()
    trade_ids: frozenset[str] = frozenset()
    transaction_hashes: frozenset[str] = frozenset()
    maker_order_ids: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        *,
        addresses: Iterable[str] = (),
        order_hashes: Iterable[str] = (),
        trade_ids: Iterable[str] = (),
        transaction_hashes: Iterable[str] = (),
        maker_order_ids: Iterable[str] = (),
    ) -> "SelfIdentity":
        return cls(
            addresses=_normalized(addresses),
            order_hashes=_normalized(order_hashes),
            trade_ids=_normalized(trade_ids),
            transaction_hashes=_normalized(transaction_hashes),
            maker_order_ids=_normalized(maker_order_ids),
        )


def filter_self_evidence(
    rows: Iterable[Mapping[str, Any]],
    identity: SelfIdentity,
) -> dict[str, Any]:
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    self_trades: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for raw in rows:
        row = dict(raw)
        key = _evidence_key(row)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        reasons = _self_reasons(row, identity)
        if "own_maker" in reasons and "own_taker" in reasons:
            self_trades.append({**row, "self_exclusion_reasons": reasons})
        if reasons:
            excluded.append({**row, "self_exclusion_reasons": reasons})
        else:
            included.append(row)
    return {
        "state": "VERIFIED" if _identity_count(identity) else "UNVERIFIED_SELF_IDENTITY",
        "included": included,
        "excluded": excluded,
        "included_count": len(included),
        "excluded_count": len(excluded),
        "duplicate_count": duplicates,
        "identity_count": _identity_count(identity),
        "self_trade_count": len(self_trades),
        "self_trade_contaminated": bool(self_trades),
        "self_trades": self_trades,
    }


def _self_reasons(row: Mapping[str, Any], identity: SelfIdentity) -> list[str]:
    reasons: list[str] = []
    for field in ("maker", "taker", "maker_address", "owner", "trade_owner"):
        if _identity(row.get(field)) in identity.addresses:
            reasons.append(f"own_{field}")
    if _identity(row.get("order_hash") or row.get("order_id")) in identity.order_hashes:
        reasons.append("own_order_hash")
    if _identity(row.get("trade_id") or row.get("id")) in identity.trade_ids:
        reasons.append("own_trade_id")
    if _identity(row.get("transaction_hash") or row.get("tx_hash")) in identity.transaction_hashes:
        reasons.append("own_transaction_hash")
    maker_orders = row.get("maker_orders") if isinstance(row.get("maker_orders"), list) else []
    if any(
        _identity(item.get("order_id")) in identity.maker_order_ids
        for item in maker_orders
        if isinstance(item, Mapping)
    ):
        reasons.append("own_maker_order_id")
    if any(
        _identity(item.get("maker") or item.get("maker_address")) in identity.addresses
        for item in maker_orders
        if isinstance(item, Mapping)
    ):
        reasons.append("own_maker_order_address")
    return sorted(set(reasons))


def _evidence_key(row: Mapping[str, Any]) -> str:
    transaction_hash = _identity(row.get("transaction_hash") or row.get("tx_hash"))
    log_index = str(row.get("log_index") if row.get("log_index") is not None else "")
    order_hash = _identity(row.get("order_hash") or row.get("order_id"))
    if transaction_hash or log_index or order_hash:
        return f"event:{transaction_hash}:{log_index}:{order_hash}"
    return payload_hash(row, prefix="evidence-")


def _normalized(values: Iterable[str]) -> frozenset[str]:
    return frozenset(value for value in (_identity(item) for item in values) if value)


def _identity(value: Any) -> str:
    return str(value or "").strip().lower()


def _identity_count(identity: SelfIdentity) -> int:
    return sum(
        len(values)
        for values in (
            identity.addresses,
            identity.order_hashes,
            identity.trade_ids,
            identity.transaction_hashes,
            identity.maker_order_ids,
        )
    )
