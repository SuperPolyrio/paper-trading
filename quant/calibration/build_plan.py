"""Build a frozen no-submit taker calibration coverage plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .probe_sampler import coverage_buckets


def build_plan(config_path: Path) -> dict[str, Any]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("calibration config must be an object")
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    buckets = [
        {
            **bucket.__dict__,
            "depth_ratio": format(bucket.depth_ratio, "f"),
            "weight": format(bucket.weight, "f"),
        }
        for bucket in coverage_buckets()
    ]
    return {
        "schema_version": "taker_calibration_plan_v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config_path": str(config_path.resolve()),
        "config_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "venue_regime_id": config["venue_regime_id"],
        "core_domain": {
            key: value
            for key, value in config.items()
            if key not in {"sample_gates", "promotion_gates", "samplers"}
        },
        "sample_gates": config["sample_gates"],
        "promotion_gates": config["promotion_gates"],
        "coverage_probe_buckets": buckets,
        "strategy_weighted_sampler": {
            "status": "READY",
            "source": "historical strategy intents at execution time",
        },
        "live_submission": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/calibration/plan.json"))
    args = parser.parse_args(argv)
    payload = build_plan(args.config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
