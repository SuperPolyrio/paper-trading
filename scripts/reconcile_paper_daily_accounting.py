#!/usr/bin/env python3
"""Build the daily paper cash, position, and reservation closure report."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant.paper.paper_ledger import PostgresPaperLedgerSink  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-id")
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--tolerance", type=Decimal, default=Decimal("0.00000001"))
    parser.add_argument(
        "--json-out",
        type=Path,
        default=ROOT / "runtime_outputs/paper_accounting/daily-latest.json",
    )
    args = parser.parse_args()

    ledger = PostgresPaperLedgerSink()
    rows = ledger.build_daily_accounting_snapshot(
        strategy_id=args.strategy_id,
        accounting_date=args.date,
        tolerance=args.tolerance,
    )
    payload = {
        "schema_version": "paper_daily_accounting_v1",
        "status": "PASS" if rows and all(bool(row["passed"]) for row in rows) else "FAIL",
        "account_count": len(rows),
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "rows": rows,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(payload, default=str, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "schema_version": payload["schema_version"],
                "status": payload["status"],
                "account_count": payload["account_count"],
                "passed_count": payload["passed_count"],
                "json_out": str(args.json_out),
            },
            sort_keys=True,
        )
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
