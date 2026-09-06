"""Guarded resolved-position redemption E2E; dry-run is the default."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from quant.calibration.settlement_redeemer import (
    SafeSettlementRedeemer,
    SettlementRedeemError,
)

DEFAULT_ENV = Path("~/.config/prediction-market-quant/calibration.env").expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--approval-token")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--rpc-url", default="https://polygon-bor-rpc.publicnode.com")
    parser.add_argument("--proxy-url", default="http://127.0.0.1:17890")
    parser.add_argument("--asset-id")
    parser.add_argument("--condition-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    output = args.output or Path("reports/settlement") / args.run_id
    output.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": "settlement_redeem_e2e_v1",
        "run_id": args.run_id,
        "mode": "EXECUTE" if args.execute else "DRY_RUN",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "submit_called": False,
        "status": "STARTED",
    }
    try:
        if args.execute and args.approval_token != args.run_id:
            raise RuntimeError(
                "live redeem requires --approval-token equal to --run-id"
            )
        load_dotenv(args.env_file, override=False)
        private_key = str(os.environ.get("POLYMARKET_PRIVATE_KEY") or "")
        safe_address = str(os.environ.get("POLYMARKET_FUNDER_ADDRESS") or "")
        if not private_key or not safe_address:
            raise RuntimeError("wallet credentials are unavailable")
        with SafeSettlementRedeemer(
            private_key=private_key,
            safe_address=safe_address,
            rpc_url=args.rpc_url,
            proxy_url=args.proxy_url,
        ) as redeemer:
            candidate = redeemer.select_candidate(
                asset_id=args.asset_id,
                condition_id=args.condition_id,
                max_size=Decimal(20),
            )
            preflight = redeemer.preflight(candidate, require_auth=args.execute)
            payload["preflight"] = preflight.as_dict()
            payload["balance_before"] = {
                "pusd": format(preflight.pusd_balance, "f"),
                "token": format(preflight.token_balance, "f"),
            }
            payload["status"] = "DRY_RUN_PASS"
            if args.execute:
                payload["submit_called"] = True
                payload["execution"] = redeemer.submit_and_wait(
                    preflight,
                    metadata=f"professional simulator redeem {args.run_id}",
                )
                payload["status"] = "LIVE_REDEEM_CONFIRMED"
    except Exception as exc:
        payload["status"] = "BLOCKED"
        payload["reason"] = f"{exc.__class__.__name__}:{exc}"
        if isinstance(exc, SettlementRedeemError) and exc.evidence:
            payload["evidence"] = exc.evidence
    (output / "balance_before_after.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    (output / "paper_vs_real.csv").write_text(
        "field,paper,real,difference,status\n"
        "payout,NOT_RUN,NOT_RUN,NOT_RUN,BLOCKED_UNTIL_LIVE_REDEEM\n",
        encoding="utf-8",
    )
    (output / "redeem_e2e.md").write_text(
        "# Redeem E2E\n\n"
        f"- Run: `{args.run_id}`\n"
        f"- Mode: `{payload['mode']}`\n"
        f"- Status: **{payload['status']}**\n"
        f"- Submit called: `{payload['submit_called']}`\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload["status"] in {"DRY_RUN_PASS", "LIVE_REDEEM_CONFIRMED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
