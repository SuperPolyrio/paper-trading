#!/usr/bin/env python3
"""Build the online research-only Maker calibration artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.execution.models.maker_probability_calibration import (
    MakerProbabilityCalibrationArtifact,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-summary", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runtime_outputs/maker_calibration/probability-current.json"),
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=Path(
            "runtime_outputs/maker_calibration/offline-summary-current.json"
        ),
        help="stable paired benchmark summary consumed by the online resolver",
    )
    args = parser.parse_args()
    summary_raw = args.benchmark_summary.read_bytes()
    summary = json.loads(summary_raw)
    artifact = MakerProbabilityCalibrationArtifact.from_benchmark_report(summary)
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    temporary_summary = args.summary_output.with_name(
        f".{args.summary_output.name}.tmp-{os.getpid()}"
    )
    try:
        temporary_summary.write_bytes(summary_raw)
        os.replace(temporary_summary, args.summary_output)
    finally:
        temporary_summary.unlink(missing_ok=True)
    output = artifact.write(args.output)
    payload = {
        "status": "PASS",
        "research_only": True,
        "authoritative_pnl": False,
        "artifact_path": str(output.resolve()),
        "artifact_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "artifact_hash": artifact.artifact_hash,
        "calibration_domain": artifact.calibration_domain,
        "probability_domain": "[0,0.5)",
        "benchmark_summary_path": str(args.summary_output.resolve()),
        "benchmark_summary_sha256": hashlib.sha256(summary_raw).hexdigest(),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
