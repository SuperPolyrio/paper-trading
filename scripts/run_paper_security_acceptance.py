#!/usr/bin/env python3
"""Run deterministic paper/live isolation acceptance without live credentials."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.adapters.polymarket_paper_clob_client import (  # noqa: E402
    PolymarketPaperClobClient,
)
from quant.paper.security import (  # noqa: E402
    PaperSecurityViolation,
    audit_paper_dependency_boundary,
    build_credential_manifest,
    enforce_paper_security_boundary,
    scan_paths_for_secret_material,
    verify_credential_rotation,
    verify_hash_chain,
)


FORBIDDEN_CLIENT_METHODS = (
    "create_order",
    "create_and_post_order",
    "post_order",
    "submit_order",
    "cancel_order",
)


def _check(name: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "detail": detail}


def run_acceptance(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    source_issues = audit_paper_dependency_boundary(root)
    checks.append(
        _check("paper_source_dependency_boundary", not source_issues, source_issues)
    )
    scan_paths = [
        path
        for path in (root / "quant" / "paper").glob("*.py")
        if path.name != "security.py"
    ]
    scan_paths.extend(
        (
            root / "quant" / "adapters" / "polymarket_paper_clob_client.py",
            root / "scripts" / "paper_runtime_credentials.sh",
            root / "scripts" / "run_paper_security_acceptance.py",
            root / "deploy" / "systemd" / "paper-runtime.env.example",
            root / "deploy" / "gcp" / "provision_paper_live_isolation.sh",
            root / "deploy" / "gcp" / "provision_paper_egress_policy.sh",
            root / "deploy" / "postgres" / "paper_runtime_role.sql",
        )
    )
    secret_issues = scan_paths_for_secret_material(scan_paths)
    checks.append(_check("paper_source_secret_scan", not secret_issues, secret_issues))

    with tempfile.TemporaryDirectory(prefix="paper-security-") as raw_tmp:
        tmp = Path(raw_tmp)
        audit_path = tmp / "paper-security-audit.jsonl"
        safe_report = enforce_paper_security_boundary(
            environ={"POLY_QUANT_DISABLE_DOTENV": "1"},
            audit_log=audit_path,
            source_root=root,
        )
        checks.append(
            _check("safe_paper_environment", safe_report["status"] == "PASS", {})
        )

        sentinel = "acceptance-only-private-value-must-not-leak"
        rejected = False
        leaked = False
        try:
            enforce_paper_security_boundary(
                environ={"POLYMARKET_PRIVATE_KEY": sentinel},
                audit_log=audit_path,
                source_root=root,
            )
        except PaperSecurityViolation as exc:
            rejected = True
            leaked = sentinel in str(exc) or sentinel in audit_path.read_text(
                encoding="utf-8"
            )
        checks.append(
            _check(
                "live_key_negative_test",
                rejected and not leaked,
                {"startup_rejected": rejected, "secret_value_leaked": leaked},
            )
        )

        missing_methods = [
            method
            for method in FORBIDDEN_CLIENT_METHODS
            if hasattr(PolymarketPaperClobClient, method)
        ]
        checks.append(
            _check(
                "read_only_clob_capability",
                not missing_methods,
                {
                    "forbidden_methods_exposed": missing_methods,
                    "capabilities": sorted(PolymarketPaperClobClient.capabilities),
                },
            )
        )

        credential_path = tmp / "paper-db-password"
        credential_path.write_text("acceptance-password-v1\n", encoding="utf-8")
        os.chmod(credential_path, 0o600)
        before = build_credential_manifest({"paper-db-password": credential_path})
        credential_path.write_text("acceptance-password-v2\n", encoding="utf-8")
        os.chmod(credential_path, 0o600)
        after = build_credential_manifest({"paper-db-password": credential_path})
        rotation = verify_credential_rotation(
            before,
            after,
            required_names=["paper-db-password"],
        )
        checks.append(
            _check(
                "credential_rotation_drill",
                rotation["status"] == "PASS",
                rotation["checks"],
            )
        )

        chain = verify_hash_chain(audit_path)
        checks.append(
            _check(
                "audit_hash_chain",
                chain["status"] == "PASS" and chain["records"] == 2,
                {"status": chain["status"], "records": chain["records"]},
            )
        )
        tampered_path = tmp / "tampered-audit.jsonl"
        tampered = audit_path.read_text(encoding="utf-8").replace(
            "PAPER_SECURITY_BOUNDARY", "PAPER_SECURITY_TAMPERED", 1
        )
        tampered_path.write_text(tampered, encoding="utf-8")
        tamper_check = verify_hash_chain(tampered_path)
        checks.append(
            _check(
                "audit_tamper_detection",
                tamper_check["status"] == "FAIL",
                {
                    "status": tamper_check["status"],
                    "issue_count": len(tamper_check["issues"]),
                },
            )
        )

    failures = [row for row in checks if row["status"] == "FAIL"]
    return {
        "schema_version": "paper_security_acceptance_v1",
        "status": "PASS" if not failures else "FAIL",
        "evidence_class": "DETERMINISTIC_SECURITY_ACCEPTANCE_NOT_PRODUCTION_IAM",
        "paper_only": True,
        "network_calls_performed": False,
        "live_credentials_read": False,
        "live_submission_performed": False,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runtime_outputs/security/paper-security-acceptance.json"),
    )
    args = parser.parse_args()
    report = run_acceptance(args.root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(args.output, 0o600)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
