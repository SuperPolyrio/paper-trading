"""CLI and daemon entrypoint for official account economics synchronization."""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from quant.core.db import postgres_connection
from quant.paper.authority import ControlPlanePostgresConnectionFactory

from .official_account_sync import (
    OFFICIAL_ACTIVITY_TYPES,
    OfficialAccountSyncService,
    PostgresOfficialAccountSyncStore,
)
from .official_reward_client import PolymarketOfficialRewardClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("sync-once", "backfill", "calibrate", "daemon", "status")
    )
    parser.add_argument("--account-address", default="")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--strategy-id", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--lookback-hours", type=float, default=72.0)
    parser.add_argument("--interval-seconds", type=float, default=900.0)
    parser.add_argument("--bridge-address", action="append", default=[])
    parser.add_argument("--activity-type", action="append", default=[])
    parser.add_argument("--proxy-url", default="")
    parser.add_argument("--data-api-url", default="")
    parser.add_argument("--clob-url", default="")
    parser.add_argument("--bridge-api-url", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--strict-sources", action="store_true")
    parser.add_argument("--skip-daily-sources", action="store_true")
    parser.add_argument("--sync-reward-rules", action="store_true")
    parser.add_argument("--skip-reward-rules", action="store_true")
    parser.add_argument("--no-sdk", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = _parser().parse_args(argv)
    account = _required_address(args.account_address)
    strategy_id = (
        str(args.strategy_id).strip()
        or os.environ.get("POLY_QUANT_REWARD_STRATEGY_ID", "").strip()
        or f"official-wallet:{account}"
    )
    account_id = (
        str(args.account_id).strip()
        or os.environ.get("POLY_QUANT_REWARD_ACCOUNT_ID", "").strip()
        or account
    )
    output_dir = Path(
        str(args.output_dir).strip()
        or os.environ.get(
            "POLY_QUANT_REWARD_REPORT_DIR",
            "runtime_outputs/official_account_economics",
        )
    )
    accounting_factory = ControlPlanePostgresConnectionFactory(postgres_connection)
    store = PostgresOfficialAccountSyncStore(accounting_factory)
    if args.command == "status":
        print(json.dumps(_status(store, account, strategy_id), indent=2, default=str))
        return 0
    if args.command == "calibrate":
        as_of = _timestamp(args.end) if args.end else datetime.now(timezone.utc)
        report = store.calibration_report(
            account_address=account,
            strategy_id=strategy_id,
            as_of=as_of,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] != "MISMATCH" else 2

    proxy_url = (
        str(args.proxy_url).strip()
        or os.environ.get("POLY_QUANT_REWARD_PROXY_URL", "").strip()
        or None
    )
    http_timeout = float(
        os.environ.get("POLY_QUANT_REWARD_HTTP_TIMEOUT_SECONDS", "10")
    )
    client = PolymarketOfficialRewardClient(
        sdk_client=(
            None
            if args.no_sdk
            else _sdk_client(proxy_url=proxy_url, timeout_seconds=http_timeout)
        ),
        base_url=(
            str(args.clob_url).strip()
            or os.environ.get("POLY_QUANT_REWARD_CLOB_URL", "").strip()
            or "https://clob.polymarket.com"
        ),
        data_base_url=(
            str(args.data_api_url).strip()
            or os.environ.get("POLY_QUANT_REWARD_DATA_API_URL", "").strip()
            or os.environ.get("POLYDATA_POLYMARKET_DATA_API_BASE", "").strip()
            or "https://data-api.polymarket.com"
        ),
        bridge_base_url=(
            str(args.bridge_api_url).strip()
            or os.environ.get("POLY_QUANT_REWARD_BRIDGE_API_URL", "").strip()
            or "https://bridge.polymarket.com"
        ),
        proxy_url=proxy_url,
        timeout_seconds=http_timeout,
    )
    bridge_addresses = tuple(
        dict.fromkeys(
            [
                *args.bridge_address,
                *filter(
                    None,
                    (
                        item.strip()
                        for item in os.environ.get(
                            "POLY_QUANT_REWARD_BRIDGE_ADDRESSES", ""
                        ).split(",")
                    ),
                ),
            ]
        )
    )
    service = OfficialAccountSyncService(
        client=client,
        connection_factory=accounting_factory,
        account_address=account,
        strategy_id=strategy_id,
        account_id=account_id,
        bridge_addresses=bridge_addresses,
        output_dir=output_dir,
    )
    activity_types = tuple(args.activity_type) or OFFICIAL_ACTIVITY_TYPES
    if args.command in {"sync-once", "backfill"}:
        end = _timestamp(args.end) if args.end else datetime.now(timezone.utc)
        start = (
            _timestamp(args.start)
            if args.start
            else end - timedelta(hours=args.lookback_hours)
        )
        result = service.sync_once(
            window_start=start,
            window_end=end,
            activity_types=activity_types,
            include_daily_sources=not args.skip_daily_sources,
            include_reward_rules=args.sync_reward_rules or not args.skip_reward_rules,
            strict_sources=args.strict_sources,
        )
        print(json.dumps(_jsonable(asdict(result)), indent=2, sort_keys=True))
        return 0 if result.status in {"PASS", "DEGRADED"} else 2

    stopped = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=args.lookback_hours)
        result = service.sync_once(
            window_start=start,
            window_end=end,
            activity_types=activity_types,
            include_daily_sources=not args.skip_daily_sources,
            include_reward_rules=args.sync_reward_rules or not args.skip_reward_rules,
            strict_sources=args.strict_sources,
        )
        print(json.dumps(_jsonable(asdict(result)), sort_keys=True), flush=True)
        deadline = time.monotonic() + max(1.0, args.interval_seconds)
        while not stopped and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    return 0


def _load_env() -> None:
    for path in (
        Path(".env"),
        Path(".env.local"),
        Path.home() / ".config/prediction-market-quant/official-account-sync.env",
        Path.home() / ".config/prediction-market-quant/calibration.env",
    ):
        if path.exists():
            load_dotenv(path, override=False)


def _required_address(explicit: str) -> str:
    for value in (
        explicit,
        os.environ.get("POLY_QUANT_REWARD_WALLET_ADDRESS", ""),
        os.environ.get("POLYMARKET_FUNDER_ADDRESS", ""),
        os.environ.get("POLYMARKET_USER_ADDRESS", ""),
    ):
        normalized = str(value).strip().lower()
        if normalized:
            if not normalized.startswith("0x") or len(normalized) != 42:
                raise ValueError("official reward wallet must be a 20-byte EVM address")
            return normalized
    raise ValueError("official reward wallet address is not configured")


def _sdk_client(*, proxy_url: str | None, timeout_seconds: float) -> Any | None:
    api_key = _first_env("POLY_QUANT_PROBE_API_KEY", "POLYMARKET_API_KEY")
    api_secret = _first_env("POLY_QUANT_PROBE_API_SECRET", "POLYMARKET_API_SECRET")
    api_passphrase = _first_env(
        "POLY_QUANT_PROBE_API_PASSPHRASE", "POLYMARKET_API_PASSPHRASE"
    )
    private_key = _first_env("POLY_QUANT_PROBE_PRIVATE_KEY", "POLYMARKET_PRIVATE_KEY")
    if not all((api_key, api_secret, api_passphrase, private_key)):
        return None
    try:
        import httpx
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
        from py_clob_client_v2.http_helpers import helpers
    except ImportError as exc:
        raise RuntimeError(
            "authenticated reward SDK requires the prediction-market-quant conda env"
        ) from exc
    old = getattr(helpers, "_http_client", None)
    if old is not None:
        old.close()
    helpers._http_client = httpx.Client(
        http2=True,
        proxy=proxy_url,
        trust_env=False,
        timeout=httpx.Timeout(float(timeout_seconds)),
    )
    return ClobClient(
        os.environ.get("POLY_QUANT_REWARD_CLOB_URL", "https://clob.polymarket.com"),
        chain_id=137,
        key=private_key,
        creds=ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        ),
        signature_type=int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2")),
        funder=(
            os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip() or None
        ),
        use_server_time=True,
        retry_on_error=False,
    )


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _timestamp(value: str) -> datetime:
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.fromtimestamp(float(text), tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _status(
    store: PostgresOfficialAccountSyncStore, account: str, strategy_id: str
) -> dict[str, Any]:
    with store.connection_factory(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM quant.paper_official_account_sync_runs
            WHERE account_address=%s AND strategy_id=%s
            ORDER BY started_at DESC LIMIT 1
            """,
            (account, strategy_id),
        )
        run = cur.fetchone()
        cur.execute(
            """
            SELECT stream_key,watermark_ts,last_success_at
            FROM quant.paper_official_account_sync_checkpoints
            WHERE account_address=%s ORDER BY source,stream_key
            """,
            (account,),
        )
        checkpoints = cur.fetchall()
    return {
        "schema_version": "official-account-sync-status-v1",
        "account_address": account,
        "strategy_id": strategy_id,
        "latest_run": dict(run) if run is not None else None,
        "checkpoints": [dict(row) for row in checkpoints],
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
