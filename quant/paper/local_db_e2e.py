"""Disposable local PostgreSQL migration and paper-worker acceptance."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import socket
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.core.db import PostgresSettings, postgres_connection
from quant.orderbook.local_event_bus import UnixEventPublisher

from .authority import (
    AuthorityLeaseController,
    AuthorityLeaseHandle,
    AuthorityLeaseLost,
    AuthorityLeaseStore,
    AuthorityLeaseUnavailable,
    ControlPlanePostgresConnectionFactory,
    FencedPostgresConnectionFactory,
)
from .db_migration import (
    CORE_PARITY_TABLES,
    apply_schema,
    latency_report,
    sync_market_catalog,
    sync_tables,
    verify_parity,
)
from .live_shadow_service import LivePaperShadowService
from .live_shadow_store import LiveShadowStore
from .paper_ledger import PostgresPaperLedgerSink
from .professional_execution import ProfessionalPaperExecutionKernel
from .taker_execution import (
    PaperLatencyModel,
    TakerExecutionConfig,
    TakerOnlyPaperExecutionEngine,
)

DEFAULT_CONTAINER = "poly-quant-paper-db-local-e2e"
DEFAULT_PORT = 55433
DEFAULT_DATABASE = "paper_target_e2e"
DEFAULT_OUTPUT_ROOT = Path("runtime_outputs/production")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (datetime, Decimal)):
        return str(value)
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_value(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def validate_disposable_target(
    source: PostgresSettings,
    target: PostgresSettings,
) -> None:
    if target.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("local E2E target must use a loopback address")
    if not target.database.startswith("paper_target_"):
        raise ValueError("local E2E database must start with paper_target_")
    if (
        target.host == source.host
        and target.port == source.port
        and target.database == source.database
    ):
        raise ValueError("local E2E target must not equal the source database")
    if target.port == source.port:
        raise ValueError("local E2E target must use an independent PostgreSQL port")


def _factory(settings: PostgresSettings) -> Callable[..., Any]:
    @contextmanager
    def connect(*, readonly: bool = False) -> Iterator[Any]:
        with postgres_connection(settings, readonly=readonly) as conn:
            yield conn

    return connect


def _run_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("docker", *args),
        check=check,
        capture_output=True,
        text=True,
    )


def _assert_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", int(port)))
        except OSError as exc:
            raise RuntimeError(f"local target port {port} is already in use") from exc


def _remove_owned_container(name: str) -> None:
    inspected = _run_docker(
        "inspect",
        "--format",
        '{{index .Config.Labels "io.poly-quant.purpose"}}',
        name,
        check=False,
    )
    if inspected.returncode != 0:
        return
    if inspected.stdout.strip() != "paper-db-local-e2e":
        raise RuntimeError(f"refusing to remove unowned Docker container {name}")
    _run_docker("rm", "-f", name)


def _start_postgres(
    *,
    name: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> None:
    _remove_owned_container(name)
    _assert_port_available(port)
    _run_docker(
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        "io.poly-quant.purpose=paper-db-local-e2e",
        "--publish",
        f"127.0.0.1:{port}:5432",
        "--shm-size",
        "512m",
        "--env",
        f"POSTGRES_USER={user}",
        "--env",
        f"POSTGRES_PASSWORD={password}",
        "--env",
        f"POSTGRES_DB={database}",
        "postgres:16",
    )


def _wait_postgres(
    settings: PostgresSettings, *, timeout_seconds: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with (
                postgres_connection(settings, readonly=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute("SELECT 1 AS ready")
                if cur.fetchone()["ready"] == 1:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"temporary PostgreSQL did not become ready: {last_error}")


def _relation_isolation(settings: PostgresSettings) -> dict[str, Any]:
    forbidden = (
        "quant.paper_market_registry_tokens",
        "quant.paper_market_registry_markets",
        "quant.clob_l2_current_coverage",
        "core.markets",
    )
    with postgres_connection(settings, readonly=True) as conn, conn.cursor() as cur:
        present: dict[str, str | None] = {}
        for relation in forbidden:
            cur.execute("SELECT to_regclass(%s)::text AS relation", (relation,))
            present[relation] = cur.fetchone()["relation"]
    failures = [relation for relation, value in present.items() if value is not None]
    return {
        "status": "PASS" if not failures else "FAIL",
        "forbidden_relations": present,
        "failures": failures,
    }


def _candidate(settings: PostgresSettings) -> dict[str, Any]:
    with postgres_connection(settings, readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id,market_id,condition_id,market_slug,outcome_name,
                   event_id,category,enable_neg_risk,current_tick_size,min_order_size
            FROM quant.paper_execution_market_catalog catalog
            WHERE execution_eligible=TRUE AND active=TRUE
              AND closed=FALSE AND resolved=FALSE
              AND archived=FALSE AND deprecated=FALSE
              AND market_state='LIVE'
              AND enable_neg_risk=FALSE
              AND (
                  SELECT count(*)
                  FROM quant.paper_execution_market_catalog peer
                  WHERE peer.condition_id=catalog.condition_id
              ) >= 2
            ORDER BY source_updated_at DESC NULLS LAST,asset_id
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("no isolated binary LIVE catalog candidate is available")
    return dict(row)


def _prepare_target(
    settings: PostgresSettings,
    *,
    candidate: Mapping[str, Any],
    strategy_id: str,
) -> None:
    with postgres_connection(settings, readonly=False) as conn, conn.cursor() as cur:
        cur.execute("UPDATE quant.paper_live_watchlist SET enabled=FALSE")
        cur.execute(
            """
            UPDATE quant.paper_execution_market_catalog
            SET coverage_grade='A',has_gap=FALSE,synced_at=clock_timestamp()
            WHERE asset_id=%s
            """,
            (str(candidate["asset_id"]),),
        )
        cur.execute(
            "DELETE FROM quant.paper_strategy_risk_controls WHERE strategy_id=%s",
            (strategy_id,),
        )
        conn.commit()


async def _wait_until(
    predicate: Callable[[], Any],
    *,
    label: str,
    timeout_seconds: float = 15.0,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_value: Any = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        await asyncio.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {label}; last={last_value!r}")


def _book_message(asset_id: str) -> dict[str, Any]:
    event_ts_ms = int(time.time() * 1000)
    return {
        "event_type": "book",
        "asset_id": str(asset_id),
        "timestamp": event_ts_ms,
        "hash": f"local-e2e-{asset_id}-{event_ts_ms}",
        "bids": [
            {"price": "0.40", "size": "100"},
            {"price": "0.39", "size": "200"},
        ],
        "asks": [
            {"price": "0.60", "size": "100"},
            {"price": "0.61", "size": "200"},
        ],
    }


def _build_service(
    settings: PostgresSettings,
    *,
    socket_path: Path,
    status_path: Path,
    shutdown_path: Path,
    worker_id: str,
) -> tuple[
    LivePaperShadowService,
    LiveShadowStore,
    PostgresPaperLedgerSink,
    AuthorityLeaseController,
]:
    base_factory = _factory(settings)
    authority_handle = AuthorityLeaseHandle()
    authority_controller = AuthorityLeaseController(
        AuthorityLeaseStore(base_factory),
        partition_key="paper-local-e2e",
        owner_instance_id=worker_id,
        lease_seconds=10.0,
        heartbeat_seconds=2.0,
        handle=authority_handle,
    )
    worker_factory = FencedPostgresConnectionFactory(
        base_factory,
        authority_handle,
    )
    store = LiveShadowStore(worker_factory)
    ledger = PostgresPaperLedgerSink(
        worker_factory,
        initial_cash=Decimal(10000),
        ensure_schema=False,
    )
    engine = TakerOnlyPaperExecutionEngine(
        TakerExecutionConfig(
            latency=PaperLatencyModel(order_delay_ms=25),
            max_book_age_ms=10_000,
            fee_bps=Decimal(0),
        )
    )
    service = LivePaperShadowService(
        store=store,
        client=None,
        external_event_socket=socket_path,
        external_sources=("primary", "secondary"),
        external_feed_stale_seconds=10.0,
        engine=engine,
        execution_kernel=ProfessionalPaperExecutionKernel(engine),
        market_terms_resolver=None,
        portfolio_store=ledger,
        max_watch_assets=1,
        seed_watchlist_limit=0,
        watch_refresh_seconds=1.0,
        intent_poll_seconds=0.05,
        health_seconds=0.5,
        settlement_poll_seconds=3600.0,
        db_operation_timeout_seconds=10.0,
        nav_snapshot_seconds=1.0,
        nav_history_seconds=60.0,
        status_path=status_path,
        health_spool_path=status_path.with_name("health-spool.jsonl"),
        shutdown_path=shutdown_path,
        worker_id=worker_id,
        authority_controller=authority_controller,
    )
    return service, store, ledger, authority_controller


async def _publish_redundant_book(
    socket_path: Path,
    *,
    asset_id: str,
) -> tuple[UnixEventPublisher, UnixEventPublisher]:
    publishers = (
        UnixEventPublisher(
            socket_path,
            source="primary",
            producer_id="local-e2e-primary",
            reconnect_seconds=0.1,
        ),
        UnixEventPublisher(
            socket_path,
            source="secondary",
            producer_id="local-e2e-secondary",
            reconnect_seconds=0.1,
        ),
    )
    for publisher in publishers:
        publisher.start()
    await _wait_until(
        lambda: all(publisher.connected for publisher in publishers),
        label="both local L2 publishers",
    )
    message = _book_message(asset_id)
    for publisher in publishers:
        if not publisher.publish(messages=(message,)):
            raise RuntimeError(f"failed to enqueue {publisher.source} snapshot")
    await _wait_until(
        lambda: all(publisher.published >= 1 for publisher in publishers),
        label="both local L2 snapshots",
    )
    return publishers


def _terminal_result(store: LiveShadowStore, intent_id: int) -> dict[str, Any] | None:
    row = store.load_intent(intent_id)
    if row is None or row.get("status") not in {"COMPLETED", "FAILED", "CANCELED"}:
        return None
    return row


def _strategy_counts(settings: PostgresSettings, strategy_id: str) -> dict[str, int]:
    queries = {
        "intents": (
            "SELECT count(*) AS n FROM quant.paper_live_order_intents WHERE strategy_id=%s",
            (strategy_id,),
        ),
        "fills": (
            "SELECT count(*) AS n FROM quant.paper_fills WHERE strategy_id=%s",
            (strategy_id,),
        ),
        "ledger_entries": (
            "SELECT count(*) AS n FROM quant.paper_ledger_entries WHERE strategy_id=%s",
            (strategy_id,),
        ),
        "journal_lines": (
            "SELECT count(*) AS n FROM quant.paper_journal_lines WHERE strategy_id=%s",
            (strategy_id,),
        ),
        "active_reservations": (
            "SELECT count(*) AS n FROM quant.paper_order_reservations WHERE strategy_id=%s AND status='ACTIVE'",
            (strategy_id,),
        ),
    }
    with postgres_connection(settings, readonly=True) as conn, conn.cursor() as cur:
        result: dict[str, int] = {}
        for name, (query, params) in queries.items():
            cur.execute(query, params)
            result[name] = int(cur.fetchone()["n"])
    return result


async def _stop_service(
    task: asyncio.Task[Any],
    shutdown_path: Path,
) -> Any:
    shutdown_path.parent.mkdir(parents=True, exist_ok=True)
    shutdown_path.touch()
    return await asyncio.wait_for(task, timeout=15.0)


async def _run_worker_acceptance(
    settings: PostgresSettings,
    *,
    output_dir: Path,
    candidate: Mapping[str, Any],
    strategy_id: str,
) -> dict[str, Any]:
    socket_path = output_dir / "paper-events.sock"
    status_path = output_dir / "worker-status.json"
    shutdown_path = output_dir / "shutdown.request"
    asset_id = str(candidate["asset_id"])
    base_factory = _factory(settings)
    control_factory = ControlPlanePostgresConnectionFactory(base_factory)
    setup_store = LiveShadowStore(control_factory)
    setup_ledger = PostgresPaperLedgerSink(
        control_factory,
        initial_cash=Decimal(10000),
        ensure_schema=False,
    )
    setup_ledger.ensure_account(strategy_id)
    if not setup_store.watch(asset_id, strategy_id=strategy_id, reason="intent_asset"):
        raise RuntimeError("candidate could not be added to the target watchlist")

    AuthorityLeaseStore(base_factory).set_fencing_enforced(True)
    service, _, _, first_authority = _build_service(
        settings,
        socket_path=socket_path,
        status_path=status_path,
        shutdown_path=shutdown_path,
        worker_id="paper-local-e2e-first",
    )
    service_task = asyncio.create_task(service.run(max_seconds=45.0))
    publishers: tuple[UnixEventPublisher, UnixEventPublisher] = ()
    first_token = None
    contender_rejected = False
    try:
        await _wait_until(socket_path.exists, label="paper event socket")
        first_token = first_authority.token
        if first_token is None:
            raise RuntimeError("first worker did not acquire authority")
        contender = AuthorityLeaseController(
            AuthorityLeaseStore(base_factory),
            partition_key="paper-local-e2e",
            owner_instance_id="paper-local-e2e-contender",
            lease_seconds=10.0,
            heartbeat_seconds=2.0,
        )
        try:
            contender.acquire()
        except AuthorityLeaseUnavailable:
            contender_rejected = True
        if not contender_rejected:
            contender.release()
            raise RuntimeError("concurrent authority contender was not rejected")
        await _wait_until(
            lambda: asset_id in service.targets,
            label="candidate watch target",
        )
        publishers = await _publish_redundant_book(socket_path, asset_id=asset_id)
        await _wait_until(
            lambda: (
                bool(service.history.get(asset_id))
                and service.history[asset_id][-1].coverage_grade == "A"
                and not service.history[asset_id][-1].has_gap
            ),
            label="redundant grade-A book",
        )

        size = max(Decimal(5), Decimal(str(candidate.get("min_order_size") or 1)))
        buy_client_id = f"{strategy_id}-buy"
        buy_intent_id = setup_store.submit(
            strategy_id=strategy_id,
            client_order_id=buy_client_id,
            asset_id=asset_id,
            side="BUY",
            time_in_force="FAK",
            limit_price=Decimal("0.60"),
            size=size,
            post_only=False,
            decision_ts=_now(),
        )
        buy_row = await _wait_until(
            lambda: _terminal_result(setup_store, buy_intent_id),
            label="paper BUY terminal result",
        )
        duplicate_intent_id = setup_store.submit(
            strategy_id=strategy_id,
            client_order_id=buy_client_id,
            asset_id=asset_id,
            side="BUY",
            time_in_force="FAK",
            limit_price=Decimal("0.60"),
            size=size,
            post_only=False,
            decision_ts=_now(),
        )
        buy_result = dict(buy_row["result"] or {})
        if buy_result.get("status") != "FILLED":
            raise RuntimeError(f"paper BUY was not filled: {buy_result}")
        if duplicate_intent_id != buy_intent_id:
            raise RuntimeError("client_order_id idempotency failed")
        after_buy = setup_ledger.portfolio_snapshot(strategy_id, asset_id)
        if after_buy.position_size != size:
            raise RuntimeError(f"unexpected position after BUY: {after_buy}")

        for publisher in publishers:
            publisher.publish(messages=(_book_message(asset_id),))
        await asyncio.sleep(0.1)
        sell_intent_id = setup_store.submit(
            strategy_id=strategy_id,
            client_order_id=f"{strategy_id}-sell",
            asset_id=asset_id,
            side="SELL",
            time_in_force="FAK",
            limit_price=Decimal("0.40"),
            size=size,
            post_only=False,
            decision_ts=_now(),
        )
        sell_row = await _wait_until(
            lambda: _terminal_result(setup_store, sell_intent_id),
            label="paper SELL terminal result",
        )
        sell_result = dict(sell_row["result"] or {})
        if sell_result.get("status") != "FILLED":
            raise RuntimeError(f"paper SELL was not filled: {sell_result}")
        after_sell = setup_ledger.portfolio_snapshot(strategy_id, asset_id)
        expected_cash = Decimal(10000) - size * Decimal("0.60") + size * Decimal("0.40")
        if after_sell.position_size != 0 or after_sell.cash_balance != expected_cash:
            raise RuntimeError(
                f"unexpected portfolio after round trip: {after_sell}, expected_cash={expected_cash}"
            )
        summary = setup_ledger.summary(strategy_id=strategy_id)
        if Decimal(str(summary["account"]["realized_pnl"])) != -size * Decimal("0.20"):
            raise RuntimeError(f"unexpected realized PnL: {summary}")
        counts_before_restart = _strategy_counts(settings, strategy_id)
    finally:
        for publisher in publishers:
            await publisher.close()
        if not service_task.done():
            await _stop_service(service_task, shutdown_path)
        else:
            await service_task

    restart_service, _, _, restart_authority = _build_service(
        settings,
        socket_path=socket_path,
        status_path=output_dir / "worker-restart-status.json",
        shutdown_path=shutdown_path,
        worker_id="paper-local-e2e-restart",
    )
    restart_task = asyncio.create_task(restart_service.run(max_seconds=20.0))
    restart_publishers: tuple[UnixEventPublisher, UnixEventPublisher] = ()
    stale_writer_checks: dict[str, dict[str, Any]] = {}
    restart_token = None
    try:
        await _wait_until(socket_path.exists, label="restarted paper event socket")
        restart_token = restart_authority.token
        if restart_token is None or first_token is None:
            raise RuntimeError("restart worker did not acquire authority")
        if restart_token.lease_epoch != first_token.lease_epoch + 1:
            raise RuntimeError(
                "authority epoch did not advance on takeover: "
                f"{first_token.lease_epoch} -> {restart_token.lease_epoch}"
            )
        stale_factory = FencedPostgresConnectionFactory(
            base_factory,
            AuthorityLeaseHandle(first_token),
        )
        stale_mutations = {
            "order": (
                """
                UPDATE quant.paper_live_order_intents
                SET updated_at=clock_timestamp()
                WHERE intent_id=%s
                """,
                (buy_intent_id,),
            ),
            "ledger": (
                """
                UPDATE quant.paper_accounts
                SET updated_at=clock_timestamp()
                WHERE strategy_id=%s
                """,
                (strategy_id,),
            ),
        }
        for name, (query, params) in stale_mutations.items():
            rejected = False
            error = None
            try:
                with stale_factory(readonly=False) as conn, conn.cursor() as cur:
                    cur.execute(query, params)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                rejected = "stale or expired lease epoch" in str(exc)
            stale_writer_checks[name] = {"rejected": rejected, "error": error}
        if not all(row["rejected"] for row in stale_writer_checks.values()):
            raise RuntimeError(
                f"database accepted stale authority epoch: {stale_writer_checks}"
            )
        restart_publishers = await _publish_redundant_book(
            socket_path,
            asset_id=asset_id,
        )
        await _wait_until(
            lambda: (
                bool(restart_service.history.get(asset_id))
                and restart_service.history[asset_id][-1].coverage_grade == "A"
            ),
            label="restarted grade-A book",
        )
        await asyncio.sleep(0.5)
    finally:
        for publisher in restart_publishers:
            await publisher.close()
        if not restart_task.done():
            restart_stats = await _stop_service(restart_task, shutdown_path)
        else:
            restart_stats = await restart_task

    counts_after_restart = _strategy_counts(settings, strategy_id)
    after_restart = setup_ledger.portfolio_snapshot(strategy_id, asset_id)
    if counts_after_restart != counts_before_restart:
        raise RuntimeError(
            f"worker restart changed durable row counts: {counts_before_restart} -> {counts_after_restart}"
        )
    if after_restart != after_sell:
        raise RuntimeError(
            f"worker restart changed portfolio: {after_sell} -> {after_restart}"
        )
    crashed_owner = AuthorityLeaseController(
        AuthorityLeaseStore(base_factory),
        partition_key="paper-local-e2e-expiry",
        owner_instance_id="paper-local-e2e-crashed",
        lease_seconds=0.4,
        heartbeat_seconds=0.1,
    )
    crashed_token = crashed_owner.acquire()
    await asyncio.sleep(0.5)
    failover_owner = AuthorityLeaseController(
        AuthorityLeaseStore(base_factory),
        partition_key="paper-local-e2e-expiry",
        owner_instance_id="paper-local-e2e-failover",
        lease_seconds=2.0,
        heartbeat_seconds=0.5,
    )
    failover_token = failover_owner.acquire()
    expired_owner_failed_closed = False
    try:
        crashed_owner.heartbeat()
    except AuthorityLeaseLost:
        expired_owner_failed_closed = not crashed_owner.held
    if not expired_owner_failed_closed:
        raise RuntimeError("expired owner did not fail closed on heartbeat")
    if failover_token.lease_epoch != crashed_token.lease_epoch + 1:
        raise RuntimeError("expired lease takeover did not advance epoch")
    failover_owner.release()
    return {
        "status": "PASS",
        "strategy_id": strategy_id,
        "paper_only": True,
        "live_submission_performed": False,
        "candidate": dict(candidate),
        "book": {"best_bid": "0.40", "best_ask": "0.60", "depth_per_top": "100"},
        "buy": buy_result,
        "sell": sell_result,
        "idempotency": {
            "first_intent_id": buy_intent_id,
            "duplicate_intent_id": duplicate_intent_id,
            "status": "PASS",
        },
        "portfolio_after_buy": after_buy,
        "portfolio_after_sell": after_sell,
        "portfolio_after_restart": after_restart,
        "portfolio_summary": summary,
        "counts_before_restart": counts_before_restart,
        "counts_after_restart": counts_after_restart,
        "restart": {
            "status": "PASS",
            "transport_state_at_exit": restart_stats.transport_state,
            "last_error": restart_stats.last_error,
        },
        "authority": {
            "status": "PASS",
            "partition_key": "paper-local-e2e",
            "fencing_enforced": True,
            "first_owner": first_token.owner_instance_id,
            "first_epoch": first_token.lease_epoch,
            "contender_rejected": contender_rejected,
            "restart_owner": restart_token.owner_instance_id,
            "restart_epoch": restart_token.lease_epoch,
            "stale_writer_rejected_by_database": True,
            "stale_writer_checks": stale_writer_checks,
            "expiry_failover": {
                "status": "PASS",
                "crashed_epoch": crashed_token.lease_epoch,
                "failover_epoch": failover_token.lease_epoch,
                "expired_owner_failed_closed": expired_owner_failed_closed,
            },
        },
    }


def _source_strategy_rows(settings: PostgresSettings, strategy_id: str) -> int:
    with postgres_connection(settings, readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM quant.paper_live_order_intents WHERE strategy_id=%s",
            (strategy_id,),
        )
        return int(cur.fetchone()["n"])


def run_local_acceptance(
    *,
    source: PostgresSettings,
    port: int = DEFAULT_PORT,
    database: str = DEFAULT_DATABASE,
    container: str = DEFAULT_CONTAINER,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    keep_target: bool = False,
) -> dict[str, Any]:
    run_id = _now().strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(output_root) / f"paper-db-local-e2e-{run_id}"
    report_path = output_dir / "acceptance.json"
    latest_path = Path(output_root) / "paper-db-local-e2e-latest.json"
    user = "paper_e2e"
    password = secrets.token_urlsafe(32)
    target = PostgresSettings(
        host="127.0.0.1",
        port=int(port),
        user=user,
        password=password,
        database=database,
        search_path="quant,core,oracle,ops,public",
        connect_timeout_seconds=3,
    )
    validate_disposable_target(source, target)
    strategy_id = f"paper-db-local-e2e-{run_id}"
    report: dict[str, Any] = {
        "schema_version": "paper_db_local_e2e_v2",
        "generated_at": _now(),
        "status": "RUNNING",
        "report_path": str(report_path.resolve()),
        "scope": {
            "local_only": True,
            "target_host": target.host,
            "target_port": target.port,
            "target_database": target.database,
            "source_database": source.database,
            "gcp_accessed": False,
            "live_submission_performed": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(report_path, report)
    try:
        _start_postgres(
            name=container,
            port=target.port,
            user=target.user,
            password=target.password,
            database=target.database,
        )
        _wait_postgres(target)
        report["migration"] = {
            "schema": apply_schema(target),
            "snapshot": sync_tables(source, target),
            "catalog": sync_market_catalog(source, target),
        }
        report["migration"]["core_parity"] = verify_parity(
            source,
            target,
            tables=CORE_PARITY_TABLES,
        )
        report["migration"]["latency"] = latency_report(target, samples=10)
        report["relation_isolation"] = _relation_isolation(target)
        if report["migration"]["core_parity"]["status"] != "PASS":
            raise RuntimeError("core authority migration parity failed")
        if report["relation_isolation"]["status"] != "PASS":
            raise RuntimeError("target unexpectedly contains data-plane relations")
        candidate = _candidate(target)
        _prepare_target(
            target,
            candidate=candidate,
            strategy_id=strategy_id,
        )
        if _source_strategy_rows(source, strategy_id) != 0:
            raise RuntimeError(
                "isolated strategy unexpectedly exists in source database"
            )
        report["worker_acceptance"] = asyncio.run(
            _run_worker_acceptance(
                target,
                output_dir=output_dir,
                candidate=candidate,
                strategy_id=strategy_id,
            )
        )
        source_rows_after = _source_strategy_rows(source, strategy_id)
        report["source_read_only"] = {
            "status": "PASS" if source_rows_after == 0 else "FAIL",
            "isolated_strategy_rows": source_rows_after,
        }
        if source_rows_after:
            raise RuntimeError("source database received local E2E strategy rows")
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["completed_at"] = _now()
        report["target_retained"] = bool(keep_target)
        _write_json(report_path, report)
        _write_json(latest_path, report)
        if not keep_target:
            _remove_owned_container(container)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--keep-target", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_local_acceptance(
        source=PostgresSettings(),
        port=args.port,
        database=args.database,
        container=args.container,
        output_root=args.output_root,
        keep_target=args.keep_target,
    )
    migration = report.get("migration") or {}
    worker = report.get("worker_acceptance") or {}
    print(
        json.dumps(
            _json_value(
                {
                    "status": report["status"],
                    "report_path": report["report_path"],
                    "snapshot_rows": (migration.get("snapshot") or {}).get(
                        "total_rows"
                    ),
                    "catalog_rows": (migration.get("catalog") or {}).get("rows"),
                    "core_parity": (migration.get("core_parity") or {}).get("status"),
                    "worker_acceptance": worker.get("status"),
                    "buy_status": (worker.get("buy") or {}).get("status"),
                    "sell_status": (worker.get("sell") or {}).get("status"),
                    "restart_status": (worker.get("restart") or {}).get("status"),
                    "source_read_only": (report.get("source_read_only") or {}).get(
                        "status"
                    ),
                    "target_retained": report["target_retained"],
                }
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
