from __future__ import annotations

import gzip
import json
from pathlib import Path

from quant.paper import db_backup, db_migration


def test_dr_backup_scope_covers_position_operations_without_data_plane() -> None:
    tables = set(db_migration.SNAPSHOT_TABLES)

    assert {
        "simulator_position_operations",
        "simulator_position_operation_events",
        "simulator_position_operation_nonces",
        "paper_position_operation_applications",
    } <= tables
    assert "paper_market_registry_tokens" not in tables
    assert "paper_registry_outbox" not in tables
    assert "l2_archive_manifest" not in tables
    assert "paper_execution_partition_leases" not in tables
    assert "paper_execution_market_catalog" not in tables


def test_manifest_digest_ignores_only_its_signature() -> None:
    manifest = {"schema_version": "v1", "tables": [{"table": "a"}]}
    digest = db_backup._manifest_digest(manifest)

    signed = {**manifest, "manifest_sha256": digest}
    assert db_backup._manifest_digest(signed) == digest
    assert db_backup._manifest_digest({**signed, "tables": []}) != digest


def test_restore_gate_uses_paper_ledger_finality_contract() -> None:
    assert db_backup.PAPER_LEDGER_FINALITY_STATES == (
        "CONFIRMED",
        "FAILED_REVERSED",
    )


def test_validate_backup_detects_file_tampering(tmp_path: Path) -> None:
    bundle = tmp_path / "backup"
    bundle.mkdir()
    data_path = bundle / "000-paper_accounts.csv.gz"
    with gzip.open(data_path, "wb") as handle:
        handle.write(b'"strategy","USDC"\n')
    manifest = {
        "schema_version": db_backup.BACKUP_SCHEMA_VERSION,
        "status": "COMPLETE",
        "tables": [
            {
                "table": "paper_accounts",
                "file": data_path.name,
                "file_sha256": db_backup._file_sha256(data_path),
            }
        ],
    }
    manifest["manifest_sha256"] = db_backup._manifest_digest(manifest)
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert db_backup.validate_backup(bundle)["status"] == "PASS"
    data_path.write_bytes(data_path.read_bytes() + b"tampered")
    result = db_backup.validate_backup(bundle)
    assert result["status"] == "FAIL"
    assert result["failures"] == [f"file_sha256:{data_path.name}"]
