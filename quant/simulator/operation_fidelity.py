"""Unified event and terminal-state reconciliation for paper operations."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ECONOMIC_EVENT_FIELDS = (
    "event_type",
    "state",
    "order_type",
    "liquidity_role",
    "execution_class",
    "filled_size",
    "average_price",
    "filled_notional",
    "cash_delta",
    "fee",
    "realized_pnl_delta",
    "token_deltas",
)


@dataclass(frozen=True)
class OperationEvidenceBundle:
    scenario_id: str
    operation_type: str
    before: Mapping[str, Any]
    real: Mapping[str, Any]
    paper: Mapping[str, Any]
    reconciliation: Mapping[str, Any]
    rules: Mapping[str, Any]


def reconcile_operation(
    *,
    scenario_id: str,
    operation_type: str,
    before: Mapping[str, Any],
    real: Mapping[str, Any],
    paper: Mapping[str, Any],
    rules: Mapping[str, Any] | None = None,
) -> OperationEvidenceBundle:
    """Compare normalized official truth with paper events and account state.

    ``OFFICIAL_FIXTURE`` evidence proves deterministic semantics only. A full PASS
    additionally requires ``LIVE`` evidence and every required source reference.
    """

    policy = dict(rules or {})
    tolerance = abs(_decimal(policy.get("tolerance", "0.000001")))
    real_events = _events(real)
    paper_events = _events(paper)
    real_duplicates = _duplicates(real_events)
    paper_duplicates = _duplicates(paper_events)
    real_by_key = _by_event_key(real_events)
    paper_by_key = _by_event_key(paper_events)
    event_keys_match = set(real_by_key) == set(paper_by_key)
    event_differences: list[dict[str, Any]] = []
    for event_key in sorted(set(real_by_key) | set(paper_by_key)):
        left = real_by_key.get(event_key)
        right = paper_by_key.get(event_key)
        if left is None or right is None:
            event_differences.append(
                {
                    "event_key": event_key,
                    "field": "event_presence",
                    "real": left is not None,
                    "paper": right is not None,
                    "status": "MISMATCH",
                }
            )
            continue
        for field in ECONOMIC_EVENT_FIELDS:
            event_differences.extend(
                _compare_values(
                    left.get(field),
                    right.get(field),
                    path=f"events.{event_key}.{field}",
                    tolerance=tolerance,
                )
            )

    terminal_differences = _compare_values(
        real.get("after"),
        paper.get("after"),
        path="after",
        tolerance=tolerance,
    )
    all_differences = event_differences + terminal_differences
    economic_mismatches = [
        row for row in all_differences if row.get("status") == "MISMATCH"
    ]
    required_paths = tuple(str(item) for item in policy.get("required_live_evidence", ()))
    missing_live_evidence = [
        path for path in required_paths if not _truthy_path(real, path)
    ]
    evidence_mode = str(real.get("evidence_mode") or "UNKNOWN").upper()
    source_file_verification = real.get("_source_file_verification")
    source_files_verified = bool(
        isinstance(source_file_verification, Mapping)
        and source_file_verification.get("status") == "PASS"
    )
    verified_hashes = {
        str(value).lower()
        for value in (
            source_file_verification.get("verified_sha256", ())
            if isinstance(source_file_verification, Mapping)
            else ()
        )
    }
    unbound_live_hashes = [
        path
        for path in required_paths
        if _requires_file_hash(path)
        and str(_path_value(real, path) or "").lower() not in verified_hashes
    ]
    deterministic_pass = bool(
        not real_duplicates
        and not paper_duplicates
        and event_keys_match
        and not economic_mismatches
        and bool(paper.get("duplicate_replay_ignored", False))
    )
    external_pass = bool(
        evidence_mode == "LIVE"
        and not missing_live_evidence
        and source_files_verified
        and not unbound_live_hashes
    )
    if not deterministic_pass:
        status = "FAIL"
    elif external_pass:
        status = "PASS"
    else:
        status = "PASS_OFFLINE"
    checks = {
        "real_event_ids_unique": not real_duplicates,
        "paper_event_ids_unique": not paper_duplicates,
        "event_keys_match": event_keys_match,
        "event_economics_match": not any(
            row.get("status") == "MISMATCH" for row in event_differences
        ),
        "terminal_state_matches": not any(
            row.get("status") == "MISMATCH" for row in terminal_differences
        ),
        "duplicate_replay_is_idempotent": bool(
            paper.get("duplicate_replay_ignored", False)
        ),
        "live_source_files_verified": source_files_verified,
        "live_hashes_bound_to_source_files": not unbound_live_hashes,
        "live_source_evidence_complete": external_pass,
    }
    reconciliation = {
        "schema_version": "paper-operation-reconciliation-v1",
        "scenario_id": scenario_id,
        "operation_type": operation_type,
        "status": status,
        "deterministic_gate": "PASS" if deterministic_pass else "FAIL",
        "external_truth_gate": (
            "PASS" if external_pass else "INSUFFICIENT_EVIDENCE"
        ),
        "evidence_mode": evidence_mode,
        "tolerance": format(tolerance, "f"),
        "checks": checks,
        "real_duplicate_event_keys": real_duplicates,
        "paper_duplicate_event_keys": paper_duplicates,
        "missing_live_evidence": missing_live_evidence,
        "unbound_live_hashes": unbound_live_hashes,
        "source_file_verification": source_file_verification,
        "differences": all_differences,
        "content_sha256": _hash(
            {
                "scenario_id": scenario_id,
                "operation_type": operation_type,
                "before": before,
                "real": real,
                "paper": paper,
                "rules": policy,
            }
        ),
    }
    return OperationEvidenceBundle(
        scenario_id=scenario_id,
        operation_type=operation_type,
        before=dict(before),
        real=dict(real),
        paper=dict(paper),
        reconciliation=reconciliation,
        rules=policy,
    )


def load_operation_evidence(directory: Path | str) -> OperationEvidenceBundle:
    root = Path(directory)
    before = _read_json(root / "before.json")
    real = _read_json(root / "real.json")
    real["_source_file_verification"] = _verify_source_files(root, real)
    paper = _read_json(root / "paper.json")
    rules_path = root / "rules.json"
    rules = _read_json(rules_path) if rules_path.exists() else {}
    scenario_id = str(
        real.get("scenario_id")
        or paper.get("scenario_id")
        or before.get("scenario_id")
        or root.name
    )
    operation_type = str(
        real.get("operation_type")
        or paper.get("operation_type")
        or before.get("operation_type")
        or "UNKNOWN"
    )
    return reconcile_operation(
        scenario_id=scenario_id,
        operation_type=operation_type,
        before=before,
        real=real,
        paper=paper,
        rules=rules,
    )


def write_operation_evidence_bundle(
    output_root: Path | str,
    bundle: OperationEvidenceBundle,
) -> dict[str, str]:
    root = Path(output_root) / _safe_name(bundle.scenario_id)
    root.mkdir(parents=True, exist_ok=True)
    payloads = {
        "before": bundle.before,
        "real": bundle.real,
        "paper": bundle.paper,
        "reconciliation": bundle.reconciliation,
        "rules": bundle.rules,
    }
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for name, payload in payloads.items():
        path = root / f"{name}.json"
        content = _pretty_json(payload)
        _atomic_text(path, content)
        paths[name] = path
        hashes[path.name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": "paper-operation-evidence-manifest-v1",
        "scenario_id": bundle.scenario_id,
        "operation_type": bundle.operation_type,
        "status": bundle.reconciliation["status"],
        "files": hashes,
    }
    manifest_path = root / "manifest.json"
    _atomic_text(manifest_path, _pretty_json(manifest))
    paths["manifest"] = manifest_path
    return {name: str(path.resolve()) for name, path in paths.items()}


def verify_operation_evidence_bundle(directory: Path | str) -> dict[str, Any]:
    root = Path(directory)
    manifest = _read_json(root / "manifest.json")
    mismatches: list[str] = []
    for filename, expected in (manifest.get("files") or {}).items():
        path = root / str(filename)
        if not path.is_file():
            mismatches.append(f"missing:{filename}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != str(expected):
            mismatches.append(f"sha256:{filename}")
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "scenario_id": manifest.get("scenario_id"),
        "mismatches": mismatches,
    }


def _events(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("events")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _event_key(row: Mapping[str, Any]) -> str:
    return str(row.get("event_key") or row.get("event_id") or "")


def _duplicates(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in rows:
        key = _event_key(row)
        if not key or key in seen:
            duplicates.add(key or "<missing>")
        seen.add(key)
    return sorted(duplicates)


def _by_event_key(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_event_key(row): dict(row) for row in rows if _event_key(row)}


def _compare_values(
    real: Any,
    paper: Any,
    *,
    path: str,
    tolerance: Decimal,
) -> list[dict[str, Any]]:
    if isinstance(real, Mapping) or isinstance(paper, Mapping):
        left = real if isinstance(real, Mapping) else {}
        right = paper if isinstance(paper, Mapping) else {}
        rows: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            rows.extend(
                _compare_values(
                    left.get(key),
                    right.get(key),
                    path=f"{path}.{key}",
                    tolerance=tolerance,
                )
            )
        return rows
    if _is_decimal(real) and _is_decimal(paper):
        left_decimal = _decimal(real)
        right_decimal = _decimal(paper)
        delta = right_decimal - left_decimal
        status = "MATCH" if abs(delta) <= tolerance else "MISMATCH"
        return [
            {
                "path": path,
                "real": format(left_decimal, "f"),
                "paper": format(right_decimal, "f"),
                "delta": format(delta, "f"),
                "tolerance": format(tolerance, "f"),
                "status": status,
            }
        ]
    status = "MATCH" if real == paper else "MISMATCH"
    return [{"path": path, "real": real, "paper": paper, "status": status}]


def _truthy_path(payload: Mapping[str, Any], path: str) -> bool:
    return _path_value(payload, path) not in (None, "", [], {}, False)


def _path_value(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _requires_file_hash(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1].lower()
    return leaf.endswith(("_hash", "_sha256"))


def _verify_source_files(root: Path, real: Mapping[str, Any]) -> dict[str, Any]:
    source_manifest = real.get("source_manifest")
    evidence_files = (
        source_manifest.get("evidence_files")
        if isinstance(source_manifest, Mapping)
        else None
    )
    if not isinstance(evidence_files, Mapping) or not evidence_files:
        return {
            "status": "FAIL",
            "verified_sha256": [],
            "mismatches": ["source_manifest.evidence_files_missing"],
        }
    resolved_root = root.resolve()
    verified: list[str] = []
    mismatches: list[str] = []
    for relative_name, expected_hash in sorted(evidence_files.items()):
        relative = Path(str(relative_name))
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(resolved_root):
            mismatches.append(f"unsafe_path:{relative_name}")
            continue
        if not path.is_file():
            mismatches.append(f"missing:{relative_name}")
            continue
        expected = str(expected_hash).lower()
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            mismatches.append(f"sha256:{relative_name}")
            continue
        verified.append(actual)
    return {
        "status": "PASS" if verified and not mismatches else "FAIL",
        "verified_sha256": sorted(set(verified)),
        "mismatches": mismatches,
    }


def _is_decimal(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return True


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError(f"operation evidence must be a JSON object: {path}")
    return dict(payload)


def _pretty_json(payload: Any) -> str:
    return json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=True,
        default=str,
    ) + "\n"


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _safe_name(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in str(value)
    ).strip("-")
    if not normalized:
        raise ValueError("scenario_id does not contain a safe filename")
    return normalized
