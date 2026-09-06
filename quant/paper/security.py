"""Fail-closed paper/live credential, dependency, and audit boundaries."""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORBIDDEN_PAPER_ENV_KEYS = frozenset(
    {
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "POLYMARKET_BUILDER_API_KEY",
        "POLYMARKET_BUILDER_SECRET",
        "POLYMARKET_RELAYER_API_KEY",
        "POLYMARKET_RELAYER_API_KEY_ADDRESS",
        "POLY_QUANT_PROBE_PRIVATE_KEY",
        "POLY_QUANT_PROBE_API_KEY",
        "POLY_QUANT_PROBE_API_SECRET",
        "POLY_QUANT_PROBE_API_PASSPHRASE",
        "POLY_QUANT_LIVE_PRIVATE_KEY",
        "ORDER_EXECUTION_PRIVATE_KEY",
        "ORDER_EXECUTION_LIVE_CONFIRM",
        "ORDER_EXECUTION_SUBMIT_URL",
        "LIVE_ENABLE_PHRASE",
    }
)
FORBIDDEN_PAPER_ENV_PREFIXES = (
    "POLY_QUANT_PROBE_",
    "POLY_QUANT_LIVE_",
    "POLYMARKET_RELAYER_",
    "POLYMARKET_BUILDER_",
)
PLAINTEXT_SECRET_FILE_KEYS = frozenset(
    {
        "POLYDATA_POSTGRES_PASSWORD",
        "POLYMARKET_POSTGRES_PASSWORD",
        "POLYMARKET_POSTGRESQL_PASSWORD",
    }
)
FORBIDDEN_PAPER_IMPORT_PREFIXES = (
    "py_clob_client",
    "quant.calibration.real_live_adapter",
    "quant.calibration.live_probe_runner",
    "quant.calibration.settlement_redeemer",
    "quant.settlement.redeem_e2e",
)
FORBIDDEN_PAPER_CALLS = frozenset(
    {
        "build_and_submit_once",
        "create_and_post_order",
        "create_order",
        "post_order",
        "submit_and_wait",
        "submit_prepared_once",
    }
)
SENSITIVE_DETAIL_KEY = re.compile(
    r"(?:password|private.?key|secret|passphrase|signature|mnemonic|credential)",
    re.IGNORECASE,
)
SECRET_MATERIAL_PATTERNS = (
    (
        "private_key_pem",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "ethereum_private_key",
        re.compile(r"(?<![0-9A-Fa-f])0x[0-9A-Fa-f]{64}(?![0-9A-Fa-f])"),
    ),
)


class PaperSecurityViolation(RuntimeError):
    pass


def is_forbidden_paper_env_key(key: str) -> bool:
    normalized = str(key).strip().upper()
    return normalized in FORBIDDEN_PAPER_ENV_KEYS or normalized.startswith(
        FORBIDDEN_PAPER_ENV_PREFIXES
    )


def forbidden_paper_environment_keys(environ: Mapping[str, str]) -> list[str]:
    return sorted(key for key in environ if is_forbidden_paper_env_key(key))


def inspect_environment_file(path: Path) -> dict[str, Any]:
    keys = _env_keys(path)
    mode = path.stat().st_mode & 0o777 if path.is_file() else None
    return {
        "path": str(path),
        "exists": path.is_file(),
        "mode": oct(mode) if mode is not None else None,
        "forbidden_live_keys": sorted(
            key for key in keys if is_forbidden_paper_env_key(key)
        ),
        "plaintext_secret_keys": sorted(keys & PLAINTEXT_SECRET_FILE_KEYS),
        "owner_only": mode is not None and mode & 0o077 == 0,
        "key_count": len(keys),
    }


def enforce_paper_security_boundary(
    *,
    environ: Mapping[str, str] | None = None,
    audit_log: Path | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    forbidden = forbidden_paper_environment_keys(env)
    dependency_issues = (
        audit_paper_dependency_boundary(source_root) if source_root else []
    )
    status = "PASS" if not forbidden and not dependency_issues else "FAIL"
    report = {
        "schema_version": "paper_security_boundary_v1",
        "generated_at": _now().isoformat(),
        "status": status,
        "paper_only": True,
        "live_submission_performed": False,
        "forbidden_environment_keys": forbidden,
        "dependency_issues": dependency_issues,
    }
    if audit_log is not None:
        HashChainAuditLog(audit_log).append(
            event_type="PAPER_SECURITY_BOUNDARY",
            status=status,
            details={
                "forbidden_environment_keys": forbidden,
                "dependency_issue_count": len(dependency_issues),
            },
        )
    if status != "PASS":
        raise PaperSecurityViolation(
            "paper security boundary rejected startup: "
            f"forbidden_keys={forbidden}, dependency_issues={len(dependency_issues)}"
        )
    return report


def audit_paper_dependency_boundary(root: Path | None = None) -> list[dict[str, Any]]:
    paper_root = (root or Path(__file__).resolve().parents[2]) / "quant" / "paper"
    issues: list[dict[str, Any]] = []
    for path in sorted(paper_root.glob("*.py")):
        if path.name == "security.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            issues.append(
                {
                    "path": str(path),
                    "line": getattr(exc, "lineno", None),
                    "kind": "parse_error",
                    "name": type(exc).__name__,
                }
            )
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [str(node.module or "")]
            else:
                names = []
            for name in names:
                if name.startswith(FORBIDDEN_PAPER_IMPORT_PREFIXES):
                    issues.append(
                        _dependency_issue(path, node, "forbidden_import", name)
                    )
            if isinstance(node, ast.Call):
                call_name = _call_name(node.func)
                if call_name in FORBIDDEN_PAPER_CALLS:
                    issues.append(
                        _dependency_issue(path, node, "forbidden_call", call_name)
                    )
    return issues


def scan_paths_for_secret_material(paths: Sequence[Path]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path in sorted({Path(item) for item in paths}):
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            issues.append({"path": str(path), "line": 0, "rule": type(exc).__name__})
            continue
        for rule, pattern in SECRET_MATERIAL_PATTERNS:
            for match in pattern.finditer(source):
                issues.append(
                    {
                        "path": str(path),
                        "line": source.count("\n", 0, match.start()) + 1,
                        "rule": rule,
                    }
                )
    return issues


class HashChainAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(
        self,
        *,
        event_type: str,
        status: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            os.chmod(self.path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            previous_hash = ""
            for line in handle:
                try:
                    previous_hash = str(json.loads(line).get("event_hash") or "")
                except json.JSONDecodeError:
                    previous_hash = "INVALID_PREVIOUS_RECORD"
            record = {
                "schema_version": "paper_security_audit_event_v1",
                "generated_at": _now().isoformat(),
                "event_type": str(event_type),
                "status": str(status),
                "details": _redact_details(dict(details or {})),
                "previous_hash": previous_hash,
            }
            record["event_hash"] = _record_hash(record)
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return record


def verify_hash_chain(path: Path) -> dict[str, Any]:
    previous_hash = ""
    issues: list[dict[str, Any]] = []
    records = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {
            "status": "FAIL",
            "records": 0,
            "issues": [{"line": 0, "reason": type(exc).__name__}],
        }
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            issues.append({"line": line_number, "reason": "invalid_json"})
            continue
        records += 1
        observed_hash = str(record.pop("event_hash", ""))
        if str(record.get("previous_hash") or "") != previous_hash:
            issues.append({"line": line_number, "reason": "previous_hash_mismatch"})
        if observed_hash != _record_hash(record):
            issues.append({"line": line_number, "reason": "event_hash_mismatch"})
        previous_hash = observed_hash
    return {
        "status": "PASS" if not issues else "FAIL",
        "records": records,
        "head_hash": previous_hash,
        "issues": issues,
    }


def build_credential_manifest(credentials: Mapping[str, Path]) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    for name, raw_path in sorted(credentials.items()):
        path = Path(raw_path)
        if not path.is_file():
            rows[str(name)] = {"path": str(path), "exists": False}
            continue
        mode = path.stat().st_mode & 0o777
        rows[str(name)] = {
            "path": str(path),
            "exists": True,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
            "mode": oct(mode),
            "owner_only": mode & 0o077 == 0,
            "nonempty": path.stat().st_size > 0,
        }
    passed = bool(rows) and all(
        row.get("exists") and row.get("owner_only") and row.get("nonempty")
        for row in rows.values()
    )
    return {
        "schema_version": "paper_credential_manifest_v1",
        "generated_at": _now().isoformat(),
        "status": "PASS" if passed else "FAIL",
        "credentials": rows,
    }


def verify_credential_rotation(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    required_names: Sequence[str],
) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    old = before.get("credentials") or {}
    new = after.get("credentials") or {}
    for name in required_names:
        old_row = old.get(name) or {}
        new_row = new.get(name) or {}
        checks[str(name)] = bool(
            old_row.get("sha256")
            and new_row.get("sha256")
            and old_row.get("sha256") != new_row.get("sha256")
            and new_row.get("owner_only")
            and new_row.get("nonempty")
        )
    return {
        "schema_version": "paper_credential_rotation_v1",
        "generated_at": _now().isoformat(),
        "status": "PASS" if checks and all(checks.values()) else "FAIL",
        "checks": checks,
    }


def _dependency_issue(
    path: Path,
    node: ast.AST,
    kind: str,
    name: str,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "line": getattr(node, "lineno", None),
        "kind": kind,
        "name": name,
    }


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return str(node.attr)
    if isinstance(node, ast.Name):
        return str(node.id)
    return ""


def _env_keys(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    keys: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip().upper())
    return keys


def _redact_details(value: Any, *, key: str = "") -> Any:
    if key and SENSITIVE_DETAIL_KEY.search(key):
        if key in {"forbidden_environment_keys", "plaintext_secret_keys"}:
            return sorted(str(item) for item in value)
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_details(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_details(item) for item in value]
    return value


def _record_hash(record: Mapping[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    source = sub.add_parser("source-audit")
    source.add_argument("--root", type=Path, default=Path.cwd())
    source.add_argument("--output", type=Path)
    runtime = sub.add_parser("runtime-enforce")
    runtime.add_argument("--root", type=Path, default=Path.cwd())
    runtime.add_argument(
        "--audit-log",
        type=Path,
        default=Path("runtime_outputs/security/paper-security-audit.jsonl"),
    )
    runtime.add_argument("--output", type=Path)
    secret_scan = sub.add_parser("secret-scan")
    secret_scan.add_argument("path", nargs="+", type=Path)
    secret_scan.add_argument("--output", type=Path)
    env = sub.add_parser("env-audit")
    env.add_argument("--env-file", action="append", type=Path, default=[])
    env.add_argument("--output", type=Path)
    chain = sub.add_parser("verify-audit-log")
    chain.add_argument("path", type=Path)
    chain.add_argument("--output", type=Path)
    manifest = sub.add_parser("credential-manifest")
    manifest.add_argument("--credential", action="append", default=[])
    manifest.add_argument("--output", type=Path)
    rotation = sub.add_parser("rotation-verify")
    rotation.add_argument("--before", type=Path, required=True)
    rotation.add_argument("--after", type=Path, required=True)
    rotation.add_argument("--required", action="append", default=[])
    rotation.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "source-audit":
        issues = audit_paper_dependency_boundary(args.root)
        report = {
            "schema_version": "paper_source_security_audit_v1",
            "generated_at": _now().isoformat(),
            "status": "PASS" if not issues else "FAIL",
            "issues": issues,
        }
    elif args.command == "runtime-enforce":
        try:
            report = enforce_paper_security_boundary(
                environ=os.environ,
                audit_log=args.audit_log,
                source_root=args.root,
            )
        except PaperSecurityViolation as exc:
            report = {
                "schema_version": "paper_security_boundary_v1",
                "generated_at": _now().isoformat(),
                "status": "FAIL",
                "reason": str(exc),
                "live_submission_performed": False,
            }
    elif args.command == "secret-scan":
        issues = scan_paths_for_secret_material(args.path)
        report = {
            "schema_version": "paper_secret_scan_v1",
            "generated_at": _now().isoformat(),
            "status": "PASS" if not issues else "FAIL",
            "issues": issues,
        }
    elif args.command == "env-audit":
        files = [inspect_environment_file(path) for path in args.env_file]
        passed = bool(files) and all(
            row["exists"]
            and row["owner_only"]
            and not row["forbidden_live_keys"]
            and not row["plaintext_secret_keys"]
            for row in files
        )
        report = {
            "schema_version": "paper_environment_security_audit_v1",
            "generated_at": _now().isoformat(),
            "status": "PASS" if passed else "FAIL",
            "files": files,
        }
    elif args.command == "verify-audit-log":
        report = verify_hash_chain(args.path)
    elif args.command == "credential-manifest":
        credentials = {}
        for item in args.credential:
            if "=" not in item:
                raise SystemExit("--credential requires NAME=PATH")
            name, path = item.split("=", 1)
            credentials[name] = Path(path)
        report = build_credential_manifest(credentials)
    else:
        before = json.loads(args.before.read_text(encoding="utf-8"))
        after = json.loads(args.after.read_text(encoding="utf-8"))
        report = verify_credential_rotation(
            before,
            after,
            required_names=args.required,
        )
    if args.output:
        _write_json(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
