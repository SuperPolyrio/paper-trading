"""CLI for Phase 5A control-plane preflight without placing an order."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path

from .probe_plan import parse_probe_plan, validate_probe_plan


DEFAULT_CONFIG = Path("config/calibration/taker_mechanical.yaml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--no-submit", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = parse_probe_plan(args.config)
        issues = validate_probe_plan(plan, live=args.live, require_credentials=True)
        credential_names = (
            plan.account.private_key_env,
            plan.account.api_key_env,
            plan.account.api_secret_env,
            plan.account.api_passphrase_env,
        )
        credential_environment = {
            name: bool(str(os.environ.get(name) or "").strip()) for name in credential_names
        }
        try:
            sdk_version = metadata.version("py-clob-client-v2")
        except metadata.PackageNotFoundError:
            sdk_version = "NOT_INSTALLED"
            issues.append("official_v2_sdk_unavailable")
        payload = {
            "status": "PASS" if not issues else "FAIL",
            "mode": "live" if args.live else "no-submit",
            "config": str(args.config),
            "plan_hash": plan.plan_hash,
            "issues": sorted(set(issues)),
            "credential_environment": credential_environment,
            "sdk_version": sdk_version,
            "exchange_order_submitted": False,
        }
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "mode": "live" if args.live else "no-submit",
            "config": str(args.config),
            "issues": [f"{exc.__class__.__name__}:{str(exc)}"],
            "exchange_order_submitted": False,
        }
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
