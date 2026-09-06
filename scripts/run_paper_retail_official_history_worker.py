#!/usr/bin/env python3
"""Process durable retail official-wallet shadow import requests."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.core.db import postgres_connection  # noqa: E402
from quant.paper.public_api import PostgresPaperApiBackend  # noqa: E402
from quant.paper.retail_official_history import (  # noqa: E402
    RetailOfficialHistoryJobStore,
    RetailOfficialHistoryWorker,
    RetailOfficialHistoryWorkerConfig,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("once", "daemon"))
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("runtime_outputs/retail_official_history"),
    )
    parser.add_argument("--lease-seconds", type=int, default=3600)
    parser.add_argument("--command-timeout-seconds", type=int, default=3600)
    args = parser.parse_args()

    PostgresPaperApiBackend(
        pepper=b"internal-retail-official-history-schema-only"
    ).ensure_retail_schema()
    worker = RetailOfficialHistoryWorker(
        store=RetailOfficialHistoryJobStore(postgres_connection),
        config=RetailOfficialHistoryWorkerConfig(
            artifact_root=args.artifact_root,
            lease_seconds=args.lease_seconds,
            command_timeout_seconds=args.command_timeout_seconds,
        ),
    )
    if args.command == "once":
        result = worker.run_once()
        print(json.dumps(result, indent=2, default=str, sort_keys=True))
        return 0 if result["status"] in {"IDLE", "PASS", "DEGRADED"} else 2

    stopped = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    last_code = 0
    while not stopped:
        result = worker.run_once()
        print(json.dumps(result, default=str, sort_keys=True), flush=True)
        if result["status"] == "FAILED":
            last_code = 2
        if result["status"] == "IDLE":
            deadline = time.monotonic() + max(1.0, args.interval_seconds)
            while not stopped and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
    return last_code


if __name__ == "__main__":
    raise SystemExit(main())
