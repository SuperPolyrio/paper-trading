"""Immutable experiment manifests for comparable calibration probes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from importlib import metadata
from pathlib import Path
import subprocess
from typing import Any, Mapping

from quant.paper.taker_execution import TakerExecutionConfig

from .calibration_domain import ModelState, payload_hash


SCHEMA_VERSION = "paper_calibration_schema_v2"
BOOK_RECONSTRUCTION_VERSION = "local_book_l2_v1"
REGISTRY_VERSION = "dynamic_market_registry_v1"
VENUE_REGIME_ID = "polymarket-clob-v2-async-commit-20260724"


@dataclass(frozen=True)
class ExperimentManifest:
    manifest_id: str
    created_at: datetime
    git_commit: str
    git_dirty_fingerprint: str | None
    source_tree_hash: str
    db_schema_version: str
    paper_execution_model_version: str
    latency_model_version: str
    fee_model_version: str
    book_reconstruction_version: str
    registry_version: str
    venue_regime_id: str
    sdk_name: str
    sdk_version: str
    python_version: str
    execution_config_hash: str
    configuration_hash: str
    model_state: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat()
        return payload


def build_experiment_manifest(
    *,
    project_root: Path,
    execution_config: TakerExecutionConfig,
    configuration: Mapping[str, Any],
    model_state: ModelState = ModelState.CALIBRATING,
    now: datetime | None = None,
) -> ExperimentManifest:
    commit = _git(project_root, "rev-parse", "HEAD") or "UNKNOWN"
    dirty = _git(project_root, "status", "--porcelain=v1", "--untracked-files=no")
    dirty_fingerprint = hashlib.sha256(dirty.encode("utf-8")).hexdigest()[:20] if dirty else None
    source_tree_hash = _source_tree_hash(project_root)
    sdk_version = _package_version("py-clob-client-v2")
    config_hash = payload_hash(configuration, prefix="cfg-")
    base = {
        "git_commit": commit,
        "git_dirty_fingerprint": dirty_fingerprint,
        "source_tree_hash": source_tree_hash,
        "db_schema_version": SCHEMA_VERSION,
        "paper_execution_model_version": execution_config.model_version,
        "latency_model_version": "paper_latency_fixed_v1",
        "fee_model_version": "clob_v2_market_fee_curve_v1",
        "book_reconstruction_version": BOOK_RECONSTRUCTION_VERSION,
        "registry_version": REGISTRY_VERSION,
        "venue_regime_id": VENUE_REGIME_ID,
        "sdk_name": "py-clob-client-v2",
        "sdk_version": sdk_version,
        "python_version": _python_version(),
        "execution_config_hash": execution_config.config_hash,
        "configuration_hash": config_hash,
        "model_state": model_state.value,
    }
    return ExperimentManifest(
        manifest_id=payload_hash(base, prefix="manifest-"),
        created_at=(now or datetime.now(timezone.utc)).astimezone(timezone.utc),
        **base,
    )


def assert_same_manifest(expected: ExperimentManifest, observed: Mapping[str, Any]) -> None:
    observed_id = str(observed.get("manifest_id") or "")
    if observed_id != expected.manifest_id:
        raise RuntimeError(
            f"calibration model is frozen at {observed_id or 'UNKNOWN'}, not {expected.manifest_id}"
        )


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "NOT_INSTALLED"


def _source_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    paths: list[Path] = []
    for relative in ("quant/paper", "quant/calibration"):
        directory = root / relative
        if directory.exists():
            paths.extend(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)
    dependency_file = root / "environment-backtest.yml"
    if dependency_file.exists():
        paths.append(dependency_file)
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def _python_version() -> str:
    import platform

    return platform.python_version()
