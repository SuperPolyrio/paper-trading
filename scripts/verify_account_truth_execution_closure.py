#!/usr/bin/env python3
"""Verify a hash-bound LIVE execution and account-truth evidence bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.simulator.account_truth.execution_closure import (  # noqa: E402
    verify_account_truth_execution_closure,
    write_account_truth_execution_closure,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_directory", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "reports/simulator_acceptance/account-truth-execution-closure.json"
        ),
    )
    args = parser.parse_args()
    report = verify_account_truth_execution_closure(args.evidence_directory)
    output = write_account_truth_execution_closure(args.output, report)
    print(json.dumps({**report, "output": str(output)}, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
