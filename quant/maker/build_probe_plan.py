"""Build a post-only maker probe plan without submitting an order."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml  # type: ignore[import-untyped]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--no-submit", action="store_true", required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("reports/maker/probe_plan.json")
    )
    args = parser.parse_args(argv)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    payload = {
        "schema_version": "maker_post_only_probe_plan_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "NO_SUBMIT_READY",
        "exchange_submit_called": False,
        "one_live_resting_probe_max": True,
        "config": config,
        "promotion_status": "RESEARCH_UNCALIBRATED",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
