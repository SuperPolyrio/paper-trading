from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from quant.paper.production_runtime import MANIFEST_FILES

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_MANIFESTS = (
    ROOT
    / "docs/reference/polymarket_official/snapshots/2026-08-18T033128Z/manifest.json",
    ROOT
    / "docs/reference/polymarket_official/help_center/snapshots/2026-08-18T073151Z/manifest.json",
    ROOT
    / "docs/reference/polymarket_official/policies/snapshots/2026-08-18T074341Z/manifest.json",
)


def test_production_manifest_contains_only_existing_files() -> None:
    missing = [path for path in MANIFEST_FILES if not (ROOT / path).is_file()]
    assert missing == []


def test_market_data_ownership_boundary_excludes_collectors() -> None:
    forbidden = (
        "quant/orderbook/l2_archive_service.py",
        "quant/orderbook/l2_archive_worker.py",
        "quant/orderbook/registry_daemon.py",
        "quant/market/registry_daemon.py",
    )
    assert [path for path in forbidden if (ROOT / path).exists()] == []


def test_repository_does_not_contain_runtime_or_secret_payloads() -> None:
    forbidden_roots = ("runtime_outputs", "reports", "secrets")
    assert [name for name in forbidden_roots if (ROOT / name).exists()] == []
    assert list(ROOT.rglob("*.parquet")) == []
    assert list(ROOT.rglob("*.pem")) == []


def test_both_paper_clients_are_shipped() -> None:
    assert (ROOT / "webpage/paper-retail.html").is_file()
    assert (ROOT / "webpage/paper.html").is_file()


def test_official_reference_manifests_are_complete_and_valid() -> None:
    errors: list[str] = []
    for manifest_path in OFFICIAL_MANIFESTS:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        for row in payload["entries"]:
            if row.get("status") != "PASS":
                continue
            path = manifest_path.parent / row["local_path"]
            if not path.is_file():
                errors.append(f"missing:{path}")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != row["sha256"]:
                errors.append(f"hash:{path}")
    assert errors == []


def test_markdown_local_links_resolve() -> None:
    missing: list[str] = []
    pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    for path in (ROOT / "docs").rglob("*.md"):
        for raw_link in pattern.findall(path.read_text(encoding="utf-8")):
            link = raw_link.split("#", 1)[0].strip().strip("<>")
            if not link or "://" in link or link.startswith(("/", "mailto:")):
                continue
            if not (path.parent / link).resolve().exists():
                missing.append(f"{path.relative_to(ROOT)}:{raw_link}")
    assert missing == []
