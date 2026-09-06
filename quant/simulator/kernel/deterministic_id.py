"""Canonical hashing helpers that never depend on process-local identity."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping


def canonical_json(value: Any) -> str:
    """Serialize a simulator value in one stable representation.

    Float payloads are rejected because they make a ledger/event truth depend on
    binary representation. Raw adapters must convert prices, size and cash to
    fixed-point integers or decimal strings before creating a ``SimEvent``.
    """
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def deterministic_id(namespace: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{str(namespace).strip().lower()}:{digest}"


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"__decimal__": format(value, "f")}
    if isinstance(value, float):
        raise TypeError("float is not allowed in deterministic simulator payloads")
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set):
        return [_canonical_value(item) for item in sorted(value, key=canonical_json)]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported deterministic simulator value: {type(value).__name__}")
