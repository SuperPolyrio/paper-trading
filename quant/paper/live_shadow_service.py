"""WS-first live paper execution shadow service and CLI."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import hashlib
import json
import os
import signal
import time
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, TypeGuard
from uuid import uuid4

os.environ.setdefault("POLY_QUANT_DISABLE_DOTENV", "1")

from quant.adapters.polymarket_market_ws_client import PolymarketMarketWsClient
from quant.adapters.polymarket_paper_clob_client import PolymarketPaperClobClient
from quant.core.db import (
    ThreadLocalPostgresConnectionFactory,
    postgres_connection,
)
from quant.execution.backpressure import BackpressureSnapshot, evaluate_backpressure
from quant.execution.models.maker_model_domain import MakerModelDomainResolver
from quant.execution.models.marks import MarkInput, build_marks
from quant.orderbook.l2_subscription_snapshot import (
    L2SubscriptionEntry,
    write_subscription_snapshot,
)
from quant.orderbook.local_book import LocalOrderBook
from quant.orderbook.local_event_bus import LocalEventEnvelope, UnixEventServer
from quant.orderbook.polymarket_adapter import (
    NormalizedBookDelta,
    NormalizedBookEvent,
    NormalizedBookSnapshot,
    NormalizedTradeEvent,
    normalize_polymarket_event,
    normalize_polymarket_trade,
)
from quant.orderbook.service import RealtimeOrderBookService
from quant.orderbook.subscription_reconciler import (
    DesiredSubscription,
    build_realtime_targets_from_desired,
)
from quant.paper.security import enforce_paper_security_boundary
from quant.risk.event_risk import ExitLevel
from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    PolymarketGeoblockClient,
    PostgresAdmissionStore,
    UnifiedAdmissionService,
    order_exposure_effect,
)
from quant.simulator.economics import LiquidityRole
from quant.simulator.finality import (
    DurablePaperFillFinality,
    PostgresFillFinalityStore,
)
from quant.simulator.lifecycle_scheduler_shadow import (
    PaperLifecycleSchedulerShadow,
)
from quant.simulator.liquidity import (
    DurablePaperLiquidityOverlay,
    PostgresLiquidityOverlayStore,
    ShadowComparingPaperLiquidityOverlay,
)
from quant.simulator.observability import (
    DegradationController,
    DegradationScope,
    TokenDataState,
)
from quant.simulator.oms import (
    DurablePaperOmsGate,
    OmsAdmissionStatus,
    PostgresOwnOrderStore,
    SelfTradePolicy,
)
from quant.simulator.paper_worker_scheduler import (
    DeterministicPaperIntentScheduler,
)
from quant.simulator.run_artifact_store import PostgresSimulatorArtifactStore
from quant.simulator.shadow_worker import SchedulerOwnedShadowWorker
from quant.simulator.venue import (
    GatewayConfig,
    HeartbeatConfig,
    VenueAdmissionRequest,
    VenueAdmissionShadow,
    VenueGateway,
)

from .authority import (
    AuthorityLeaseController,
    AuthorityLeaseHandle,
    AuthorityLeaseStore,
    ControlPlanePostgresConnectionFactory,
    FencedPostgresConnectionFactory,
)
from .execution_profile import ExecutionProfileDecision, ExecutionProfileResolver
from .health_spool import HealthSpool, HealthSpoolBatch
from .live_shadow_store import (
    LiveShadowStore,
    LiveWatchTarget,
    MakerQueueAdvancePlan,
    MakerResearchBookEvent,
    QueuedIntent,
)
from .market_terms import (
    CachedPolymarketTermsResolver,
    MarketTermsUnavailable,
)
from .operations import load_paper_admission
from .paper_ledger import PostgresPaperLedgerSink
from .persistent_event_kernel import (
    AppliedKernelEvent,
    DurableCausalEvent,
    PostgresPersistentEventKernelStore,
)
from .portfolio_analytics import PaperAssetMark
from .professional_execution import (
    CentralPaperRiskGate,
    PaperFidelityContext,
    PaperRiskContext,
    PaperRiskLimits,
    ProfessionalPaperExecutionKernel,
)
from .route_health import has_independent_connected_routes
from .taker_execution import (
    ArrivalBookCheckpoint,
    OrderIntent,
    PaperBookLevel,
    PaperExecutionResult,
    PaperLatencyModel,
    PaperPortfolioSnapshot,
    PaperTakerFill,
    TakerExecutionConfig,
    TakerOnlyPaperExecutionEngine,
    calculate_fill_fee,
    paper_execution_result_from_payload,
)

DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_STATUS = Path("runtime_outputs/paper_live_shadow/status.json")


_INTENT_DB_AFFINITY: ContextVar[bool] = ContextVar(
    "paper_intent_db_affinity",
    default=False,
)


@dataclass
class PendingIntent:
    row: QueuedIntent
    decision_checkpoint: ArrivalBookCheckpoint | None
    execution_profile: ExecutionProfileDecision | None = None


@dataclass(frozen=True)
class FeedEnvelope:
    source: str
    messages: tuple[dict[str, Any], ...] = ()
    state: str | None = None
    error: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class CausalCheckpointRequest:
    intent_id: int
    asset_id: str
    as_of: datetime
    result: asyncio.Future[ArrivalBookCheckpoint | None]
    event_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class CausalLifecycleEvent:
    intent_id: int
    event_type: str
    event_ts: datetime
    asset_id: str | None
    payload: dict[str, Any]
    result: asyncio.Future[int]


def _is_plain_feed_envelope(item: object) -> TypeGuard[FeedEnvelope]:
    return bool(
        isinstance(item, FeedEnvelope)
        and item.messages
        and item.state is None
        and item.error is None
    )


@dataclass
class LiveShadowStats:
    worker_id: str
    build_id: str | None = None
    transport_state: str = "STARTING"
    watched_assets: int = 0
    ready_books: int = 0
    execution_watched_assets: int = 0
    execution_ready_books: int = 0
    execution_fresh_books: int = 0
    fresh_books: int = 0
    stale_books: int = 0
    queued_intents: int = 0
    processing_intents: int = 0
    completed_intents: int = 0
    rejected_intents: int = 0
    websocket_messages: int = 0
    reconnects: int = 0
    duplicate_events: int = 0
    out_of_order_events: int = 0
    feed_mismatch_assets: int = 0
    last_message_at: str | None = None
    last_error: str | None = None
    started_at: str = field(default_factory=lambda: _now().isoformat())
    updated_at: str = field(default_factory=lambda: _now().isoformat())
    ready_asset_sample: list[str] = field(default_factory=list)
    fresh_asset_sample: list[str] = field(default_factory=list)
    fresh_book_sample: list[dict[str, Any]] = field(default_factory=list)
    route_states: dict[str, str] = field(default_factory=dict)
    route_proxy_urls: dict[str, str | None] = field(default_factory=dict)
    route_messages: dict[str, int] = field(default_factory=dict)
    route_last_message_at: dict[str, str | None] = field(default_factory=dict)
    route_last_transport_at: dict[str, str | None] = field(default_factory=dict)
    route_reconnects: dict[str, int] = field(default_factory=dict)
    external_subscription_count: int = 0
    external_subscription_sha256: str | None = None
    external_subscription_syncs: int = 0
    external_subscription_failures: int = 0
    last_external_subscription_error: str | None = None
    settled_positions: int = 0
    rest_resync_attempts: int = 0
    rest_resync_books: int = 0
    rest_resync_failures: int = 0
    resyncing_assets: int = 0
    stale_asset_sample: list[str] = field(default_factory=list)
    backpressure_status: str = "DEFER_OR_REJECT"
    backpressure_reasons: list[str] = field(default_factory=lambda: ["starting"])
    watch_refresh_failures: int = 0
    watch_refresh_consecutive_failures: int = 0
    last_watch_refresh_at: str | None = None
    last_watch_refresh_error: str | None = None
    risk_rejections: int = 0
    operations_admission_evaluations: int = 0
    operations_admission_blocked: int = 0
    operations_admission_enforced_rejections: int = 0
    operations_admission_level: str | None = None
    last_operations_admission_reason: str | None = None
    unified_admission_evaluations: int = 0
    unified_admission_blocked: int = 0
    unified_admission_enforced_rejections: int = 0
    unified_admission_failures: int = 0
    unified_admission_mode: str | None = None
    last_unified_admission_reason: str | None = None
    market_terms_resolved: int = 0
    market_terms_failures: int = 0
    market_terms_prefetch_pending: int = 0
    market_terms_prefetch_succeeded: int = 0
    market_terms_prefetch_failures: int = 0
    market_terms_prefetch_dropped: int = 0
    last_market_terms_prefetch_error: str | None = None
    market_clarification_commands: int = 0
    market_clarifications_applied: int = 0
    market_clarification_failures: int = 0
    last_market_clarification_error: str | None = None
    nav_snapshots: int = 0
    nav_failures: int = 0
    nav_strategies: int = 0
    nav_unmarkable_positions: int = 0
    last_nav_at: str | None = None
    tca_records: int = 0
    tca_failures: int = 0
    last_tca_error: str | None = None
    venue_shadow_evaluations: int = 0
    venue_shadow_disagreements: int = 0
    venue_shadow_queued: int = 0
    venue_shadow_failures: int = 0
    venue_enforced_deferrals: int = 0
    venue_enforced_rejections: int = 0
    venue_shadow_heartbeat_auto_cancels: int = 0
    venue_shadow_reservation_releases: int = 0
    venue_shadow_restart_releases: int = 0
    last_venue_shadow_reason: str | None = None
    lifecycle_scheduler_evaluations: int = 0
    lifecycle_scheduler_disagreements: int = 0
    lifecycle_scheduler_failures: int = 0
    last_lifecycle_scheduler_audit_key: str | None = None
    last_lifecycle_scheduler_error: str | None = None
    lifecycle_scheduler_authority_batches: int = 0
    lifecycle_scheduler_authority_intents: int = 0
    lifecycle_scheduler_authority_failures: int = 0
    lifecycle_scheduler_authority_last_intent_ids: list[int] = field(
        default_factory=list
    )
    lifecycle_scheduler_authority_last_journal_hash: str | None = None
    causal_checkpoint_requests: int = 0
    causal_kernel_events: int = 0
    causal_kernel_last_sequence: int = 0
    causal_kernel_journal_hash: str | None = None
    causal_kernel_event_counts: dict[str, int] = field(default_factory=dict)
    causal_kernel_recent_events: list[dict[str, Any]] = field(default_factory=list)
    causal_kernel_recent_lifecycle_events: list[dict[str, Any]] = field(
        default_factory=list
    )
    causal_kernel_failures: int = 0
    persistent_kernel_mode: str = "MEMORY_ONLY"
    persistent_kernel_state: str = "NOT_CONFIGURED"
    persistent_kernel_recovered_events: int = 0
    persistent_kernel_duplicate_events: int = 0
    persistent_kernel_late_events: int = 0
    persistent_kernel_pending_events: int = 0
    persistent_kernel_failed_events: int = 0
    persistent_kernel_feed_batches: int = 0
    persistent_kernel_feed_envelopes: int = 0
    persistent_kernel_feed_max_batch_size: int = 0
    persistent_kernel_feed_db_transactions_avoided: int = 0
    batch_evidence_updates: int = 0
    batch_evidence_failures: int = 0
    last_batch_evidence_error: str | None = None
    own_order_oms_evaluations: int = 0
    own_order_oms_rejections: int = 0
    own_order_oms_deferred: int = 0
    own_order_oms_failures: int = 0
    own_order_oms_finalizations: int = 0
    last_own_order_oms_reason: str | None = None
    fill_finality_shadow_matches: int = 0
    fill_finality_confirmed: int = 0
    fill_finality_lifecycle_reconciled: int = 0
    fill_finality_shadow_failures: int = 0
    last_fill_finality_trade_ids: list[str] = field(default_factory=list)
    last_fill_finality_error: str | None = None
    simulator_artifact_run_id: str | None = None
    simulator_artifact_events: int = 0
    simulator_artifact_events_inserted: int = 0
    simulator_artifact_event_duplicates: int = 0
    simulator_artifact_heartbeats: int = 0
    simulator_artifact_failures: int = 0
    last_simulator_artifact_error: str | None = None
    degradation_transitions: int = 0
    degradation_rejections: int = 0
    degradation_persistence_failures: int = 0
    degradation_states_restored: int = 0
    last_degradation_reason: str | None = None
    execution_profile_decisions: int = 0
    execution_profile_failures: int = 0
    execution_profile_counts: dict[str, int] = field(default_factory=dict)
    last_execution_profile: str | None = None
    last_execution_profile_error: str | None = None
    maker_trade_events: int = 0
    maker_trade_events_persisted: int = 0
    maker_trade_event_duplicates: int = 0
    maker_trade_events_pruned: int = 0
    maker_queue_advances: int = 0
    maker_queue_rebases: int = 0
    maker_trade_unsafe_skips: int = 0
    maker_fills: int = 0
    maker_filled_size: str = "0"
    maker_trade_drops: int = 0
    maker_errors: int = 0
    last_maker_error: str | None = None
    maker_research_book_events: int = 0
    maker_research_state_updates: int = 0
    maker_research_rebases: int = 0
    maker_research_duplicate_events: int = 0
    maker_research_hypothetical_fills: int = 0
    maker_research_event_drops: int = 0
    maker_research_degraded: bool = False
    last_maker_research_error: str | None = None
    maker_model_domain_status: str | None = None
    maker_model_domain_decision_hash: str | None = None
    accounting_reconciliations: int = 0
    accounting_reconciliation_failures: int = 0
    last_accounting_reconciliation_error: str | None = None
    authority_mode: str = "UNFENCED"
    authority_state: str = "NOT_CONFIGURED"
    authority_partition_key: str | None = None
    authority_owner_instance_id: str | None = None
    authority_lease_epoch: int | None = None
    authority_lease_until: str | None = None
    authority_heartbeat_at: str | None = None
    authority_fencing_enforced: bool = False
    intent_processing_last_ms: float = 0.0
    intent_processing_max_ms: float = 0.0
    db_operation_last: str | None = None
    db_operation_last_ms: float = 0.0
    db_operation_max_ms: float = 0.0
    db_operation_slow_count: int = 0
    db_operation_counts: dict[str, int] = field(default_factory=dict)
    db_operation_total_ms: dict[str, float] = field(default_factory=dict)
    db_operation_max_ms_by_name: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LivePaperShadowService:
    def __init__(
        self,
        *,
        store: LiveShadowStore,
        client: PolymarketMarketWsClient | None,
        secondary_client: PolymarketMarketWsClient | None = None,
        external_event_socket: Path | None = None,
        external_subscription_file: Path | None = None,
        external_sources: tuple[str, ...] = ("primary", "secondary"),
        external_feed_stale_seconds: float = 30.0,
        rest_client: PolymarketPaperClobClient | None = None,
        primary_proxy_urls: list[str] | None = None,
        secondary_proxy_urls: list[str] | None = None,
        engine: TakerOnlyPaperExecutionEngine,
        execution_kernel: ProfessionalPaperExecutionKernel | None = None,
        execution_profile_resolver: ExecutionProfileResolver | None = None,
        maker_model_domain_resolver: MakerModelDomainResolver | None = None,
        market_terms_resolver: CachedPolymarketTermsResolver | None = None,
        portfolio_store: PostgresPaperLedgerSink | None = None,
        venue_admission_shadow: VenueAdmissionShadow | None = None,
        venue_admission_enforce: bool = False,
        lifecycle_scheduler_shadow: PaperLifecycleSchedulerShadow | None = None,
        lifecycle_scheduler_enforce_order: bool = False,
        global_liquidity_overlay_verified: bool = False,
        own_order_oms_gate: DurablePaperOmsGate | None = None,
        own_order_oms_enforce: bool = False,
        fill_finality_shadow: DurablePaperFillFinality | None = None,
        fill_finality_auto_reconcile: bool = False,
        fill_finality_reconcile_seconds: float = 2.0,
        simulator_artifact_store: PostgresSimulatorArtifactStore | None = None,
        simulator_run_id: str | None = None,
        degradation_controller: DegradationController | None = None,
        degradation_account_id: str = "paper-account",
        operations_admission_shadow: bool = False,
        operations_admission_enforce: bool = False,
        operations_status_path: Path = Path(
            "runtime_outputs/paper_operations/status.json"
        ),
        operations_status_max_age_seconds: float = 90.0,
        operations_yellow_max_notional: Decimal = Decimal(20),
        unified_admission_service: UnifiedAdmissionService | None = None,
        unified_admission_shadow: bool = False,
        unified_admission_enforce: bool = False,
        max_watch_assets: int = 120,
        seed_watchlist_limit: int = 0,
        seed_reconcile_seconds: float = 30.0,
        watch_refresh_seconds: float = 5.0,
        intent_poll_seconds: float = 0.1,
        health_seconds: float = 2.0,
        settlement_poll_seconds: float = 30.0,
        feed_idle_reconnect_seconds: float = 60.0,
        secondary_start_delay_seconds: float = 0.0,
        rest_resync_retry_seconds: float = 5.0,
        db_operation_timeout_seconds: float = 2.0,
        nav_snapshot_seconds: float = 5.0,
        nav_history_seconds: float = 60.0,
        history_size: int = 32,
        status_path: Path = DEFAULT_STATUS,
        health_spool_path: Path | None = None,
        shutdown_path: Path | None = None,
        worker_id: str | None = None,
        build_id: str | None = None,
        authority_controller: AuthorityLeaseController | None = None,
        persistent_event_kernel: PostgresPersistentEventKernelStore | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.external_event_socket = (
            Path(external_event_socket) if external_event_socket else None
        )
        self.external_subscription_file = (
            Path(external_subscription_file) if external_subscription_file else None
        )
        if client is None and self.external_event_socket is None:
            raise ValueError("a websocket client or external event socket is required")
        if client is not None and self.external_event_socket is not None:
            raise ValueError(
                "websocket and external event modes are mutually exclusive"
            )
        self.clients: dict[str, PolymarketMarketWsClient] = {}
        if client is not None:
            self.clients["primary"] = client
        if secondary_client is not None:
            self.clients["secondary"] = secondary_client
        route_sources = (
            tuple(dict.fromkeys(external_sources))
            if self.external_event_socket is not None
            else tuple(self.clients)
        )
        if not route_sources:
            raise ValueError("at least one market-data source is required")
        primary_proxy = (
            getattr(client, "proxy_url", None) if client is not None else None
        )
        self.proxy_pools: dict[str, list[str | None]] = {}
        if client is not None:
            self.proxy_pools["primary"] = list(
                dict.fromkeys(
                    primary_proxy_urls or ([primary_proxy] if primary_proxy else [None])
                )
            )
        if secondary_client is not None:
            secondary_proxy = getattr(secondary_client, "proxy_url", None)
            self.proxy_pools["secondary"] = list(
                dict.fromkeys(
                    secondary_proxy_urls
                    or ([secondary_proxy] if secondary_proxy else [None])
                )
            )
        self.engine = engine
        self.execution_kernel = execution_kernel or ProfessionalPaperExecutionKernel(
            engine
        )
        self.execution_profile_resolver = (
            execution_profile_resolver or ExecutionProfileResolver()
        )
        self.maker_model_domain_resolver = (
            maker_model_domain_resolver or MakerModelDomainResolver()
        )
        self.market_terms_resolver = market_terms_resolver
        self.portfolio_store = portfolio_store
        self.venue_admission_shadow = venue_admission_shadow
        self.venue_admission_enforce = bool(venue_admission_enforce)
        self.lifecycle_scheduler_shadow = lifecycle_scheduler_shadow
        self.lifecycle_scheduler_enforce_order = bool(lifecycle_scheduler_enforce_order)
        self.global_liquidity_overlay_verified = bool(global_liquidity_overlay_verified)
        self.own_order_oms_gate = own_order_oms_gate
        self.own_order_oms_enforce = bool(own_order_oms_enforce)
        self.fill_finality_shadow = fill_finality_shadow
        self.fill_finality_auto_reconcile = bool(fill_finality_auto_reconcile)
        self.fill_finality_reconcile_seconds = max(
            0.5, float(fill_finality_reconcile_seconds)
        )
        if (simulator_artifact_store is None) != (simulator_run_id is None):
            raise ValueError(
                "simulator_artifact_store and simulator_run_id must be configured together"
            )
        self.simulator_artifact_store = simulator_artifact_store
        self.simulator_run_id = (
            str(simulator_run_id) if simulator_run_id is not None else None
        )
        self.simulator_artifact_worker = (
            SchedulerOwnedShadowWorker(simulator_artifact_store)
            if simulator_artifact_store is not None
            else None
        )
        self.degradation_controller = degradation_controller or DegradationController()
        self.degradation_account_id = str(degradation_account_id)
        self.operations_admission_shadow = bool(operations_admission_shadow)
        self.operations_admission_enforce = bool(operations_admission_enforce)
        self.operations_status_path = Path(operations_status_path)
        self.operations_status_max_age_seconds = max(
            1.0, float(operations_status_max_age_seconds)
        )
        self.operations_yellow_max_notional = max(
            Decimal(0), Decimal(str(operations_yellow_max_notional))
        )
        self.unified_admission_service = unified_admission_service
        self.unified_admission_shadow = bool(unified_admission_shadow)
        self.unified_admission_enforce = bool(unified_admission_enforce)
        self.paper_intent_scheduler = DeterministicPaperIntentScheduler(
            event_namespace=self.simulator_run_id or "paper-worker"
        )
        self.rest_client = rest_client
        self.max_watch_assets = max(1, int(max_watch_assets))
        self.seed_watchlist_limit = max(0, int(seed_watchlist_limit))
        self.seed_reconcile_seconds = max(5.0, float(seed_reconcile_seconds))
        self.watch_refresh_seconds = max(1.0, float(watch_refresh_seconds))
        self.intent_poll_seconds = max(0.05, float(intent_poll_seconds))
        self.health_seconds = max(0.5, float(health_seconds))
        self.settlement_poll_seconds = max(1.0, float(settlement_poll_seconds))
        self.feed_idle_reconnect_seconds = max(5.0, float(feed_idle_reconnect_seconds))
        self.external_feed_stale_seconds = max(5.0, float(external_feed_stale_seconds))
        self.secondary_start_delay_seconds = max(
            0.0, float(secondary_start_delay_seconds)
        )
        self.rest_resync_retry_seconds = max(1.0, float(rest_resync_retry_seconds))
        self.db_operation_timeout_seconds = max(
            0.5, float(db_operation_timeout_seconds)
        )
        self.nav_snapshot_seconds = max(1.0, float(nav_snapshot_seconds))
        self.nav_history_seconds = max(
            self.nav_snapshot_seconds,
            float(nav_history_seconds),
        )
        self.history_size = max(4, int(history_size))
        self.status_path = status_path
        self.health_spool = HealthSpool(
            health_spool_path or status_path.with_name("health-spool.jsonl")
        )
        self.shutdown_path = Path(shutdown_path) if shutdown_path else None
        self.worker_id = worker_id or f"paper-live-{uuid4().hex[:12]}"
        self.build_id = str(build_id).strip() if build_id else None
        self.authority_controller = authority_controller
        self.persistent_event_kernel = persistent_event_kernel
        self.connection_id = f"paper-ws-{uuid4().hex}"
        self._kernel_session_id = f"{self.worker_id}:{uuid4().hex}"
        self._persistent_kernel_blocked = False
        self.message_seq = 0
        self.targets: dict[str, LiveWatchTarget] = {}
        self.service = RealtimeOrderBookService(
            [], depth_levels=100_000, sample_interval_ms=1
        )
        self.route_services = {
            source: RealtimeOrderBookService(
                [], depth_levels=100_000, sample_interval_ms=1
            )
            for source in route_sources
        }
        self.history: dict[str, deque[ArrivalBookCheckpoint]] = defaultdict(
            lambda: deque(maxlen=self.history_size)
        )
        self.pending: dict[int, PendingIntent] = {}
        self.stats = LiveShadowStats(
            worker_id=self.worker_id,
            build_id=self.build_id,
        )
        if self.persistent_event_kernel is not None:
            self.stats.persistent_kernel_mode = "POSTGRES_FENCED"
            self.stats.persistent_kernel_state = "STARTING"
        self.stats.simulator_artifact_run_id = self.simulator_run_id
        self.stats.route_states = {source: "STARTING" for source in route_sources}
        self.stats.route_proxy_urls = {
            source: (
                f"collector://gcp-{source}"
                if self.external_event_socket is not None
                else getattr(self.clients[source], "proxy_url", None)
            )
            for source in route_sources
        }
        self.stats.route_messages = {source: 0 for source in route_sources}
        self.stats.route_last_message_at = {source: None for source in route_sources}
        self.stats.route_last_transport_at = {
            source: None for source in route_sources
        }
        self.stats.route_reconnects = {source: 0 for source in route_sources}
        self.feed_mismatch_assets: set[str] = set()
        self.redundant_ready_assets: set[str] = set()
        self.route_gap_assets: dict[str, set[str]] = {
            source: set() for source in route_sources
        }
        self._external_subscription_revision: tuple[
            tuple[str, str | None], ...
        ] = ()
        self._persisted_current_state: dict[str, tuple[Any, ...]] = {}
        self.resyncing_assets: set[str] = set()
        self._refresh_routes_pending: dict[str, set[str]] = {}
        self._feed_queue: asyncio.Queue[
            FeedEnvelope | CausalCheckpointRequest | CausalLifecycleEvent
        ] = asyncio.Queue(maxsize=4096)
        self._feed_tasks: dict[str, asyncio.Task[None]] = {}
        self._causal_kernel_sequence = 0
        self._causal_kernel_hash = ""
        self._causal_kernel_recent: deque[dict[str, Any]] = deque(maxlen=64)
        self._causal_kernel_recent_lifecycle: deque[dict[str, Any]] = deque(maxlen=64)
        self._seen_events: OrderedDict[str, None] = OrderedDict()
        self._seen_event_limit = 100_000
        self._maker_trades: deque[NormalizedTradeEvent] = deque()
        self._maker_trade_limit = 20_000
        self._maker_research_book_events: deque[MakerResearchBookEvent] = deque()
        self._maker_research_book_event_limit = 50_000
        self._maker_research_levels: set[tuple[str, str, Decimal]] = set()
        self._last_maker_trade_prune = 0.0
        self._last_accounting_reconcile = 0.0
        self._last_watch_refresh = 0.0
        self._watch_refresh_task: asyncio.Task[list[LiveWatchTarget]] | None = None
        self._market_terms_prefetch_queue: asyncio.Queue[tuple[str, str]] = (
            asyncio.Queue(maxsize=max(64, self.max_watch_assets * 2))
        )
        self._market_terms_prefetch_pending: set[str] = set()
        self._market_terms_prefetch_tasks: list[asyncio.Task[None]] = []
        self._last_seed_reconcile = 0.0
        self._seed_reconcile_task: asyncio.Task[int] | None = None
        self._last_intent_poll = 0.0
        self._intent_claim_task: asyncio.Task[list[QueuedIntent]] | None = None
        self._claimed_intents: deque[QueuedIntent] = deque()
        self._last_order_expiry = 0.0
        self._last_market_clarification_poll = 0.0
        self._last_health = 0.0
        self._last_settlement = 0.0
        self._settlement_task: asyncio.Task[int] | None = None
        self._last_nav_snapshot = 0.0
        self._nav_snapshot_task: asyncio.Task[list[dict[str, Any]]] | None = None
        self._rest_resync_requested = False
        self._rest_resync_task: asyncio.Task[dict[str, dict[str, Any]]] | None = None
        self._last_rest_resync = 0.0
        self._current_book_write_task: asyncio.Task[None] | None = None
        self._current_book_write_states: dict[str, tuple[Any, ...]] = {}
        self._health_counts_task: asyncio.Task[dict[str, int]] | None = None
        self._health_write_task: asyncio.Task[None] | None = None
        self._health_write_batch: HealthSpoolBatch | None = None
        self._health_loop_task: asyncio.Task[None] | None = None
        self._intent_claim_loop_task: asyncio.Task[None] | None = None
        self._artifact_heartbeat_loop_task: asyncio.Task[None] | None = None
        self._authority_heartbeat_loop_task: asyncio.Task[None] | None = None
        self._external_last_seen: dict[str, float] = {}
        self._external_connected_enqueued: set[str] = set()
        self._event_server: UnixEventServer | None = None
        self._last_simulator_artifact_heartbeat = 0.0
        self._last_fill_finality_reconcile = 0.0
        # One intent is processed serially. Pin its DB work to one worker thread
        # so ThreadLocalPostgresConnectionFactory can actually reuse the same
        # connection and the fencing session marker across every lifecycle step.
        self._intent_db_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="paper-intent-db",
        )
        # Ingress acknowledgement must not wait for the previous intent's
        # matching, accounting, or audit writes. Pin claims to a separate
        # thread so its thread-local PostgreSQL connection is also reused.
        self._claim_db_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="paper-claim-db",
        )
        configure_db_call = getattr(self.market_terms_resolver, "set_db_call", None)
        if configure_db_call is not None:
            configure_db_call(self._db_call)

    async def run(self, *, max_seconds: float = 0.0) -> LiveShadowStats:
        started = time.monotonic()
        if self.shutdown_path is not None:
            self.shutdown_path.unlink(missing_ok=True)
        shutdown_requested = asyncio.Event()
        loop = asyncio.get_running_loop()
        previous_signal_handlers: dict[signal.Signals, Any] = {}

        def request_shutdown(_signum: int, _frame: Any) -> None:
            shutdown_requested.set()
            loop.call_soon_threadsafe(lambda: None)

        for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_signal_handlers[shutdown_signal] = signal.getsignal(
                    shutdown_signal
                )
                signal.signal(shutdown_signal, request_shutdown)
            except (OSError, RuntimeError, ValueError):
                continue
        if self.authority_controller is not None:
            self.stats.authority_mode = "FENCED"
            self.stats.authority_state = "ACQUIRING"
            try:
                await self._db_call(self.authority_controller.acquire)
            except Exception as exc:
                self.stats.authority_state = "BLOCKED"
                self.stats.last_error = f"authority_acquire: {_exception_text(exc)}"
                self._sync_authority_stats()
                self.stats.updated_at = _now().isoformat()
                _write_json(self.status_path, self.stats.as_dict())
                raise
            self._sync_authority_stats()
            self._authority_heartbeat_loop_task = asyncio.create_task(
                self._authority_heartbeat_loop(),
                name="paper-live-authority-heartbeat",
            )
        await self._persist_simulator_artifact_heartbeat(status="RUNNING", force=True)
        await self._prewarm_intent_db()
        await self._restore_degradation_states()
        await self._db_call(self.store.recover_abandoned)
        if self.own_order_oms_gate is not None:
            await self._db_call(
                self.own_order_oms_gate.store.reconcile_terminal_intents
            )
        await self._reconcile_unapplied_accounting(force=True)
        await self._advance_fill_finality(force=True)
        recover_maker_queues = getattr(self.store, "recover_maker_queues", None)
        if recover_maker_queues is not None:
            await self._db_call(recover_maker_queues)
        recover_research = getattr(self.store, "recover_maker_research_queues", None)
        if recover_research is not None:
            await self._db_call(recover_research)
        load_research_levels = getattr(
            self.store, "load_open_maker_research_levels", None
        )
        if load_research_levels is not None:
            self._maker_research_levels = await self._db_call(load_research_levels)
        if self.portfolio_store is not None and hasattr(
            self.portfolio_store, "reconcile_terminal_reservations"
        ):
            await self._db_call(self.portfolio_store.reconcile_terminal_reservations)
        await self._reconcile_venue_shadow_restart()
        if getattr(self.market_terms_resolver, "resolve_asset", None) is not None:
            self._market_terms_prefetch_tasks = [
                asyncio.create_task(
                    self._market_terms_prefetch_loop(),
                    name=f"paper-live-market-terms-prefetch-{index}",
                )
                for index in range(2)
            ]
        await self._refresh_watchlist(force=True)
        await self._restore_persistent_event_kernel()
        if self.external_event_socket is not None:
            self._event_server = UnixEventServer(
                self.external_event_socket,
                self._handle_external_envelope,
            )
            await self._event_server.start()
            self._feed_tasks = {
                "external-server": asyncio.create_task(
                    self._event_server.serve_forever(),
                    name="paper-live-external-event-server",
                ),
                "external-monitor": asyncio.create_task(
                    self._external_feed_monitor(),
                    name="paper-live-external-feed-monitor",
                ),
                "consumer": asyncio.create_task(
                    self._feed_consumer_loop(),
                    name="paper-live-feed-consumer",
                ),
            }
        else:
            self._feed_tasks = {
                source: asyncio.create_task(
                    self._feed_loop(
                        source,
                        client,
                        initial_delay_seconds=(
                            self.secondary_start_delay_seconds
                            if source == "secondary"
                            else 0.0
                        ),
                    ),
                    name=f"paper-live-{source}",
                )
                for source, client in self.clients.items()
            }
            self._feed_tasks["consumer"] = asyncio.create_task(
                self._feed_consumer_loop(),
                name="paper-live-feed-consumer",
            )
        await self._write_health(force=True)
        self._health_loop_task = asyncio.create_task(
            self._health_loop(),
            name="paper-live-health-loop",
        )
        self._artifact_heartbeat_loop_task = asyncio.create_task(
            self._artifact_heartbeat_loop(),
            name="paper-live-artifact-heartbeat-loop",
        )
        self._intent_claim_loop_task = asyncio.create_task(
            self._intent_claim_loop(),
            name="paper-live-intent-claim-loop",
        )
        try:
            while (
                not shutdown_requested.is_set()
                and not (self.shutdown_path is not None and self.shutdown_path.exists())
                and (not max_seconds or time.monotonic() - started < max_seconds)
                and (
                    self.authority_controller is None or self.authority_controller.held
                )
            ):
                try:
                    await asyncio.sleep(self.intent_poll_seconds)
                    await self._advance_venue_shadow()
                    self._advance_rest_resync()
                    await self._expire_working_orders()
                    await self._poll_intents()
                    await self._reconcile_unapplied_accounting(force=False)
                    await self._advance_maker_research_book_events()
                    await self._advance_maker_trades()
                    await self._advance_replacements()
                    await self._advance_cancellations()
                    await self._advance_market_clarifications()
                    await self._execute_due()
                    await self._advance_fill_finality(force=False)
                    await self._settle_resolved_positions(force=False)
                    self._advance_nav_snapshots()
                    await self._refresh_watchlist(force=False)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.stats.last_error = (
                        f"{exc.__class__.__name__}: {str(exc)[:500]}"
                    )
                    await self._write_health(force=True)
        finally:
            _log_shutdown_phase("begin")
            service_tasks = [
                task
                for task in (
                    self._health_loop_task,
                    self._intent_claim_loop_task,
                    self._artifact_heartbeat_loop_task,
                )
                if task is not None
            ]
            service_tasks.extend(self._market_terms_prefetch_tasks)
            self._market_terms_prefetch_tasks = []
            for task in service_tasks:
                task.cancel()
            if service_tasks:
                await asyncio.gather(*service_tasks, return_exceptions=True)
            close_market_terms = getattr(self.market_terms_resolver, "close", None)
            if close_market_terms is not None:
                await close_market_terms()
            if self.venue_admission_shadow is not None:
                try:
                    await self._db_call(
                        self.venue_admission_shadow.mark_stopped,
                        now_ts_ns=_datetime_to_ns(_now()),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.stats.venue_shadow_failures += 1
                    self.stats.last_venue_shadow_reason = _exception_text(exc)
            for shutdown_signal, previous_handler in previous_signal_handlers.items():
                signal.signal(shutdown_signal, previous_handler)
            if self.shutdown_path is not None:
                self.shutdown_path.unlink(missing_ok=True)
            self.stats.transport_state = "STOPPED"
            try:
                await self._persist_simulator_artifact_heartbeat(
                    status="STOPPED", force=True
                )
            except Exception as exc:  # noqa: BLE001
                self.stats.simulator_artifact_failures += 1
                self.stats.last_simulator_artifact_error = _exception_text(exc)
            if self._event_server is not None:
                try:
                    await asyncio.wait_for(
                        self._event_server.close(),
                        timeout=3.0,
                    )
                except TimeoutError:
                    pass
            _log_shutdown_phase("event_server_closed")
            for task in self._feed_tasks.values():
                task.cancel()
            if self._feed_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *self._feed_tasks.values(),
                            return_exceptions=True,
                        ),
                        timeout=3.0,
                    )
                except TimeoutError:
                    pass
            _log_shutdown_phase("feed_tasks_closed")
            self.stats.updated_at = _now().isoformat()
            stopped_payload = self.stats.as_dict()
            stopped_payload["sampled_at"] = stopped_payload["updated_at"]
            _write_json(self.status_path, stopped_payload)
            self.health_spool.append(stopped_payload)
            if self.clients:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(
                                _close_ws_client(client, abort=False)
                                for client in self.clients.values()
                            ),
                            return_exceptions=True,
                        ),
                        timeout=3.0,
                    )
                except TimeoutError:
                    pass
            _log_shutdown_phase("ws_clients_closed")
            background_tasks = [
                task
                for task in (
                    self._rest_resync_task,
                    self._seed_reconcile_task,
                    self._watch_refresh_task,
                    self._current_book_write_task,
                    self._health_counts_task,
                    self._health_write_task,
                    self._intent_claim_task,
                    self._settlement_task,
                    self._nav_snapshot_task,
                )
                if task is not None
            ]
            for task in background_tasks:
                task.cancel()
            if background_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *background_tasks,
                            return_exceptions=True,
                        ),
                        timeout=5.0,
                    )
                except TimeoutError:
                    pass
            _log_shutdown_phase("background_tasks_closed")
            await self._shutdown_authority()
            self._intent_db_executor.shutdown(wait=False, cancel_futures=True)
            self._claim_db_executor.shutdown(wait=False, cancel_futures=True)
            self.stats.updated_at = _now().isoformat()
            stopped_payload = self.stats.as_dict()
            stopped_payload["sampled_at"] = stopped_payload["updated_at"]
            _write_json(self.status_path, stopped_payload)
        return self.stats

    async def _handle_external_envelope(
        self,
        envelope: LocalEventEnvelope,
    ) -> None:
        if envelope.source not in self.route_services:
            return
        self._external_last_seen[envelope.source] = time.monotonic()
        self.stats.route_last_transport_at[envelope.source] = _now().isoformat()
        state = (
            "CONNECTED"
            if (
                self.stats.route_states.get(envelope.source) != "CONNECTED"
                and envelope.source not in self._external_connected_enqueued
            )
            else None
        )
        if state == "CONNECTED":
            self._external_connected_enqueued.add(envelope.source)
        await self._feed_queue.put(
            FeedEnvelope(
                source=envelope.source,
                state=state,
                messages=envelope.messages,
            )
        )

    async def _feed_consumer_loop(self) -> None:
        deferred: deque[
            FeedEnvelope | CausalCheckpointRequest | CausalLifecycleEvent
        ] = deque()
        while True:
            item = deferred.popleft() if deferred else await self._feed_queue.get()
            consumed = 1
            try:
                if self._persistent_kernel_blocked:
                    await self._restore_persistent_event_kernel()
                if _is_plain_feed_envelope(item):
                    envelopes = [item]
                    while len(envelopes) < 64:
                        try:
                            next_item = self._feed_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if not _is_plain_feed_envelope(next_item):
                            deferred.append(next_item)
                            break
                        envelopes.append(next_item)
                        consumed += 1
                    await self._apply_plain_feed_batch(tuple(envelopes))
                elif isinstance(item, CausalCheckpointRequest):
                    checkpoint = _checkpoint_at(
                        self.history.get(item.asset_id),
                        item.as_of,
                    )
                    if not item.result.done():
                        item.result.set_result(checkpoint)
                    self.stats.causal_checkpoint_requests += 1
                    for event_type in item.event_types:
                        await self._persist_causal_kernel_event(
                            event_type,
                            intent_id=item.intent_id,
                            asset_id=item.asset_id,
                            event_ts=item.as_of,
                            payload={
                                "checkpoint_id": getattr(
                                    checkpoint, "checkpoint_id", None
                                )
                            },
                        )
                elif isinstance(item, CausalLifecycleEvent):
                    sequence = await self._persist_causal_kernel_event(
                        item.event_type,
                        intent_id=item.intent_id,
                        asset_id=item.asset_id,
                        event_ts=item.event_ts,
                        payload=item.payload,
                    )
                    if not item.result.done():
                        item.result.set_result(sequence)
                else:
                    state_changed = bool(
                        item.state
                        and self.stats.route_states.get(item.source) != item.state
                    )
                    if item.error or state_changed:
                        await self._persist_causal_kernel_event(
                            "VENUE_STATE_CHANGED",
                            event_ts=item.received_at,
                            payload={
                                "source": item.source,
                                "state": item.state,
                                "error": item.error,
                            },
                        )
                    await self._apply_feed_envelope_cooperatively(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                source = item.source if isinstance(item, FeedEnvelope) else "kernel"
                self.stats.last_error = f"feed_consumer:{source}:{_exception_text(exc)}"
                self.stats.causal_kernel_failures += 1
                if (
                    isinstance(item, (CausalCheckpointRequest, CausalLifecycleEvent))
                    and not item.result.done()
                ):
                    item.result.set_exception(exc)
            finally:
                for _ in range(consumed):
                    self._feed_queue.task_done()

    async def _causal_checkpoint(
        self,
        *,
        intent_id: int,
        asset_id: str,
        as_of: datetime,
        event_types: tuple[str, ...] = (),
    ) -> ArrivalBookCheckpoint | None:
        consumer = self._feed_tasks.get("consumer")
        if consumer is None or consumer.done():
            checkpoint = _checkpoint_at(self.history.get(asset_id), as_of)
            for event_type in event_types:
                self._record_causal_kernel_event(
                    event_type,
                    intent_id=intent_id,
                    asset_id=asset_id,
                    event_ts=as_of,
                    payload={
                        "checkpoint_id": getattr(checkpoint, "checkpoint_id", None)
                    },
                )
            return checkpoint
        future = asyncio.get_running_loop().create_future()
        await self._feed_queue.put(
            CausalCheckpointRequest(
                intent_id=int(intent_id),
                asset_id=str(asset_id),
                as_of=as_of,
                result=future,
                event_types=tuple(event_types),
            )
        )
        return await future

    async def _causal_lifecycle_event(
        self,
        event_type: str,
        *,
        intent_id: int = 0,
        asset_id: str | None = None,
        event_ts: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        observed_at = event_ts or _now()
        consumer = getattr(self, "_feed_tasks", {}).get("consumer")
        if consumer is None or consumer.done():
            return self._record_causal_kernel_event(
                event_type,
                intent_id=intent_id,
                asset_id=asset_id,
                event_ts=observed_at,
                payload=payload,
            )
        future = asyncio.get_running_loop().create_future()
        await self._feed_queue.put(
            CausalLifecycleEvent(
                intent_id=int(intent_id),
                event_type=str(event_type),
                event_ts=observed_at,
                asset_id=str(asset_id) if asset_id is not None else None,
                payload=dict(payload or {}),
                result=future,
            )
        )
        return await future

    def _record_causal_kernel_event(
        self,
        event_type: str,
        *,
        intent_id: int = 0,
        asset_id: str | None = None,
        event_ts: datetime | None = None,
        payload: dict[str, Any] | None = None,
        sequence_override: int | None = None,
        journal_hash_override: str | None = None,
    ) -> int:
        """Record one boundary already owned by the single feed/command actor."""

        expected_sequence = int(getattr(self, "_causal_kernel_sequence", 0)) + 1
        sequence = int(sequence_override or expected_sequence)
        if sequence != expected_sequence:
            raise RuntimeError(
                "persistent causal sequence diverged: "
                f"expected={expected_sequence},actual={sequence}"
            )
        self._causal_kernel_sequence = sequence
        normalized_type = str(event_type).strip().upper() or "UNKNOWN"
        normalized_ts = event_ts if isinstance(event_ts, datetime) else _now()
        event = {
            "sequence": sequence,
            "event_type": normalized_type,
            "intent_id": int(intent_id),
            "asset_id": str(asset_id) if asset_id is not None else None,
            "event_ts": normalized_ts.isoformat(),
            "payload": dict(payload or {}),
        }
        encoded = json.dumps(
            event,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        previous_hash = str(getattr(self, "_causal_kernel_hash", ""))
        calculated_hash = hashlib.sha256(
            f"{previous_hash}|{encoded}".encode()
        ).hexdigest()
        if journal_hash_override is not None and calculated_hash != str(
            journal_hash_override
        ):
            raise RuntimeError(
                "persistent causal journal hash diverged: "
                f"expected={journal_hash_override},actual={calculated_hash}"
            )
        self._causal_kernel_hash = calculated_hash
        recent = getattr(self, "_causal_kernel_recent", None)
        if recent is None:
            recent = deque(maxlen=64)
            self._causal_kernel_recent = recent
        recent.append(event)
        lifecycle_recent = getattr(self, "_causal_kernel_recent_lifecycle", None)
        if lifecycle_recent is None:
            lifecycle_recent = deque(maxlen=64)
            self._causal_kernel_recent_lifecycle = lifecycle_recent
        if normalized_type not in {
            "BOOK_SNAPSHOT",
            "BOOK_DELTA",
            "TICK_SIZE_CHANGE",
            "EXTERNAL_TRADE",
            "MARKET_DATA",
            "MARKET_DATA_BATCH",
        }:
            lifecycle_recent.append(event)
        if not hasattr(self, "stats"):
            self.stats = LiveShadowStats(worker_id="causal-kernel")
        self.stats.causal_kernel_events += 1
        self.stats.causal_kernel_last_sequence = sequence
        self.stats.causal_kernel_journal_hash = self._causal_kernel_hash
        counts = self.stats.causal_kernel_event_counts
        counts[normalized_type] = counts.get(normalized_type, 0) + 1
        self.stats.causal_kernel_recent_events = list(recent)
        self.stats.causal_kernel_recent_lifecycle_events = list(lifecycle_recent)
        return sequence

    async def _persist_causal_kernel_event(
        self,
        event_type: str,
        *,
        intent_id: int = 0,
        asset_id: str | None = None,
        event_ts: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        observed_at = event_ts or _now()
        if self.persistent_event_kernel is None:
            return self._record_causal_kernel_event(
                event_type,
                intent_id=intent_id,
                asset_id=asset_id,
                event_ts=observed_at,
                payload=payload,
            )
        event = DurableCausalEvent.build(
            event_type=event_type,
            event_ts=observed_at,
            receive_ts=_now(),
            source_sequence=self._causal_kernel_sequence + 1,
            aggregate_key=(
                f"intent:{intent_id}"
                if intent_id
                else f"asset:{asset_id}"
                if asset_id
                else "paper-worker"
            ),
            intent_id=intent_id,
            asset_id=asset_id,
            record_payload=payload,
            replay_payload={"kind": "lifecycle"},
            event_namespace=self._kernel_session_id,
        )
        append = await self._db_call(
            self.persistent_event_kernel.append,
            event,
            worker_id=self.worker_id,
        )
        if not append.should_apply:
            self.stats.persistent_kernel_duplicate_events += 1
            return int(append.applied_sequence or self._causal_kernel_sequence)
        applied = await self._ack_persistent_events((event,))
        return int(applied[-1].sequence)

    async def _append_persistent_feed_envelope(
        self,
        envelope: FeedEnvelope,
        *,
        observed_at: datetime,
    ) -> DurableCausalEvent | bool | None:
        if getattr(self, "persistent_event_kernel", None) is None:
            return None
        event = self._build_persistent_feed_event(
            envelope,
            observed_at=observed_at,
        )
        try:
            append = await self._db_call(
                self.persistent_event_kernel.append,
                event,
                worker_id=self.worker_id,
            )
        except Exception:
            self._block_persistent_kernel()
            raise
        if not append.should_apply:
            self.stats.persistent_kernel_duplicate_events += 1
            return False
        return event

    def _build_persistent_feed_event(
        self,
        envelope: FeedEnvelope,
        *,
        observed_at: datetime,
    ) -> DurableCausalEvent:
        asset_ids = sorted(
            {
                asset_id
                for message in envelope.messages
                for asset_id in _market_causal_asset_ids(message)
            }
        )
        event_types = [
            _market_causal_event_type(message) for message in envelope.messages
        ]
        return DurableCausalEvent.build(
            event_type="MARKET_DATA_BATCH",
            event_ts=observed_at,
            receive_ts=envelope.received_at,
            source_sequence=self.message_seq,
            aggregate_key=f"market-feed:{envelope.source}",
            asset_id=asset_ids[0] if asset_ids else None,
            record_payload={
                "source": envelope.source,
                "asset_ids": asset_ids,
                "event_types": event_types,
                "message_count": len(envelope.messages),
            },
            replay_payload={
                "kind": "feed_envelope",
                "source": envelope.source,
                "received_at": observed_at.isoformat(),
                "messages": list(envelope.messages),
            },
            event_namespace=self._kernel_session_id,
        )

    async def _ack_persistent_events(
        self,
        events: tuple[DurableCausalEvent, ...],
    ) -> tuple[AppliedKernelEvent, ...]:
        if self.persistent_event_kernel is None:
            return ()
        try:
            applied = await self._db_call(
                self.persistent_event_kernel.mark_applied,
                events,
                worker_id=self.worker_id,
            )
            for row in applied:
                self._record_causal_kernel_event(
                    row.event.event_type,
                    intent_id=row.event.intent_id,
                    asset_id=row.event.asset_id,
                    event_ts=row.event.event_ts,
                    payload=row.event.record_payload,
                    sequence_override=row.sequence,
                    journal_hash_override=row.journal_hash,
                )
                self.stats.persistent_kernel_late_events += int(row.late_event)
            self._persistent_kernel_blocked = False
            self.stats.persistent_kernel_state = "READY"
            return applied
        except Exception as exc:
            self._block_persistent_kernel()
            for event in events:
                try:
                    await self._db_call(
                        self.persistent_event_kernel.mark_failed,
                        event.event_id,
                        worker_id=self.worker_id,
                        error=_exception_text(exc),
                    )
                except Exception as mark_exc:  # noqa: BLE001
                    self.stats.last_error = (
                        f"persistent_kernel_failure_record: {_exception_text(mark_exc)}"
                    )
            raise

    async def _restore_persistent_event_kernel(self) -> None:
        if self.persistent_event_kernel is None:
            return
        recovery_batch_size = 256
        self.stats.persistent_kernel_state = "RECOVERING"
        try:
            state = await self._db_call(self.persistent_event_kernel.load_state)
            self._causal_kernel_sequence = int(state.last_applied_sequence)
            self._causal_kernel_hash = str(state.last_journal_hash)
            self.stats.causal_kernel_last_sequence = int(state.last_applied_sequence)
            self.stats.causal_kernel_journal_hash = str(state.last_journal_hash)
            self.stats.persistent_kernel_pending_events = int(state.pending_events)
            self.stats.persistent_kernel_failed_events = int(state.failed_events)
            while True:
                pending = await self._db_call(
                    self.persistent_event_kernel.recover_pending,
                    worker_id=self.worker_id,
                    limit=recovery_batch_size,
                )
                if not pending:
                    break
                for event in pending:
                    replay = event.replay_payload
                    if replay.get("kind") == "feed_envelope":
                        source = str(replay.get("source") or "")
                        messages = replay.get("messages")
                        if source not in self.route_services or not isinstance(
                            messages, list
                        ):
                            raise RuntimeError(
                                f"cannot replay causal feed event {event.event_id}"
                            )
                        observed_at = _parse_datetime(
                            str(replay.get("received_at") or event.event_ts.isoformat())
                        )
                        self.message_seq += 1
                        for message in messages:
                            if not isinstance(message, dict):
                                raise TypeError(
                                    f"invalid replay message in {event.event_id}"
                                )
                            self._apply_message(
                                source,
                                message,
                                observed_at=observed_at,
                            )
                    await self._ack_persistent_events((event,))
                    self.stats.persistent_kernel_recovered_events += 1
                if len(pending) < recovery_batch_size:
                    break
            final_state = await self._db_call(self.persistent_event_kernel.load_state)
            self.stats.persistent_kernel_pending_events = int(
                final_state.pending_events
            )
            self.stats.persistent_kernel_failed_events = int(final_state.failed_events)
            self._persistent_kernel_blocked = bool(
                final_state.pending_events or final_state.failed_events
            )
            self.stats.persistent_kernel_state = (
                "BLOCKED" if self._persistent_kernel_blocked else "READY"
            )
        except Exception:
            self._block_persistent_kernel()
            # Recovery runs before run() enters its normal shutdown guard.
            # Release fencing now so a supervised retry need not wait for TTL.
            await self._shutdown_authority()
            raise

    def _block_persistent_kernel(self) -> None:
        self._persistent_kernel_blocked = True
        self.stats.persistent_kernel_state = "BLOCKED"
        self.resyncing_assets.update(self.targets)
        self._rest_resync_requested = bool(self.resyncing_assets)

    async def _external_feed_monitor(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for source in self.route_services:
                last_seen = self._external_last_seen.get(source)
                if (
                    last_seen is None
                    or now - last_seen <= self.external_feed_stale_seconds
                    or self.stats.route_states.get(source) == "RECONNECTING"
                ):
                    continue
                self._external_connected_enqueued.discard(source)
                await self._feed_queue.put(
                    FeedEnvelope(
                        source=source,
                        state="RECONNECTING",
                        error="external_collector_event_stream_stale",
                    )
                )

    async def _connect_and_subscribe(
        self, source: str, client: PolymarketMarketWsClient
    ) -> None:
        await client.connect()
        asset_ids = self.service.subscribed_token_ids
        chunks = _chunks(asset_ids, 250)
        for index, chunk in enumerate(chunks):
            await client.subscribe(chunk, initial=index == 0)
            if index + 1 < len(chunks):
                await asyncio.sleep(0.2)
        self.stats.route_states[source] = "CONNECTED"
        self.stats.route_proxy_urls[source] = getattr(client, "proxy_url", None)
        self._update_transport_state()

    async def _feed_loop(
        self,
        source: str,
        client: PolymarketMarketWsClient,
        *,
        initial_delay_seconds: float = 0.0,
    ) -> None:
        reconnects = 0
        if initial_delay_seconds > 0:
            await asyncio.sleep(initial_delay_seconds)
        while True:
            try:
                await self._connect_and_subscribe(source, client)
                await self._feed_queue.put(
                    FeedEnvelope(source=source, state="CONNECTED")
                )
                while True:
                    messages = await asyncio.wait_for(
                        client.recv(),
                        timeout=self.feed_idle_reconnect_seconds,
                    )
                    if messages:
                        await self._feed_queue.put(
                            FeedEnvelope(source=source, messages=tuple(messages))
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                reconnects += 1
                self.stats.reconnects += 1
                self.stats.route_reconnects[source] = reconnects
                self.stats.route_states[source] = "RECONNECTING"
                self.route_services[source].mark_reconnect_stale(
                    f"{source}_ws_reconnect"
                )
                self._update_transport_state()
                await self._feed_queue.put(
                    FeedEnvelope(
                        source=source,
                        state="RECONNECTING",
                        error=_exception_text(exc),
                    )
                )
                await client.close()
                _rotate_client_proxy(client, self.proxy_pools[source], reconnects)
                self.stats.route_proxy_urls[source] = getattr(client, "proxy_url", None)
                await asyncio.sleep(
                    min(30.0, max(0.2, float(client.reconnect_seconds)))
                )

    async def _refresh_watchlist(self, *, force: bool) -> None:
        now = time.monotonic()
        if self._seed_reconcile_task is not None and self._seed_reconcile_task.done():
            error = self._seed_reconcile_task.exception()
            if error is not None:
                self.stats.last_error = f"seed_reconcile: {_exception_text(error)}"
            self._seed_reconcile_task = None
        if self.seed_watchlist_limit > 0 and (
            self._seed_reconcile_task is None
            and (
                force or now - self._last_seed_reconcile >= self.seed_reconcile_seconds
            )
        ):
            self._seed_reconcile_task = asyncio.create_task(
                self._db_call(
                    self.store.seed_watchlist,
                    limit=self.seed_watchlist_limit,
                ),
                name="paper-live-seed-reconcile",
            )
            self._last_seed_reconcile = now

        if force:
            self._last_watch_refresh = now
            try:
                rows = await self._db_call(
                    self.store.load_watch_targets,
                    limit=self.max_watch_assets,
                )
            except Exception as exc:  # noqa: BLE001
                self._record_watch_refresh_failure(exc)
                return
            await self._apply_watch_targets(rows)
            return

        if self._watch_refresh_task is not None:
            if not self._watch_refresh_task.done():
                return
            task = self._watch_refresh_task
            self._watch_refresh_task = None
            try:
                rows = task.result()
            except Exception as exc:  # noqa: BLE001
                self._record_watch_refresh_failure(exc)
                return
            await self._apply_watch_targets(rows)
            return

        if now - self._last_watch_refresh < self.watch_refresh_seconds:
            return
        self._last_watch_refresh = now
        self._watch_refresh_task = asyncio.create_task(
            self._db_call(
                self.store.load_watch_targets,
                limit=self.max_watch_assets,
            ),
            name="paper-live-watch-refresh",
        )

    def _record_watch_refresh_failure(self, exc: Exception) -> None:
        self.stats.watch_refresh_failures += 1
        self.stats.watch_refresh_consecutive_failures += 1
        self.stats.last_watch_refresh_error = _exception_text(exc)

    async def _apply_watch_targets(self, rows: list[LiveWatchTarget]) -> None:
        self.stats.watch_refresh_consecutive_failures = 0
        self.stats.last_watch_refresh_error = None
        self.stats.last_watch_refresh_at = _now().isoformat()
        previous_targets = self.targets
        next_targets = {row.asset_id: row for row in rows}
        realtime_targets = build_realtime_targets_from_desired(
            [_desired(row) for row in rows]
        )
        added, removed = self.service.replace_targets(realtime_targets)
        refresh_requested = {
            asset_id
            for asset_id in set(previous_targets) & set(next_targets)
            if previous_targets[asset_id].refresh_nonce
            != next_targets[asset_id].refresh_nonce
        }
        for route_service in self.route_services.values():
            route_service.replace_targets(realtime_targets)
        self.targets = next_targets
        if added and (previous_targets or self.external_event_socket is not None):
            self.resyncing_assets.update(added)
            self._rest_resync_requested = True
            if self.external_event_socket is not None:
                for gap_assets in self.route_gap_assets.values():
                    gap_assets.update(added)
        if refresh_requested:
            self.resyncing_assets.update(refresh_requested)
            self.redundant_ready_assets.difference_update(refresh_requested)
            connected_sources = self._connected_sources()
            for asset_id in refresh_requested:
                self._refresh_routes_pending[asset_id] = set(connected_sources)
        for source, client in self.clients.items():
            if self.stats.route_states.get(source) != "CONNECTED":
                continue
            try:
                for chunk in _chunks(removed, 250):
                    await client.unsubscribe(chunk)
                for chunk in _chunks(added, 250):
                    await client.subscribe(chunk, initial=False)
                for chunk in _chunks(sorted(refresh_requested), 50):
                    await client.unsubscribe(chunk)
                    await client.subscribe(chunk, initial=False)
            except Exception:  # noqa: BLE001 - source reader owns reconnect.
                await client.close()
        for asset_id in removed:
            self.history.pop(asset_id, None)
            self.resyncing_assets.discard(asset_id)
            self.feed_mismatch_assets.discard(asset_id)
            self.redundant_ready_assets.discard(asset_id)
            for gap_assets in self.route_gap_assets.values():
                gap_assets.discard(asset_id)
            self._refresh_routes_pending.pop(asset_id, None)
            self._persisted_current_state.pop(asset_id, None)
        replace_target_assignments = getattr(
            self.store,
            "replace_target_assignments",
            None,
        )
        if replace_target_assignments is not None:
            await self._db_call(
                replace_target_assignments,
                worker_id=self.worker_id,
                asset_ids=next_targets,
            )
        control_observed_at = _now()
        for asset_id in sorted(set(previous_targets) & set(next_targets)):
            if _gate_state(previous_targets[asset_id]) == _gate_state(
                next_targets[asset_id]
            ):
                continue
            book = self.service.get_book(asset_id)
            if book is None:
                continue
            self.history[asset_id].append(
                checkpoint_from_live_book(
                    book,
                    next_targets[asset_id],
                    observed_at=control_observed_at,
                    connection_id=self.connection_id,
                    message_seq=self.message_seq,
                )
            )
        self.stats.watched_assets = len(rows)
        self._sync_external_subscription(rows)
        self._queue_market_terms_prefetch(rows, preferred_asset_ids=added)

    def _sync_external_subscription(self, rows: list[LiveWatchTarget]) -> None:
        path = self.external_subscription_file
        if path is None:
            return
        revision = tuple(
            sorted((row.asset_id, row.refresh_nonce) for row in rows)
        )
        if revision == self._external_subscription_revision:
            return
        try:
            snapshot = write_subscription_snapshot(
                path,
                tuple(
                    L2SubscriptionEntry(
                        asset_id=row.asset_id,
                        market_id=_int_or_zero(row.market_id),
                        condition_id=row.condition_id,
                        market_slug=row.market_slug,
                        market_state=row.market_state,
                        # This file is already the bounded Paper watch universe.
                        # Stream every selected target so stale registry state
                        # cannot hide exposure or an explicit live probe.
                        execution_eligible=True,
                        book_status="ok",
                        subscription_reason="paper_live_watchlist",
                        priority_at=row.refresh_nonce,
                    )
                    for row in sorted(rows, key=lambda item: item.asset_id)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - retain the prior atomic file.
            self.stats.external_subscription_failures += 1
            self.stats.last_external_subscription_error = _exception_text(exc)
            return
        self._external_subscription_revision = revision
        self.stats.external_subscription_count = snapshot.token_count
        self.stats.external_subscription_sha256 = snapshot.subscription_sha256
        self.stats.external_subscription_syncs += 1
        self.stats.last_external_subscription_error = None

    def _queue_market_terms_prefetch(
        self,
        rows: list[LiveWatchTarget],
        *,
        preferred_asset_ids: set[str],
    ) -> None:
        resolver = self.market_terms_resolver
        if getattr(resolver, "resolve_asset", None) is None:
            return
        needs_refresh = getattr(resolver, "needs_refresh", None)
        ordered = sorted(
            rows,
            key=lambda row: (row.asset_id not in preferred_asset_ids, row.asset_id),
        )
        for row in ordered:
            if row.asset_id in self._market_terms_prefetch_pending:
                continue
            if needs_refresh is not None and not needs_refresh(row.asset_id):
                continue
            try:
                self._market_terms_prefetch_queue.put_nowait(
                    (row.asset_id, row.condition_id)
                )
            except asyncio.QueueFull:
                self.stats.market_terms_prefetch_dropped += 1
                break
            self._market_terms_prefetch_pending.add(row.asset_id)
        self.stats.market_terms_prefetch_pending = len(
            self._market_terms_prefetch_pending
        )

    async def _market_terms_prefetch_loop(self) -> None:
        resolver = self.market_terms_resolver
        resolve_asset = getattr(resolver, "resolve_asset", None)
        if resolve_asset is None:
            return
        while True:
            asset_id, condition_id = await self._market_terms_prefetch_queue.get()
            try:
                await resolve_asset(asset_id=asset_id, condition_id=condition_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.market_terms_prefetch_failures += 1
                self.stats.last_market_terms_prefetch_error = _exception_text(exc)
            else:
                self.stats.market_terms_prefetch_succeeded += 1
                self.stats.last_market_terms_prefetch_error = None
            finally:
                self._market_terms_prefetch_pending.discard(asset_id)
                self.stats.market_terms_prefetch_pending = len(
                    self._market_terms_prefetch_pending
                )
                self._market_terms_prefetch_queue.task_done()
            await asyncio.sleep(0)

    async def _apply_feed_envelope_cooperatively(
        self,
        envelope: FeedEnvelope,
    ) -> None:
        observed_at = self._apply_feed_envelope_header(envelope)
        if observed_at is None:
            return
        durable_event = await self._append_persistent_feed_envelope(
            envelope,
            observed_at=observed_at,
        )
        if durable_event is False:
            return
        # A collector can forward a large WS batch after reconnect. Processing
        # it as one synchronous block starves health, intent and cancel loops.
        # Keep per-source ordering, but yield between bounded message groups.
        for index, message in enumerate(envelope.messages, start=1):
            if durable_event is None:
                causal_asset_ids = _market_causal_asset_ids(message)
                self._record_causal_kernel_event(
                    _market_causal_event_type(message),
                    asset_id=causal_asset_ids[0] if causal_asset_ids else None,
                    event_ts=observed_at,
                    payload={
                        "source": envelope.source,
                        "asset_ids": list(causal_asset_ids),
                    },
                )
            self._apply_message(envelope.source, message, observed_at=observed_at)
            if index % 128 == 0:
                await asyncio.sleep(0)
        if isinstance(durable_event, DurableCausalEvent):
            await self._ack_persistent_events((durable_event,))

    async def _apply_plain_feed_batch(
        self,
        envelopes: tuple[FeedEnvelope, ...],
    ) -> None:
        if not envelopes:
            return
        if self.persistent_event_kernel is None:
            for envelope in envelopes:
                await self._apply_feed_envelope_cooperatively(envelope)
            return

        prepared: list[tuple[FeedEnvelope, datetime, DurableCausalEvent]] = []
        for envelope in envelopes:
            observed_at = self._apply_feed_envelope_header(envelope)
            if observed_at is None:
                continue
            prepared.append(
                (
                    envelope,
                    observed_at,
                    self._build_persistent_feed_event(
                        envelope,
                        observed_at=observed_at,
                    ),
                )
            )
        if not prepared:
            return

        events = tuple(row[2] for row in prepared)
        try:
            appends = await self._db_call(
                self.persistent_event_kernel.append_many,
                events,
                worker_id=self.worker_id,
            )
        except Exception:
            self._block_persistent_kernel()
            raise
        if len(appends) != len(prepared):
            self._block_persistent_kernel()
            raise RuntimeError("persistent kernel returned an incomplete feed batch")

        self.stats.persistent_kernel_feed_batches += 1
        self.stats.persistent_kernel_feed_envelopes += len(prepared)
        self.stats.persistent_kernel_feed_max_batch_size = max(
            self.stats.persistent_kernel_feed_max_batch_size,
            len(prepared),
        )
        events_to_ack: list[DurableCausalEvent] = []
        applied_messages = 0
        for (envelope, observed_at, event), append in zip(
            prepared,
            appends,
            strict=True,
        ):
            if not append.should_apply:
                self.stats.persistent_kernel_duplicate_events += 1
                continue
            for message in envelope.messages:
                self._apply_message(
                    envelope.source,
                    message,
                    observed_at=observed_at,
                )
                applied_messages += 1
                if applied_messages % 128 == 0:
                    await asyncio.sleep(0)
            events_to_ack.append(event)

        self.stats.persistent_kernel_feed_db_transactions_avoided += max(
            0,
            len(prepared) - 1,
        ) + max(0, len(events_to_ack) - 1)
        if events_to_ack:
            await self._ack_persistent_events(tuple(events_to_ack))

    def _apply_feed_envelope(self, envelope: FeedEnvelope) -> None:
        observed_at = self._apply_feed_envelope_header(envelope)
        if observed_at is None:
            return
        for message in envelope.messages:
            causal_asset_ids = _market_causal_asset_ids(message)
            self._record_causal_kernel_event(
                _market_causal_event_type(message),
                asset_id=causal_asset_ids[0] if causal_asset_ids else None,
                event_ts=observed_at,
                payload={
                    "source": envelope.source,
                    "asset_ids": list(causal_asset_ids),
                },
            )
            self._apply_message(envelope.source, message, observed_at=observed_at)

    def _apply_feed_envelope_header(
        self,
        envelope: FeedEnvelope,
    ) -> datetime | None:
        if envelope.error:
            self.stats.last_error = f"{envelope.source}: {envelope.error}"
        if envelope.state:
            self.stats.route_states[envelope.source] = envelope.state
            if envelope.state != "CONNECTED" and not self._connected_sources():
                self.service.mark_reconnect_stale("all_paper_ws_routes_disconnected")
                self.history.clear()
                self._seen_events.clear()
                self.connection_id = f"paper-ws-{uuid4().hex}"
                self.resyncing_assets = set(self.service.subscribed_token_ids)
                self._rest_resync_requested = bool(self.resyncing_assets)
            self._update_transport_state()
        if not envelope.messages:
            return None
        observed_at = envelope.received_at
        self.stats.websocket_messages += 1
        self.stats.route_messages[envelope.source] = (
            self.stats.route_messages.get(envelope.source, 0) + 1
        )
        self.stats.last_message_at = observed_at.isoformat()
        self.stats.route_last_message_at[envelope.source] = observed_at.isoformat()
        self.message_seq += 1
        return observed_at

    def _apply_message(
        self, source: str, message: dict[str, Any], *, observed_at: datetime
    ) -> None:
        route_service = self.route_services[source]
        event_type = str(message.get("event_type") or message.get("type") or "")
        if event_type == "connection_gap":
            asset_id = str(message.get("asset_id") or "").strip()
            if asset_id and asset_id in self.targets:
                self.route_gap_assets[source].add(asset_id)
                route_book = route_service.get_book(asset_id)
                if route_book is not None:
                    route_book.mark_stale(f"{source}_connection_gap")
                self.redundant_ready_assets.discard(asset_id)
                if not self._usable_route_sources(asset_id):
                    self.resyncing_assets.add(asset_id)
                self._append_gate_checkpoint(asset_id, observed_at=observed_at)
            return
        if event_type in {"connection_recovered", "connection_heartbeat"}:
            return
        if event_type == "last_trade_price":
            trade = normalize_polymarket_trade(message)
            if trade is None or trade.token_id not in self.targets:
                return
            signature = _trade_event_signature(trade)
            if signature in self._seen_events:
                self.stats.duplicate_events += 1
                self._seen_events.move_to_end(signature)
                return
            self._remember_event(signature)
            if len(self._maker_trades) >= self._maker_trade_limit:
                self._maker_trades.popleft()
                self.stats.maker_trade_drops += 1
            self._maker_trades.append(trade)
            self.stats.maker_trade_events += 1
            return
        affected: set[str] = set()
        applied: set[str] = set()
        refresh_confirmed: set[str] = set()
        for event in normalize_polymarket_event(message):
            affected.add(event.token_id)
            route_output = _apply_current_event(route_service, event)
            pending_routes = self._refresh_routes_pending.get(event.token_id)
            route_book = route_service.get_book(event.token_id)
            if (
                pending_routes is not None
                and route_output is not None
                and route_book is not None
                and route_book.ready
            ):
                pending_routes.discard(source)
                if not pending_routes:
                    self._refresh_routes_pending.pop(event.token_id, None)
                    refresh_confirmed.add(event.token_id)
            if (
                route_output is not None
                and route_output.applied
                and (
                    isinstance(event, NormalizedBookSnapshot)
                    or self.external_event_socket is not None
                )
            ):
                self.route_gap_assets[source].discard(event.token_id)
            signature = _event_signature(event)
            if signature in self._seen_events:
                self.stats.duplicate_events += 1
                self._seen_events.move_to_end(signature)
                continue
            self._remember_event(signature)
            prior_displayed_size: Decimal | None = None
            if isinstance(event, NormalizedBookDelta):
                prior_book = self.service.get_book(event.token_id)
                if prior_book is not None and prior_book.ready:
                    prior_levels = (
                        prior_book.bids if event.side == "bid" else prior_book.asks
                    )
                    prior_displayed_size = max(
                        Decimal("0"), prior_levels.get(event.price, Decimal("0"))
                    )
            output = _apply_current_event(self.service, event)
            if output is None:
                self.stats.out_of_order_events += 1
            elif output.applied:
                applied.add(output.token_id)
                book = self.service.get_book(event.token_id)
                if book is not None:
                    if isinstance(event, NormalizedBookDelta):
                        self.engine.liquidity_overlay.on_level_update(
                            asset_id=event.token_id,
                            book_generation=book.generation,
                            side=str(event.side).upper(),
                            price_tick=event.price,
                            displayed_size=max(Decimal(0), event.size),
                            event_id=signature,
                        )
                    self._queue_maker_research_book_event(
                        event,
                        signature=signature,
                        book=book,
                        prior_displayed_size=prior_displayed_size,
                    )

        gate_changed: set[str] = set()
        for asset_id in affected:
            mismatched = self._route_books_mismatch(asset_id)
            was_mismatched = asset_id in self.feed_mismatch_assets
            if mismatched:
                self.feed_mismatch_assets.add(asset_id)
            else:
                self.feed_mismatch_assets.discard(asset_id)
            if mismatched != was_mismatched:
                gate_changed.add(asset_id)
            redundant_ready = self._route_books_confirmed(asset_id)
            was_redundant_ready = asset_id in self.redundant_ready_assets
            if redundant_ready:
                self.redundant_ready_assets.add(asset_id)
            else:
                self.redundant_ready_assets.discard(asset_id)
            if redundant_ready != was_redundant_ready:
                gate_changed.add(asset_id)
            if asset_id in refresh_confirmed:
                gate_changed.add(asset_id)
        self.stats.feed_mismatch_assets = len(self.feed_mismatch_assets)

        # A route snapshot can be authoritative even when the merged REST-seeded
        # book has a newer venue timestamp. Route usability is therefore also a
        # valid recovery signal; the remaining source count still limits the
        # checkpoint to B until both independent routes recover.
        route_recovered = {
            asset_id for asset_id in affected if self._usable_route_sources(asset_id)
        }
        recovered_assets = applied | refresh_confirmed | route_recovered
        # A refresh nonce deliberately invalidates every route that was connected
        # when the request was observed.  Do not let the first returning route
        # clear the merged resync gate while another route still owes a baseline.
        recovered_assets.difference_update(self._refresh_routes_pending)
        self.resyncing_assets.difference_update(recovered_assets)
        for asset_id in applied | gate_changed:
            book = self.service.get_book(asset_id)
            target = self.targets.get(asset_id)
            if book is None or target is None:
                continue
            checkpoint_target = self._effective_target(asset_id, target)
            self.history[asset_id].append(
                checkpoint_from_live_book(
                    book,
                    checkpoint_target,
                    observed_at=observed_at,
                    connection_id=self.connection_id,
                    message_seq=self.message_seq,
                )
            )

    def _append_gate_checkpoint(
        self,
        asset_id: str,
        *,
        observed_at: datetime,
    ) -> None:
        book = self.service.get_book(asset_id)
        target = self.targets.get(asset_id)
        if book is None or target is None or not book.ready:
            return
        self.history[asset_id].append(
            checkpoint_from_live_book(
                book,
                self._effective_target(asset_id, target),
                observed_at=observed_at,
                connection_id=self.connection_id,
                message_seq=self.message_seq,
            )
        )

    def _advance_rest_resync(self) -> None:
        if self._rest_resync_task is not None:
            if not self._rest_resync_task.done():
                return
            task = self._rest_resync_task
            self._rest_resync_task = None
            try:
                payloads = task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.rest_resync_failures += 1
                self.stats.last_error = f"rest_resync: {_exception_text(exc)}"
            else:
                restored = 0
                for asset_id, payload in payloads.items():
                    if asset_id not in self.resyncing_assets:
                        continue
                    output = self.service.process_rest_book(
                        token_id=asset_id,
                        payload=payload,
                        event_ts_ms=_rest_event_ts_ms(payload),
                    )
                    if output.applied:
                        restored += 1
                        book = self.service.get_book(asset_id)
                        target = self.targets.get(asset_id)
                        if book is not None and target is not None:
                            self.message_seq += 1
                            self.history[asset_id].append(
                                checkpoint_from_live_book(
                                    book,
                                    self._effective_target(asset_id, target),
                                    observed_at=_now(),
                                    connection_id="paper-rest-seed",
                                    message_seq=self.message_seq,
                                )
                            )
                        if self.external_event_socket is not None:
                            for source, route_service in self.route_services.items():
                                route_service.process_rest_book(
                                    token_id=asset_id,
                                    payload=payload,
                                    event_ts_ms=_rest_event_ts_ms(payload),
                                )
                                self.route_gap_assets[source].add(asset_id)
                self.stats.rest_resync_books += restored
                if str(self.stats.last_error or "").startswith("rest_resync:"):
                    self.stats.last_error = None

        if (
            not self._rest_resync_requested
            or self.rest_client is None
            or not self._connected_sources()
        ):
            return
        stale_ids = [
            asset_id
            for asset_id in self.service.subscribed_token_ids
            if (book := self.service.get_book(asset_id)) is None or not book.ready
        ]
        if not stale_ids:
            self._rest_resync_requested = False
            return
        now = time.monotonic()
        if now - self._last_rest_resync < self.rest_resync_retry_seconds:
            return
        self.resyncing_assets.update(stale_ids)
        self.stats.rest_resync_attempts += 1
        self._last_rest_resync = now
        self._rest_resync_task = asyncio.create_task(
            self.rest_client.get_books(stale_ids, batch_size=500),
            name="paper-live-rest-resync",
        )

    def _remember_event(self, signature: str) -> None:
        self._seen_events[signature] = None
        if len(self._seen_events) > self._seen_event_limit:
            self._seen_events.popitem(last=False)

    def _route_books_mismatch(self, asset_id: str) -> bool:
        if len(self.route_services) < 2:
            return False
        books = [service.get_book(asset_id) for service in self.route_services.values()]
        if any(book is None or not book.ready for book in books):
            return False
        ready_books = [book for book in books if book is not None]
        timestamps = {book.last_event_ts_ms for book in ready_books}
        if None in timestamps or len(timestamps) != 1:
            return False
        return len({book.fingerprint() for book in ready_books}) != 1

    def _route_books_confirmed(self, asset_id: str) -> bool:
        if len(self.route_services) < 2:
            return False
        if len(self._usable_route_sources(asset_id)) != len(self.route_services):
            return False
        books = [service.get_book(asset_id) for service in self.route_services.values()]
        if any(book is None or not book.ready for book in books):
            return False
        # Independent feeds normally deliver the same delta a few milliseconds
        # apart. Different event timestamps mean one route is ahead, not that
        # the merged book has a gap. Only a same-event fingerprint conflict is
        # evidence that the two feeds disagree.
        return not self._route_books_mismatch(asset_id)

    def _usable_route_sources(self, asset_id: str) -> set[str]:
        return {
            source
            for source, service in self.route_services.items()
            if self.stats.route_states.get(source) == "CONNECTED"
            and asset_id not in self.route_gap_assets[source]
            and (book := service.get_book(asset_id)) is not None
            and book.ready
        }

    def _effective_target(
        self, asset_id: str, target: LiveWatchTarget
    ) -> LiveWatchTarget:
        if (
            self._persistent_kernel_blocked
            or asset_id in self.feed_mismatch_assets
            or asset_id in self.resyncing_assets
        ):
            return replace(target, coverage_grade="D", has_gap=True)
        usable_sources = self._usable_route_sources(asset_id)
        if len(usable_sources) >= 2:
            grade = "A"
        elif len(usable_sources) == 1:
            grade = "B"
        else:
            grade = "D"
        return replace(
            target,
            coverage_grade=grade,
            has_gap=not bool(usable_sources),
        )

    async def _intent_claim_loop(self) -> None:
        while True:
            await asyncio.sleep(self.intent_poll_seconds)
            try:
                self._harvest_intent_claim()
                self._schedule_intent_claim()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"intent_claim_schedule: {_exception_text(exc)}"

    def _harvest_intent_claim(self) -> None:
        task = self._intent_claim_task
        if task is None or not task.done():
            return
        self._intent_claim_task = None
        try:
            claimed = task.result()
        except Exception as exc:  # noqa: BLE001
            self.stats.last_error = f"intent_poll: {_exception_text(exc)}"
            return
        if str(self.stats.last_error or "").startswith("intent_poll:"):
            self.stats.last_error = None
        self._claimed_intents.extend(claimed)

    def _schedule_intent_claim(self) -> None:
        if self._intent_claim_task is not None:
            return
        if not any(self.history.values()):
            return
        gate = evaluate_backpressure(
            BackpressureSnapshot(
                intent_backlog=(
                    len(self.pending)
                    + int(self.stats.queued_intents)
                    + int(self.stats.processing_intents)
                ),
                db_writer_lag_ms=0,
                book_apply_lag_ms=0,
                ws_connected=bool(self._connected_sources()),
                clock_skew_ms=0,
                model_registry_available=True,
            )
        )
        self.stats.backpressure_status = str(gate["status"])
        self.stats.backpressure_reasons = list(gate["reasons"])
        if bool(gate["fail_closed"]):
            return
        self._last_intent_poll = time.monotonic()
        # Claiming is deliberately independent of the serial intent DB
        # executor. It acknowledges newly submitted commands while the prior
        # command is still executing, but leaves all risk, matching and ledger
        # work on the deterministic single-intent path.
        self._intent_claim_task = asyncio.create_task(
            self._claim_db_call(
                self.store.claim,
                worker_id=self.worker_id,
                limit=50,
            ),
            name="paper-live-intent-claim",
        )

    async def _poll_intents(self) -> None:
        # The dedicated claim loop normally harvests immediately. This extra
        # call preserves deterministic direct/unit-driven polling behavior.
        self._harvest_intent_claim()
        while self._claimed_intents:
            row = self._claimed_intents.popleft()
            decision = await self._causal_checkpoint(
                intent_id=row.intent_id,
                asset_id=row.intent.asset_id,
                as_of=row.intent.decision_ts,
                event_types=("STRATEGY_OBSERVATION", "STRATEGY_INTENT"),
            )
            self.pending[row.intent_id] = PendingIntent(
                row=row,
                decision_checkpoint=decision,
                execution_profile=row.execution_profile,
            )

    async def _execute_due(self) -> None:
        now = _now()
        due = [
            item
            for item in self.pending.values()
            if self._pending_modeled_arrival_ts(item) <= now
        ]
        if due and getattr(self, "lifecycle_scheduler_enforce_order", False):
            try:
                schedule = self.paper_intent_scheduler.order_due(
                    due,
                    arrival_ts=self._pending_modeled_arrival_ts,
                )
                await self._persist_scheduler_schedule(schedule)
            except Exception as exc:  # noqa: BLE001
                # Authority mode cannot silently fall back to claim/dict order.
                self.stats.lifecycle_scheduler_authority_failures += 1
                self.stats.last_lifecycle_scheduler_error = _exception_text(exc)
                return
            due = list(schedule.ordered_items)
            self.stats.lifecycle_scheduler_authority_batches += 1
            self.stats.lifecycle_scheduler_authority_intents += len(due)
            self.stats.lifecycle_scheduler_authority_last_intent_ids = list(
                schedule.intent_ids
            )
            self.stats.lifecycle_scheduler_authority_last_journal_hash = (
                schedule.journal_hash
            )
        for item in due:
            affinity_token = _INTENT_DB_AFFINITY.set(True)
            intent_started = time.monotonic()
            intent_id = item.row.intent_id
            intent = item.row.intent
            modeled_arrival_ts = self._pending_modeled_arrival_ts(item)
            arrival = None
            reservation_active = False
            oms_decision = None
            execution_profile = None
            keep_pending = False
            try:
                if (
                    self.operations_admission_shadow
                    or self.operations_admission_enforce
                ):
                    operations_admission = await asyncio.to_thread(
                        load_paper_admission,
                        self.operations_status_path,
                        post_only=bool(intent.post_only),
                        time_in_force=str(intent.order_type),
                        order_notional=(
                            intent.size
                            if str(intent.amount_unit).upper() == "QUOTE"
                            else intent.size * intent.limit_price
                        ),
                        max_age_seconds=self.operations_status_max_age_seconds,
                        yellow_max_notional=self.operations_yellow_max_notional,
                    )
                    self.stats.operations_admission_evaluations += 1
                    self.stats.operations_admission_level = (
                        operations_admission.operational_level
                    )
                    if not operations_admission.allowed:
                        self.stats.operations_admission_blocked += 1
                        self.stats.last_operations_admission_reason = ",".join(
                            operations_admission.reasons
                        )
                        if self.operations_admission_enforce:
                            self.stats.operations_admission_enforced_rejections += 1
                            reason = "operations:" + ",operations:".join(
                                operations_admission.reasons
                            )
                            result = await asyncio.to_thread(
                                self.engine.reject,
                                intent,
                                reason=reason,
                                decision_checkpoint=item.decision_checkpoint,
                                arrival_checkpoint=None,
                            )
                            await self._complete_intent(intent_id, result)
                            self.stats.completed_intents += 1
                            self.stats.rejected_intents += 1
                            continue
                venue_regime = await self._bind_venue_regime(intent_id)
                if venue_regime is not None:
                    intent = replace(
                        intent,
                        venue_regime_id=str(venue_regime["regime_id"]),
                        venue_regime_source_hash=str(venue_regime["source_hash"]),
                    )
                if self.market_terms_resolver is not None:
                    try:
                        terms = await self.market_terms_resolver.resolve(intent)
                    except Exception as exc:  # noqa: BLE001
                        self.stats.market_terms_failures += 1
                        reason = (
                            str(exc)
                            if isinstance(exc, MarketTermsUnavailable)
                            else _exception_text(exc)
                        )
                        result = await asyncio.to_thread(
                            self.engine.reject,
                            intent,
                            reason=f"market_terms_unavailable:{reason[:300]}",
                            decision_checkpoint=item.decision_checkpoint,
                            arrival_checkpoint=arrival,
                            status="DATA_NOT_READY",
                        )
                        await self._complete_intent(intent_id, result)
                        self.stats.completed_intents += 1
                        self.stats.rejected_intents += 1
                        continue
                    intent = terms.apply(intent)
                    item.row = replace(item.row, intent=intent)
                    await self._db_call(
                        self.store.persist_intent_market_terms,
                        intent_id,
                        terms,
                    )
                    self.stats.market_terms_resolved += 1

                execution_profile = item.execution_profile
                if execution_profile is None:
                    load_profile = getattr(
                        self.store,
                        "load_execution_profile_decision",
                        None,
                    )
                    if load_profile is None:
                        if self.authority_controller is not None:
                            raise RuntimeError(
                                "authority worker requires durable execution profiles"
                            )
                    else:
                        execution_profile = await self._db_call(
                            load_profile,
                            intent_id,
                        )
                    item.execution_profile = execution_profile
                profile_engine = (
                    TakerOnlyPaperExecutionEngine(
                        execution_profile.to_execution_config()
                    )
                    if execution_profile is not None
                    else self.engine
                )
                modeled_arrival_ts = profile_engine.modeled_arrival_ts(
                    intent, item.decision_checkpoint
                )
                if modeled_arrival_ts > _now():
                    mark_delay = getattr(self.store, "mark_venue_delay", None)
                    if mark_delay is None:
                        raise RuntimeError(
                            "market-specific delay requires durable venue-delay support"
                        )
                    delay_started_at = profile_engine.config.latency.arrival_ts(
                        intent.decision_ts
                    )
                    keep_pending = bool(
                        await self._db_call(
                            mark_delay,
                            intent_id,
                            delay_started_at=delay_started_at,
                            delay_until=modeled_arrival_ts,
                            delay_source=(
                                intent.venue_delay_source or "market_specific_delay"
                            ),
                        )
                    )
                    await self._causal_lifecycle_event(
                        "VENUE_DELAYED",
                        intent_id=intent_id,
                        asset_id=intent.asset_id,
                        event_ts=delay_started_at,
                        payload={
                            "delay_until": modeled_arrival_ts,
                            "delay_ms": intent.venue_taker_delay_ms,
                            "delay_source": intent.venue_delay_source,
                            "uncancelable": keep_pending,
                        },
                    )
                    continue
                if execution_profile is not None:
                    arrival = (
                        await self._db_call(
                            self.store.load_book_checkpoint_model,
                            execution_profile.book_checkpoint_id,
                        )
                        if execution_profile.book_checkpoint_id is not None
                        else None
                    )
                    if (
                        execution_profile.book_checkpoint_id is not None
                        and arrival is None
                    ):
                        raise RuntimeError(
                            "frozen execution profile checkpoint is unavailable"
                        )
                else:
                    arrival = await self._causal_checkpoint(
                        intent_id=intent_id,
                        asset_id=intent.asset_id,
                        as_of=modeled_arrival_ts,
                    )

                risk_context = PaperRiskContext()
                if self.portfolio_store is not None:
                    risk_context = await self._load_risk_context(intent)
                risk_checkpoint = await self._causal_checkpoint(
                    intent_id=intent_id,
                    asset_id=intent.asset_id,
                    as_of=_now(),
                )
                await self._sync_token_degradation(intent.asset_id, risk_checkpoint)
                degradation_reasons = list(
                    self.degradation_controller.admission_reasons(
                        token_id=intent.asset_id,
                        model_id=self.engine.config.model_version,
                        account_id=self.degradation_account_id,
                    )
                )
                if self.unified_admission_shadow or self.unified_admission_enforce:
                    try:
                        admission = await self._observe_unified_admission(
                            intent_id,
                            intent,
                        )
                        self.stats.unified_admission_evaluations += 1
                        self.stats.unified_admission_mode = (
                            admission.jurisdiction_mode.value
                        )
                        self.stats.last_unified_admission_reason = ",".join(
                            admission.reason_codes
                        )
                        if not admission.allowed:
                            self.stats.unified_admission_blocked += 1
                            if self.unified_admission_enforce:
                                degradation_reasons.extend(
                                    f"eligibility:{reason}"
                                    for reason in admission.reason_codes
                                )
                                self.stats.unified_admission_enforced_rejections += 1
                    except Exception as exc:  # noqa: BLE001
                        self.stats.unified_admission_failures += 1
                        self.stats.last_unified_admission_reason = _exception_text(exc)
                        if self.unified_admission_enforce:
                            degradation_reasons.append(
                                "eligibility:admission_service_unavailable"
                            )
                if degradation_reasons:
                    risk_context = replace(
                        risk_context,
                        degradation_reasons=tuple(degradation_reasons),
                    )
                    self.stats.degradation_rejections += 1
                    self.stats.last_degradation_reason = ",".join(degradation_reasons)
                risk_decision = self.execution_kernel.evaluate_risk(
                    intent,
                    arrival_checkpoint=risk_checkpoint,
                    context=risk_context,
                )
                await self._db_call(
                    self.store.persist_risk_decision,
                    intent_id,
                    risk_decision,
                )
                await self._db_call(
                    self.store.persist_checkpoints,
                    [
                        checkpoint
                        for checkpoint in (item.decision_checkpoint, arrival)
                        if checkpoint is not None
                    ],
                )
                try:
                    proposed_profile = self.execution_profile_resolver.resolve(
                        intent_id=intent_id,
                        intent=intent,
                        checkpoint=arrival,
                        risk_status=risk_decision.status,
                        config=self.engine.config,
                    )
                    freeze_profile = getattr(
                        self.store,
                        "freeze_execution_profile_decision",
                        None,
                    )
                    if freeze_profile is None:
                        if self.authority_controller is not None:
                            raise RuntimeError(
                                "authority worker requires durable execution profiles"
                            )
                        execution_profile = proposed_profile
                    else:
                        execution_profile = await self._db_call(
                            freeze_profile,
                            proposed_profile,
                        )
                    item.execution_profile = execution_profile
                    load_checkpoint = getattr(
                        self.store,
                        "load_book_checkpoint_model",
                        None,
                    )
                    if (
                        load_checkpoint is None
                        and self.authority_controller is not None
                        and execution_profile.book_checkpoint_id is not None
                    ):
                        raise RuntimeError(
                            "authority worker requires durable profile checkpoints"
                        )
                    frozen_arrival = (
                        await self._db_call(
                            load_checkpoint,
                            execution_profile.book_checkpoint_id,
                        )
                        if load_checkpoint is not None
                        and execution_profile.book_checkpoint_id is not None
                        else arrival
                    )
                    if (
                        execution_profile.book_checkpoint_id is not None
                        and frozen_arrival is None
                    ):
                        raise RuntimeError(
                            "frozen execution profile checkpoint is unavailable"
                        )
                    arrival = frozen_arrival
                    profile_engine = TakerOnlyPaperExecutionEngine(
                        execution_profile.to_execution_config()
                    )
                    modeled_arrival_ts = profile_engine.modeled_arrival_ts(
                        intent, item.decision_checkpoint
                    )
                    profile_name = execution_profile.profile.value
                    self.stats.execution_profile_decisions += 1
                    self.stats.execution_profile_counts[profile_name] = (
                        self.stats.execution_profile_counts.get(profile_name, 0) + 1
                    )
                    self.stats.last_execution_profile = profile_name
                    self.stats.last_execution_profile_error = None
                except Exception as exc:
                    self.stats.execution_profile_failures += 1
                    self.stats.last_execution_profile_error = _exception_text(exc)
                    raise
                await self._causal_lifecycle_event(
                    "RISK_DECISION",
                    intent_id=intent_id,
                    asset_id=intent.asset_id,
                    payload={
                        "status": risk_decision.status,
                        "accepted": risk_decision.accepted,
                    },
                )
                if not risk_decision.accepted:
                    self.stats.risk_rejections += 1
                else:
                    oms_decision = await self._observe_own_order_oms(
                        intent_id,
                        intent,
                    )
                    if self.own_order_oms_enforce and (
                        oms_decision is None or not oms_decision.accepted
                    ):
                        reason = (
                            "own_order_oms:gate_unavailable"
                            if oms_decision is None
                            else self.own_order_oms_gate.rejection_reason(oms_decision)
                        )
                        result = await asyncio.to_thread(
                            self.engine.reject,
                            intent,
                            reason=reason,
                            decision_checkpoint=item.decision_checkpoint,
                            arrival_checkpoint=arrival,
                        )
                        await self._complete_intent(intent_id, result)
                        self.stats.completed_intents += 1
                        self.stats.rejected_intents += 1
                        continue
                    # Execution timestamps belong to the modeled lifecycle.
                    # Control-plane work may finish much later on a remote DB;
                    # that wall-clock delay must not become simulated latency.
                    submit_request_ts = execution_profile.to_execution_config().latency.submit_request_ts(
                        intent.decision_ts
                    )
                    venue_shadow = await self._observe_venue_admission(
                        intent_id,
                        intent,
                        submit_request_ts,
                    )
                    await self._causal_lifecycle_event(
                        "VENUE_ADMISSION",
                        intent_id=intent_id,
                        asset_id=intent.asset_id,
                        event_ts=submit_request_ts,
                        payload={
                            "disposition": getattr(
                                venue_shadow, "disposition", "UNAVAILABLE"
                            ),
                            "reason": getattr(
                                venue_shadow, "reason", "gate_unavailable"
                            ),
                        },
                    )
                    await self._record_batch_admission(
                        intent_id,
                        paper_admitted=True,
                        venue_shadow=venue_shadow,
                    )
                    if self.venue_admission_enforce:
                        venue_action = _venue_authority_action(venue_shadow)
                        if venue_action == "DEFER":
                            self.stats.venue_enforced_deferrals += 1
                            keep_pending = True
                            continue
                        if venue_action == "REJECT":
                            result = await asyncio.to_thread(
                                self.engine.reject,
                                intent,
                                reason=f"venue_admission:{venue_shadow.reason}",
                                decision_checkpoint=item.decision_checkpoint,
                                arrival_checkpoint=arrival,
                            )
                            await self._record_batch_admission(
                                intent_id,
                                paper_admitted=False,
                                venue_shadow=venue_shadow,
                            )
                            await self._complete_intent(intent_id, result)
                            self.stats.completed_intents += 1
                            self.stats.rejected_intents += 1
                            self.stats.venue_enforced_rejections += 1
                            continue
                    arrival_ts = modeled_arrival_ts
                    if self.portfolio_store is not None:
                        await self._db_call(
                            self.portfolio_store.reserve_order,
                            intent_id,
                            intent,
                            execution_profile.to_execution_config(),
                        )
                        reservation_active = True
                    await self._causal_lifecycle_event(
                        "PAPER_COMMAND_ARRIVAL",
                        intent_id=intent_id,
                        asset_id=intent.asset_id,
                        event_ts=arrival_ts,
                        payload={
                            "checkpoint_id": getattr(arrival, "checkpoint_id", None)
                        },
                    )
                    submit_arrived = await self._db_call(
                        self.store.mark_submit_arrival,
                        intent_id,
                        request_ts=submit_request_ts,
                        arrival_ts=arrival_ts,
                    )
                    if not submit_arrived:
                        if self.portfolio_store is not None and reservation_active:
                            await self._db_call(
                                self.portfolio_store.release_order_reservation,
                                intent_id,
                                reason="cancel_arrived_before_submit",
                            )
                        continue

                portfolio: PaperPortfolioSnapshot | None = None
                if self.portfolio_store is not None and risk_decision.accepted:
                    portfolio = await self._db_call(
                        self.portfolio_store.portfolio_snapshot,
                        intent.strategy_id,
                        intent.asset_id,
                        intent_id=intent_id,
                    )
                result, _ = await self._db_call(
                    self.execution_kernel.execute,
                    intent,
                    decision_checkpoint=item.decision_checkpoint,
                    arrival_checkpoint=arrival,
                    portfolio=portfolio,
                    risk_decision=risk_decision,
                    arrival_ts_override=(
                        arrival_ts if risk_decision.accepted else None
                    ),
                    intent_sequence=intent_id,
                    fidelity_context=PaperFidelityContext(
                        venue_regime_id=(intent.venue_regime_id or "UNBOUND"),
                        venue_emulated=intent.venue_regime_id is not None,
                        global_liquidity_overlay_verified=(
                            self.global_liquidity_overlay_verified
                        ),
                        finality_model_version="paper-fill-finality-v1",
                        valuation_model_version="paper-multiview-nav-v1",
                        latency_model_version=(
                            execution_profile.latency_model_version
                            if execution_profile is not None
                            else "UNBOUND"
                        ),
                        calibration_domain_status=(
                            execution_profile.calibration_domain
                            if execution_profile is not None
                            else "UNCALIBRATED"
                        ),
                        confidence_reasons=(
                            execution_profile.reason_codes
                            if execution_profile is not None
                            else ("execution_profile_unavailable",)
                        ),
                    ),
                    execution_profile=execution_profile,
                )
                await self._persist_execution_tca(
                    intent_id,
                    result,
                    decision_checkpoint=item.decision_checkpoint,
                    arrival_checkpoint=arrival,
                    risk_decision=risk_decision,
                )
                if not risk_decision.accepted:
                    await self._record_batch_admission(
                        intent_id,
                        paper_admitted=False,
                        venue_shadow=None,
                    )
                await self._complete_intent(
                    intent_id,
                    result,
                    finalize_oms=bool(
                        oms_decision is not None
                        and getattr(oms_decision, "accepted", False)
                    ),
                )
                if (
                    result.status in {"WORKING", "PARTIAL"}
                    and result.intent.post_only
                    and arrival is not None
                ):
                    maker_domain = self.maker_model_domain_resolver.resolve()
                    self.stats.maker_model_domain_status = maker_domain.domain_status
                    self.stats.maker_model_domain_decision_hash = (
                        maker_domain.decision_hash
                    )
                    maker_queue_initialized = await self._db_call(
                        self.store.initialize_maker_queue,
                        intent_id,
                        result,
                        arrival,
                        model_version=maker_domain.queue_model_version,
                        model_domain_decision=maker_domain.as_dict(),
                    )
                    if maker_queue_initialized:
                        self._maker_research_levels.add(
                            (
                                result.intent.asset_id,
                                result.intent.side,
                                result.intent.limit_price,
                            )
                        )
                self.stats.completed_intents += 1
                if result.status not in {"FILLED", "PARTIAL", "WORKING"}:
                    self.stats.rejected_intents += 1
            except Exception as exc:  # noqa: BLE001
                await self._causal_lifecycle_event(
                    "PAPER_REJECT",
                    intent_id=intent_id,
                    asset_id=intent.asset_id,
                    payload={"reason": f"{exc.__class__.__name__}: {str(exc)[:300]}"},
                )
                if self.portfolio_store is not None:
                    await self._db_call(
                        self.portfolio_store.release_order_reservation,
                        intent_id,
                        reason=f"execution_failed:{exc.__class__.__name__}",
                    )
                await self._db_call(
                    self.store.fail,
                    intent_id,
                    f"{exc.__class__.__name__}: {str(exc)[:800]}",
                )
                if (
                    self.own_order_oms_gate is not None
                    and oms_decision is not None
                    and getattr(oms_decision, "accepted", False)
                ):
                    try:
                        await self._db_call(
                            self.own_order_oms_gate.store.finalize_intent,
                            intent_id,
                            execution_status="REJECTED",
                            event_id=f"paper-oms-execution-failed:{intent_id}",
                        )
                        self.stats.own_order_oms_finalizations += 1
                    except Exception as oms_exc:  # noqa: BLE001
                        self.stats.own_order_oms_failures += 1
                        self.stats.last_own_order_oms_reason = _exception_text(oms_exc)
                await self._record_batch_failure(intent_id, exc)
                self.stats.rejected_intents += 1
            finally:
                elapsed_ms = (time.monotonic() - intent_started) * 1000
                self.stats.intent_processing_last_ms = elapsed_ms
                self.stats.intent_processing_max_ms = max(
                    self.stats.intent_processing_max_ms,
                    elapsed_ms,
                )
                _INTENT_DB_AFFINITY.reset(affinity_token)
                if not keep_pending:
                    self.pending.pop(intent_id, None)

    async def _persist_scheduler_schedule(self, schedule: Any) -> None:
        worker = getattr(self, "simulator_artifact_worker", None)
        run_id = getattr(self, "simulator_run_id", None)
        if worker is None or run_id is None:
            return
        try:
            result = await self._db_call(
                worker.replay_events,
                run_id=run_id,
                events=schedule.events,
            )
            if (
                result.journal_hash != schedule.journal_hash
                or result.event_count != schedule.event_count
            ):
                raise RuntimeError(
                    "persisted scheduler journal does not match authority"
                )
            self.stats.simulator_artifact_events += result.event_count
            self.stats.simulator_artifact_events_inserted += (
                result.persisted_event_count
            )
            self.stats.simulator_artifact_event_duplicates += (
                result.event_count - result.persisted_event_count
            )
            self.stats.lifecycle_scheduler_authority_last_journal_hash = (
                result.journal_hash
            )
            self.stats.last_simulator_artifact_error = None
        except Exception as exc:
            self.stats.simulator_artifact_failures += 1
            self.stats.last_simulator_artifact_error = _exception_text(exc)
            raise

    def _modeled_arrival_ts(
        self,
        intent: OrderIntent,
        decision_checkpoint: ArrivalBookCheckpoint | None,
    ) -> datetime:
        modeled = getattr(self.engine, "modeled_arrival_ts", None)
        if modeled is not None:
            return modeled(intent, decision_checkpoint)
        return self.engine.config.latency.arrival_ts(intent.decision_ts)

    def _pending_modeled_arrival_ts(self, item: PendingIntent) -> datetime:
        profile = getattr(item, "execution_profile", None) or getattr(
            item.row, "execution_profile", None
        )
        if profile is None:
            return self._modeled_arrival_ts(
                item.row.intent,
                getattr(item, "decision_checkpoint", None),
            )
        return TakerOnlyPaperExecutionEngine(
            profile.to_execution_config()
        ).modeled_arrival_ts(
            item.row.intent,
            getattr(item, "decision_checkpoint", None),
        )

    async def _persist_simulator_artifact_heartbeat(
        self,
        *,
        status: str,
        force: bool,
    ) -> None:
        store = getattr(self, "simulator_artifact_store", None)
        run_id = getattr(self, "simulator_run_id", None)
        if store is None or run_id is None:
            return
        now = time.monotonic()
        if (
            not force
            and now - self._last_simulator_artifact_heartbeat < self.health_seconds
        ):
            return
        try:
            await self._db_call(
                store.persist_shadow_run_heartbeat,
                run_id=run_id,
                heartbeat_ts_ns=_datetime_to_ns(_now()),
                status=status,
            )
            self._last_simulator_artifact_heartbeat = now
            self.stats.simulator_artifact_heartbeats += 1
            self.stats.last_simulator_artifact_error = None
        except Exception as exc:
            self.stats.simulator_artifact_failures += 1
            self.stats.last_simulator_artifact_error = _exception_text(exc)
            raise

    async def apply_degradation_signal(
        self,
        *,
        scope: DegradationScope,
        identifier: str,
        signal: str,
    ) -> None:
        previous = self.degradation_controller.state_for(scope, identifier)
        transition = self.degradation_controller.apply(
            scope=scope,
            identifier=identifier,
            signal=signal,
        )
        if transition.state == previous:
            return
        artifact_store = getattr(self, "simulator_artifact_store", None)
        run_id = getattr(self, "simulator_run_id", None)
        if artifact_store is not None and run_id is not None:
            transition_key = (
                f"{run_id}:degradation:{scope.value}:{identifier}:"
                f"{_datetime_to_ns(_now())}:{transition.signal}"
            )
            try:
                await self._db_call(
                    artifact_store.persist_degradation,
                    transition,
                    transition_key=transition_key,
                )
            except Exception:
                self.stats.degradation_persistence_failures += 1
                raise
        self.stats.degradation_transitions += 1
        self.stats.last_degradation_reason = (
            f"{scope.value.lower()}:{identifier}:{transition.state}"
        )

    async def _restore_degradation_states(self) -> None:
        artifact_store = getattr(self, "simulator_artifact_store", None)
        load = getattr(artifact_store, "load_latest_degradations", None)
        if load is None:
            return
        transitions = await self._db_call(load)
        for transition in transitions:
            self.degradation_controller.restore(transition)
        self.stats.degradation_states_restored = len(transitions)

    async def _sync_token_degradation(
        self,
        asset_id: str,
        checkpoint: ArrivalBookCheckpoint | None,
    ) -> None:
        connected = self._connected_sources()
        if not connected:
            signal = "ALL_FEEDS_LOST"
        elif (
            checkpoint is None
            or checkpoint.has_gap
            or checkpoint.book_status.upper()
            not in {"READY", "READY_HIGH", "READY_MEDIUM"}
        ):
            signal = "BOOK_GAP"
        elif len(connected) >= 2 and asset_id in self.redundant_ready_assets:
            signal = "REDUNDANCY_RESTORED"
        else:
            signal = "BOOK_REBUILT_SINGLE_FEED"
        expected_state = {
            "ALL_FEEDS_LOST": TokenDataState.DATA_UNSAFE.value,
            "BOOK_GAP": TokenDataState.DATA_UNSAFE.value,
            "REDUNDANCY_RESTORED": TokenDataState.REDUNDANT.value,
            "BOOK_REBUILT_SINGLE_FEED": TokenDataState.SINGLE_FEED_SAFE.value,
        }[signal]
        if (
            self.degradation_controller.state_for(
                DegradationScope.TOKEN,
                asset_id,
            )
            == expected_state
        ):
            return
        await self.apply_degradation_signal(
            scope=DegradationScope.TOKEN,
            identifier=asset_id,
            signal=signal,
        )

    async def _bind_venue_regime(self, intent_id: int) -> dict[str, Any] | None:
        bind = getattr(self.store, "bind_venue_regime", None)
        if bind is None:
            return None
        return await self._db_call(bind, int(intent_id), venue="POLYMARKET")

    async def _persist_execution_tca(
        self,
        intent_id: int,
        result: Any,
        *,
        decision_checkpoint: Any | None,
        arrival_checkpoint: Any | None,
        risk_decision: Any,
    ) -> None:
        persist = getattr(self.store, "persist_execution_tca", None)
        if persist is None:
            return
        try:
            await self._db_call(
                persist,
                intent_id,
                result,
                decision_checkpoint=decision_checkpoint,
                arrival_checkpoint=arrival_checkpoint,
                risk_decision=risk_decision,
            )
            self.stats.tca_records += 1
            self.stats.last_tca_error = None
        except Exception as exc:  # noqa: BLE001
            self.stats.tca_failures += 1
            self.stats.last_tca_error = _exception_text(exc)

    async def _observe_venue_admission(
        self,
        intent_id: int,
        intent: Any,
        submit_request_ts: datetime,
    ) -> Any | None:
        """Evaluate the modeled gateway; the caller decides shadow or authority mode."""

        if self.venue_admission_shadow is None:
            return
        try:
            venue_shadow = await self._db_call(
                self.venue_admission_shadow.evaluate,
                VenueAdmissionRequest(
                    intent_id=str(intent_id),
                    account_id=str(intent.strategy_id),
                    signer_id=f"paper:{intent.strategy_id}",
                    ip_id="paper-live-shadow",
                    created_ts_ns=_datetime_to_ns(submit_request_ts),
                    client_order_id=str(intent.client_order_id),
                    paper_admitted=True,
                    post_only=bool(intent.post_only),
                    side=str(intent.side),
                    time_in_force=str(intent.order_type),
                    asset_id=str(intent.asset_id),
                    limit_price=str(intent.limit_price),
                    size=str(intent.size),
                ),
            )
            self.stats.venue_shadow_evaluations += 1
            self.stats.venue_shadow_disagreements += int(not venue_shadow.agreement)
            self.stats.venue_shadow_queued += int(venue_shadow.gateway_queued)
            self.stats.last_venue_shadow_reason = venue_shadow.reason
            return venue_shadow
        except Exception as exc:  # noqa: BLE001
            # Authority mode interprets None as a fail-closed deferral.
            self.stats.venue_shadow_failures += 1
            self.stats.last_venue_shadow_reason = _exception_text(exc)
            return None

    async def _observe_own_order_oms(
        self,
        intent_id: int,
        intent: Any,
    ) -> Any | None:
        if self.own_order_oms_gate is None:
            return None
        try:
            decision = await self._db_call(
                self.own_order_oms_gate.evaluate,
                intent_id,
                intent,
            )
            self.stats.own_order_oms_evaluations += 1
            self.stats.own_order_oms_rejections += int(
                decision.admission.status is OmsAdmissionStatus.REJECTED_SELF_TRADE
            )
            self.stats.own_order_oms_deferred += int(
                decision.admission.status is OmsAdmissionStatus.DEFERRED_CANCEL_PENDING
            )
            self.stats.last_own_order_oms_reason = decision.admission.reason
            return decision
        except Exception as exc:  # noqa: BLE001
            self.stats.own_order_oms_failures += 1
            self.stats.last_own_order_oms_reason = _exception_text(exc)
            return None

    async def _finalize_own_order_oms(
        self,
        intent_id: int,
        result: Any,
        *,
        oms_decision: Any | None,
    ) -> None:
        if (
            self.own_order_oms_gate is None
            or oms_decision is None
            or not oms_decision.accepted
        ):
            return
        try:
            await self._db_call(
                self.own_order_oms_gate.finalize,
                intent_id,
                result,
            )
            self.stats.own_order_oms_finalizations += 1
        except Exception as exc:  # noqa: BLE001
            self.stats.own_order_oms_failures += 1
            self.stats.last_own_order_oms_reason = _exception_text(exc)

    async def _observe_fill_finality(self, intent_id: int, result: Any) -> None:
        if self.fill_finality_shadow is None or not result.fills:
            return
        try:
            trades = await self._db_call(
                self.fill_finality_shadow.record_result,
                result,
                paper_intent_id=int(intent_id),
            )
            self.stats.fill_finality_shadow_matches += len(trades)
            self.stats.last_fill_finality_trade_ids = [
                trade.fragment.trade_id for trade in trades
            ]
            self.stats.last_fill_finality_error = None
        except Exception as exc:
            self.stats.fill_finality_shadow_failures += 1
            self.stats.last_fill_finality_error = _exception_text(exc)
            if self.fill_finality_auto_reconcile:
                raise

    async def _confirm_fill_finality(self, result: Any) -> bool:
        if (
            not getattr(self, "fill_finality_auto_reconcile", False)
            or getattr(self, "fill_finality_shadow", None) is None
            or not getattr(result, "fills", ())
        ):
            return False
        try:
            trades = await self._db_call(
                self.fill_finality_shadow.reconcile_result,
                result,
                outcome="CONFIRMED",
            )
            self.stats.fill_finality_confirmed += len(trades)
            self.stats.last_fill_finality_error = None
            return bool(trades)
        except Exception as exc:  # noqa: BLE001
            self.stats.fill_finality_shadow_failures += 1
            self.stats.last_fill_finality_error = _exception_text(exc)
            return False

    async def _advance_fill_finality(self, *, force: bool) -> None:
        if not self.fill_finality_auto_reconcile or self.fill_finality_shadow is None:
            return
        now = time.monotonic()
        if (
            not force
            and now - self._last_fill_finality_reconcile
            < self.fill_finality_reconcile_seconds
        ):
            return
        self._last_fill_finality_reconcile = now
        try:
            reconciled = await self._db_call(
                self.fill_finality_shadow.reconcile_pending_from_lifecycle,
                limit=1000,
            )
            self.stats.fill_finality_lifecycle_reconciled += int(reconciled)
            self.stats.last_fill_finality_error = None
            if reconciled:
                await self._causal_lifecycle_event(
                    "FILL_FINALITY",
                    payload={"lifecycle_reconciled": int(reconciled)},
                )
        except Exception as exc:  # noqa: BLE001
            self.stats.fill_finality_shadow_failures += 1
            self.stats.last_fill_finality_error = _exception_text(exc)

    async def _complete_intent(
        self,
        intent_id: int,
        result: Any,
        *,
        finalize_oms: bool = False,
    ) -> None:
        """Persist the order result before applying its idempotent side effects."""

        await self._db_call(self.store.complete, intent_id, result)
        try:
            await self._apply_committed_result_side_effects(
                intent_id,
                result,
                finalize_oms=finalize_oms,
            )
        except Exception as exc:  # noqa: BLE001
            # The order result is already authoritative. Keep it durable and
            # let the unapplied-result reconciler retry the idempotent effects;
            # changing the order to FAILED here would contradict the fill.
            self.stats.accounting_reconciliation_failures += 1
            self.stats.last_accounting_reconciliation_error = _exception_text(exc)

    async def _apply_result_accounting(self, intent_id: int, result: Any) -> None:
        intent = getattr(result, "intent", None)
        asset_id = getattr(intent, "asset_id", None)
        status = str(getattr(result, "status", "UNKNOWN"))
        fills = tuple(getattr(result, "fills", ()) or ())
        await self._causal_lifecycle_event(
            "PAPER_MATCH" if fills else _paper_terminal_event_type(status),
            intent_id=intent_id,
            asset_id=asset_id,
            event_ts=getattr(result, "arrival_ts", None),
            payload={
                "status": status,
                "fill_count": len(fills),
                "filled_size": str(getattr(result, "filled_size", 0)),
            },
        )
        portfolio_store = getattr(self, "portfolio_store", None)
        if portfolio_store is not None:
            await self._db_call(portfolio_store.append, result)
        await self._causal_lifecycle_event(
            "ACCOUNTING_MARK",
            intent_id=intent_id,
            asset_id=asset_id,
            payload={
                "status": status,
                "ledger_applied": portfolio_store is not None,
                "audit_key": getattr(result, "audit_key", None),
            },
        )

    async def _apply_committed_result_side_effects(
        self,
        intent_id: int,
        result: PaperExecutionResult,
        *,
        finalize_oms: bool = True,
    ) -> None:
        """Apply retryable effects after the execution result is durable.

        Portfolio accounting is deliberately the final critical write. Its
        applied-result row is the recovery marker used by
        ``load_unapplied_results``.
        """

        portfolio_store = getattr(self, "portfolio_store", None)
        if portfolio_store is not None:
            await self._db_call(
                portfolio_store.finalize_order_reservation,
                intent_id,
                result,
                self.engine.config,
            )
        own_order_oms_gate = getattr(self, "own_order_oms_gate", None)
        if finalize_oms and own_order_oms_gate is not None:
            await self._db_call(
                own_order_oms_gate.finalize,
                intent_id,
                result,
            )
            self.stats.own_order_oms_finalizations += 1
        if getattr(self, "fill_finality_shadow", None) is not None:
            await self._observe_fill_finality(intent_id, result)
        await self._apply_result_accounting(intent_id, result)
        finality_confirmed = await self._confirm_fill_finality(result)
        if finality_confirmed:
            await self._causal_lifecycle_event(
                "FILL_FINALITY",
                intent_id=intent_id,
                asset_id=result.intent.asset_id,
                payload={"outcome": "CONFIRMED"},
            )
        await self._observe_completed_result(intent_id, result)

    async def _reconcile_unapplied_accounting(self, *, force: bool) -> None:
        if self.portfolio_store is None:
            return
        loader = getattr(self.store, "load_unapplied_results", None)
        if loader is None:
            return
        now = time.monotonic()
        if not force and now - self._last_accounting_reconcile < 1.0:
            return
        self._last_accounting_reconcile = now
        try:
            rows = await self._db_call(loader, limit=100)
        except Exception as exc:  # noqa: BLE001
            self.stats.accounting_reconciliation_failures += 1
            self.stats.last_accounting_reconciliation_error = _exception_text(exc)
            return
        for row in rows:
            try:
                result = _execution_result_from_payload(dict(row["result"]))
                await self._apply_committed_result_side_effects(
                    int(row["intent_id"]),
                    result,
                )
                self.stats.accounting_reconciliations += 1
                self.stats.last_accounting_reconciliation_error = None
            except Exception as exc:  # noqa: BLE001
                self.stats.accounting_reconciliation_failures += 1
                self.stats.last_accounting_reconciliation_error = _exception_text(exc)
        reconcile_reservations = getattr(
            self.portfolio_store,
            "reconcile_terminal_reservations",
            None,
        )
        if reconcile_reservations is not None:
            try:
                await self._db_call(reconcile_reservations)
            except Exception as exc:  # noqa: BLE001
                self.stats.accounting_reconciliation_failures += 1
                self.stats.last_accounting_reconciliation_error = _exception_text(exc)

    async def _observe_completed_result(self, intent_id: int, result: Any) -> None:
        record_batch = getattr(self.store, "record_batch_execution", None)
        if record_batch is not None:
            try:
                changed = await self._db_call(record_batch, intent_id, result)
                self.stats.batch_evidence_updates += int(bool(changed))
                self.stats.last_batch_evidence_error = None
            except Exception as exc:  # noqa: BLE001
                self.stats.batch_evidence_failures += 1
                self.stats.last_batch_evidence_error = _exception_text(exc)
        await self._observe_lifecycle_scheduler(result.audit_key)

    def _queue_maker_research_book_event(
        self,
        event: NormalizedBookEvent,
        *,
        signature: str,
        book: LocalOrderBook,
        prior_displayed_size: Decimal | None,
    ) -> None:
        tracked = getattr(self, "_maker_research_levels", set())
        if not tracked:
            return
        if isinstance(event, NormalizedBookSnapshot):
            if not any(asset_id == event.token_id for asset_id, _, _ in tracked):
                return
            research_event = MakerResearchBookEvent(
                event_id=signature,
                event_kind="BOOK_SNAPSHOT",
                asset_id=event.token_id,
                event_ts=datetime.fromtimestamp(
                    event.event_ts_ms / 1000, tz=timezone.utc
                ),
                book_generation=book.generation,
                bids=event.bids,
                asks=event.asks,
            )
        else:
            maker_side = "BUY" if event.side == "bid" else "SELL"
            if (event.token_id, maker_side, event.price) not in tracked:
                return
            displayed_size = max(Decimal("0"), event.size)
            if prior_displayed_size is None or prior_displayed_size <= displayed_size:
                return
            research_event = MakerResearchBookEvent(
                event_id=signature,
                event_kind="BOOK_DELTA",
                asset_id=event.token_id,
                event_ts=datetime.fromtimestamp(
                    event.event_ts_ms / 1000, tz=timezone.utc
                ),
                book_generation=book.generation,
                side=maker_side,
                price=event.price,
                previous_size=prior_displayed_size,
                displayed_size=displayed_size,
            )
        queue = getattr(self, "_maker_research_book_events", None)
        if queue is None:
            return
        limit = int(getattr(self, "_maker_research_book_event_limit", 50_000))
        if len(queue) >= limit:
            self.stats.maker_research_event_drops += 1
            self.stats.maker_research_degraded = True
            self.stats.last_maker_research_error = "research_book_event_backlog_full"
            return
        queue.append(research_event)

    async def _advance_maker_research_book_events(self) -> None:
        queue = getattr(self, "_maker_research_book_events", None)
        apply_events = getattr(self.store, "apply_maker_research_book_events", None)
        if not queue or apply_events is None:
            return
        batch = tuple(queue[index] for index in range(min(500, len(queue))))
        try:
            counts = await self._db_call(apply_events, batch)
        except Exception as exc:  # noqa: BLE001
            self.stats.maker_research_degraded = True
            self.stats.last_maker_research_error = _exception_text(exc)
            return
        for _ in batch:
            queue.popleft()
        self.stats.maker_research_book_events += int(counts.get("events") or 0)
        self.stats.maker_research_state_updates += int(counts.get("states") or 0)
        self.stats.maker_research_rebases += int(counts.get("rebases") or 0)
        self.stats.maker_research_duplicate_events += int(counts.get("duplicates") or 0)
        self.stats.last_maker_research_error = None

    async def _advance_maker_trades(self) -> None:
        now = time.monotonic()
        prune_trade_events = getattr(self.store, "prune_maker_trade_events", None)
        if (
            prune_trade_events is not None
            and now - getattr(self, "_last_maker_trade_prune", 0.0) >= 3600
        ):
            try:
                self.stats.maker_trade_events_pruned += await self._db_call(
                    prune_trade_events,
                    before=_now() - timedelta(days=7),
                    limit=10_000,
                )
                self._last_maker_trade_prune = now
            except Exception as exc:  # noqa: BLE001
                self.stats.last_maker_error = (
                    f"maker_trade_prune:{_exception_text(exc)}"
                )
        for _ in range(min(100, len(self._maker_trades))):
            trade = self._maker_trades[0]
            event_id = _trade_event_signature(trade)
            event_ts = datetime.fromtimestamp(
                trade.event_ts_ms / 1000,
                tz=timezone.utc,
            )
            try:
                persist_trade = getattr(self.store, "persist_maker_trade_event", None)
                if persist_trade is not None:
                    inserted = await self._db_call(
                        persist_trade,
                        event_id=event_id,
                        worker_id=self.stats.worker_id,
                        asset_id=trade.token_id,
                        price=trade.price,
                        size=trade.size,
                        aggressor_side=trade.aggressor_side,
                        event_ts=event_ts,
                        transaction_hash=trade.transaction_hash,
                    )
                    if inserted:
                        self.stats.maker_trade_events_persisted += 1
                    else:
                        self.stats.maker_trade_event_duplicates += 1
                        load_trade_state = getattr(
                            self.store, "maker_trade_event_state", None
                        )
                        if load_trade_state is not None:
                            processing_state = await self._db_call(
                                load_trade_state, event_id
                            )
                            if processing_state in {"APPLIED", "SKIPPED_UNSAFE"}:
                                self._maker_trades.popleft()
                                continue
                checkpoint = None
                displayed_size_at_price = None
                history = getattr(self, "history", None)
                if history is not None:
                    checkpoint = _checkpoint_at(history.get(trade.token_id), event_ts)
                    checkpoint_safe = bool(
                        checkpoint is not None
                        and checkpoint.coverage_grade in {"A_PLUS", "A", "B"}
                        and not checkpoint.has_gap
                        and checkpoint.book_status.upper()
                        in {"READY", "READY_HIGH", "READY_MEDIUM"}
                    )
                    if not checkpoint_safe:
                        mark_processed = getattr(
                            self.store, "mark_maker_trade_event_processed", None
                        )
                        if mark_processed is not None:
                            await self._db_call(
                                mark_processed,
                                event_id,
                                state="SKIPPED_UNSAFE",
                                reason="no_safe_book_baseline_at_trade_time",
                            )
                        self.stats.maker_trade_unsafe_skips += 1
                        await self._causal_lifecycle_event(
                            "MAKER_EVIDENCE_SKIPPED",
                            asset_id=trade.token_id,
                            event_ts=event_ts,
                            payload={
                                "event_id": event_id,
                                "reason": "no_safe_book_baseline_at_trade_time",
                            },
                        )
                        self._maker_trades.popleft()
                        continue
                    assert checkpoint is not None
                    passive_side = (
                        "SELL" if trade.aggressor_side.upper() == "BUY" else "BUY"
                    )
                    levels = (
                        checkpoint.asks if passive_side == "SELL" else checkpoint.bids
                    )
                    displayed_size_at_price = sum(
                        (level.size for level in levels if level.price == trade.price),
                        Decimal(0),
                    )
                research_trade = getattr(self.store, "apply_maker_research_trade", None)
                if research_trade is not None:
                    try:
                        research_counts = await self._db_call(
                            research_trade,
                            asset_id=trade.token_id,
                            price=trade.price,
                            size=trade.size,
                            aggressor_side=trade.aggressor_side,
                            event_id=event_id,
                            event_ts=event_ts,
                            current_book_generation=(
                                checkpoint.generation
                                if checkpoint is not None
                                else None
                            ),
                            displayed_size_at_price=displayed_size_at_price,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.stats.maker_research_degraded = True
                        self.stats.last_maker_research_error = _exception_text(exc)
                    else:
                        self.stats.maker_research_state_updates += int(
                            research_counts.get("states") or 0
                        )
                        self.stats.maker_research_rebases += int(
                            research_counts.get("rebases") or 0
                        )
                        self.stats.maker_research_duplicate_events += int(
                            research_counts.get("duplicates") or 0
                        )
                        self.stats.maker_research_hypothetical_fills += int(
                            research_counts.get("fills") or 0
                        )
                plans = await self._db_call(
                    self.store.plan_maker_trade,
                    asset_id=trade.token_id,
                    price=trade.price,
                    size=trade.size,
                    aggressor_side=trade.aggressor_side,
                    event_id=event_id,
                    event_ts=event_ts,
                    current_book_generation=(
                        checkpoint.generation if checkpoint is not None else None
                    ),
                    current_checkpoint_id=(
                        checkpoint.checkpoint_id if checkpoint is not None else None
                    ),
                    current_checkpoint_observed_at=(
                        checkpoint.observed_at if checkpoint is not None else None
                    ),
                    displayed_size_at_price=displayed_size_at_price,
                )
                for plan in plans:
                    result = (
                        _maker_execution_result(
                            plan,
                            price=trade.price,
                            config=self.engine.config,
                        )
                        if plan.incremental_fill_size > 0
                        else None
                    )
                    committed = await self._db_call(
                        self.store.commit_maker_trade,
                        plan,
                        result,
                    )
                    if not committed:
                        continue
                    self.stats.maker_queue_advances += 1
                    self.stats.maker_queue_rebases += int(plan.rebased)
                    if result is None:
                        continue
                    await self._apply_committed_result_side_effects(
                        plan.intent_id,
                        result,
                    )
                    self.stats.maker_fills += 1
                    self.stats.maker_filled_size = str(
                        Decimal(self.stats.maker_filled_size) + result.filled_size
                    )
                mark_processed = getattr(
                    self.store, "mark_maker_trade_event_processed", None
                )
                if mark_processed is not None:
                    await self._db_call(
                        mark_processed,
                        event_id,
                        state="APPLIED",
                        reason="maker_queue_plans_committed",
                    )
                self._maker_trades.popleft()
                self.stats.last_maker_error = None
            except Exception as exc:  # noqa: BLE001
                self.stats.maker_errors += 1
                self.stats.last_maker_error = _exception_text(exc)
                return

    async def _observe_lifecycle_scheduler(self, audit_key: str) -> None:
        if self.lifecycle_scheduler_shadow is None:
            return
        try:
            result = await self._db_call(
                self.lifecycle_scheduler_shadow.observe,
                str(audit_key),
            )
            self.stats.lifecycle_scheduler_evaluations += 1
            self.stats.lifecycle_scheduler_disagreements += int(not result.agreement)
            self.stats.last_lifecycle_scheduler_audit_key = str(audit_key)
            self.stats.last_lifecycle_scheduler_error = None
        except Exception as exc:  # noqa: BLE001
            # Scheduler shadow evidence never changes the paper result.
            self.stats.lifecycle_scheduler_failures += 1
            self.stats.last_lifecycle_scheduler_error = _exception_text(exc)

    async def _record_batch_admission(
        self,
        intent_id: int,
        *,
        paper_admitted: bool,
        venue_shadow: Any | None,
    ) -> None:
        record = getattr(self.store, "record_batch_admission", None)
        if record is None:
            return
        try:
            changed = await self._db_call(
                record,
                intent_id,
                paper_admitted=paper_admitted,
                venue_shadow=venue_shadow,
            )
            self.stats.batch_evidence_updates += int(bool(changed))
            self.stats.last_batch_evidence_error = None
        except Exception as exc:  # noqa: BLE001
            self.stats.batch_evidence_failures += 1
            self.stats.last_batch_evidence_error = _exception_text(exc)

    async def _record_batch_failure(
        self,
        intent_id: int,
        error: Exception,
    ) -> None:
        record = getattr(self.store, "record_batch_failure", None)
        if record is None:
            return
        try:
            changed = await self._db_call(
                record,
                intent_id,
                _exception_text(error),
            )
            self.stats.batch_evidence_updates += int(bool(changed))
        except Exception as exc:  # noqa: BLE001
            self.stats.batch_evidence_failures += 1
            self.stats.last_batch_evidence_error = _exception_text(exc)

    async def _advance_venue_shadow(self) -> None:
        if self.venue_admission_shadow is None:
            return
        try:
            pulse = await self._db_call(
                self.venue_admission_shadow.pulse,
                now_ts_ns=_datetime_to_ns(_now()),
            )
            self.stats.venue_shadow_heartbeat_auto_cancels += (
                pulse.heartbeat_auto_cancels
            )
            self.stats.venue_shadow_reservation_releases += (
                pulse.reservation_release_events
            )
        except Exception as exc:  # noqa: BLE001
            self.stats.venue_shadow_failures += 1
            self.stats.last_venue_shadow_reason = _exception_text(exc)

    async def _reconcile_venue_shadow_restart(self) -> None:
        if self.venue_admission_shadow is None:
            return
        try:
            released = await self._db_call(
                self.venue_admission_shadow.reconcile_stale_reservations
            )
            self.stats.venue_shadow_restart_releases += int(released)
            self.stats.venue_shadow_reservation_releases += int(released)
        except Exception as exc:  # noqa: BLE001
            self.stats.venue_shadow_failures += 1
            self.stats.last_venue_shadow_reason = _exception_text(exc)

    async def _advance_cancellations(self) -> None:
        if not hasattr(self.store, "apply_due_cancels"):
            return
        canceled = await self._db_call(self.store.apply_due_cancels, limit=100)
        for intent_id in canceled:
            self.pending.pop(intent_id, None)
            await self._causal_lifecycle_event(
                "PAPER_CANCEL",
                intent_id=intent_id,
                payload={"reason": "cancel_ack"},
            )
            await self._db_call(
                self.store.close_maker_queue,
                intent_id,
                state="CANCELED",
                reason="cancel_ack",
            )
            if self.own_order_oms_gate is not None:
                await self._db_call(
                    self.own_order_oms_gate.store.finalize_intent,
                    intent_id,
                    execution_status="CANCELED",
                    event_id=f"paper-oms-cancel-ack:{intent_id}",
                )
            if self.portfolio_store is not None:
                await self._db_call(
                    self.portfolio_store.release_order_reservation,
                    intent_id,
                    reason="cancel_ack",
                )

    async def apply_market_clarification(
        self,
        *,
        condition_id: str,
        source_event_id: str,
        clarified_at: datetime,
        payload_hash: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Clear books and outstanding orders for one venue clarification."""

        apply = getattr(self.store, "apply_market_clarification", None)
        if apply is None:
            raise RuntimeError("paper store does not support market clarification")
        result = await self._db_call(
            apply,
            condition_id=condition_id,
            source_event_id=source_event_id,
            clarified_at=clarified_at,
            payload_hash=payload_hash,
            payload=payload,
        )
        asset_ids = [str(asset_id) for asset_id in result.get("asset_ids") or ()]
        reset_by_source: dict[str, list[str]] = {}
        for source, service in self.route_services.items():
            reset_by_source[source] = service.reset_tokens(
                asset_ids,
                reason="market_clarification",
            )

        for intent_id_value in result.get("canceled_intent_ids") or ():
            intent_id = int(intent_id_value)
            self.pending.pop(intent_id, None)
            if self.portfolio_store is not None:
                await self._db_call(
                    self.portfolio_store.release_order_reservation,
                    intent_id,
                    reason="market_clarification",
                )
            close_queue = getattr(self.store, "close_maker_queue", None)
            if close_queue is not None:
                await self._db_call(
                    close_queue,
                    intent_id,
                    state="CANCELED",
                    reason="market_clarification",
                )
            if self.own_order_oms_gate is not None:
                await self._db_call(
                    self.own_order_oms_gate.store.finalize_intent,
                    intent_id,
                    execution_status="CANCELED",
                    event_id=f"paper-oms-market-clarification:{intent_id}",
                )
        await self._causal_lifecycle_event(
            "MARKET_CLARIFICATION",
            payload={
                "condition_id": condition_id,
                "source_event_id": source_event_id,
                "payload_hash": payload_hash,
                "asset_ids": asset_ids,
                "canceled_intent_ids": result.get("canceled_intent_ids") or [],
                "reset_by_source": reset_by_source,
            },
            event_ts=clarified_at,
        )
        return {**result, "reset_by_source": reset_by_source}

    async def _advance_market_clarifications(self) -> None:
        claim = getattr(self.store, "claim_market_clarifications", None)
        if claim is None:
            return
        now = time.monotonic()
        if now - self._last_market_clarification_poll < 1.0:
            return
        self._last_market_clarification_poll = now
        commands = await self._db_call(
            claim,
            worker_id=self.worker_id,
            limit=10,
        )
        self.stats.market_clarification_commands += len(commands)
        for command in commands:
            command_id = int(command["command_id"])
            try:
                await self.apply_market_clarification(
                    condition_id=str(command["condition_id"]),
                    source_event_id=str(command["source_event_id"]),
                    clarified_at=command["clarified_at"],
                    payload_hash=str(command["payload_hash"]),
                    payload=dict(command.get("payload") or {}),
                )
                completed = await self._db_call(
                    self.store.complete_market_clarification_command,
                    command_id,
                    worker_id=self.worker_id,
                )
                if not completed:
                    raise RuntimeError(
                        "clarification command ownership changed before completion"
                    )
                self.stats.market_clarifications_applied += 1
                self.stats.last_market_clarification_error = None
            except Exception as exc:  # noqa: BLE001
                self.stats.market_clarification_failures += 1
                self.stats.last_market_clarification_error = _exception_text(exc)
                fail = getattr(self.store, "fail_market_clarification_command", None)
                if fail is not None:
                    await self._db_call(
                        fail,
                        command_id,
                        worker_id=self.worker_id,
                        error=_exception_text(exc),
                    )

    async def _advance_replacements(self) -> None:
        if not hasattr(self.store, "apply_due_replacements"):
            return
        replacements = await self._db_call(
            self.store.apply_due_replacements,
            limit=100,
        )
        for row in replacements:
            old_intent_id = int(row["old_intent_id"])
            self.pending.pop(old_intent_id, None)
            await self._causal_lifecycle_event(
                "PAPER_CANCEL",
                intent_id=old_intent_id,
                payload={
                    "reason": "replace_arrived",
                    "new_intent_id": row.get("new_intent_id"),
                },
            )
            await self._db_call(
                self.store.close_maker_queue,
                old_intent_id,
                state="REPLACED",
                reason="replace_arrived",
            )
            if self.own_order_oms_gate is not None:
                await self._db_call(
                    self.own_order_oms_gate.store.finalize_intent,
                    old_intent_id,
                    execution_status="CANCELED",
                    event_id=f"paper-oms-replaced:{old_intent_id}",
                )
            if self.portfolio_store is not None:
                await self._db_call(
                    self.portfolio_store.release_order_reservation,
                    old_intent_id,
                    reason="replace_arrived",
                )

    def _advance_nav_snapshots(self) -> None:
        if self.portfolio_store is None:
            return
        if self._nav_snapshot_task is not None:
            if not self._nav_snapshot_task.done():
                return
            task = self._nav_snapshot_task
            self._nav_snapshot_task = None
            try:
                rows = task.result()
            except Exception as exc:  # noqa: BLE001
                self.stats.nav_failures += 1
                self.stats.last_error = f"nav_snapshot: {_exception_text(exc)}"
            else:
                self.stats.nav_snapshots += 1
                self.stats.nav_strategies = len(rows)
                self.stats.nav_unmarkable_positions = sum(
                    int(row.get("unmarkable_positions") or 0) for row in rows
                )
                self.stats.last_nav_at = _now().isoformat()
                if str(self.stats.last_error or "").startswith("nav_snapshot:"):
                    self.stats.last_error = None

        now_monotonic = time.monotonic()
        if now_monotonic - self._last_nav_snapshot < self.nav_snapshot_seconds:
            return
        self._last_nav_snapshot = now_monotonic
        observed_at = _now()
        marks: dict[str, PaperAssetMark] = {}
        for asset_id, history in self.history.items():
            if not history:
                continue
            checkpoint = history[-1]
            best_bid = max((level.price for level in checkpoint.bids), default=None)
            best_ask = min((level.price for level in checkpoint.asks), default=None)
            mark = build_marks(
                MarkInput(
                    as_of=observed_at,
                    source_event_at=checkpoint.observed_at,
                    side="LONG",
                    best_bid=best_bid,
                    best_ask=best_ask,
                    stale_after_ms=self.engine.config.max_book_age_ms,
                )
            )
            marks[asset_id] = PaperAssetMark(
                asset_id=asset_id,
                observed_at=observed_at,
                result=mark,
                best_bid=best_bid,
                best_ask=best_ask,
                checkpoint_id=checkpoint.checkpoint_id,
                exit_levels=tuple(
                    ExitLevel(level.price, level.size) for level in checkpoint.bids
                ),
            )
        self._nav_snapshot_task = asyncio.create_task(
            self._db_call(
                self.portfolio_store.record_nav_snapshots,
                marks,
                observed_at=observed_at,
                history_interval_seconds=self.nav_history_seconds,
            ),
            name="paper-live-nav-snapshot",
        )

    async def _expire_working_orders(self) -> None:
        if not hasattr(self.store, "expire_working"):
            return
        now = time.monotonic()
        if now - self._last_order_expiry < 1.0:
            return
        self._last_order_expiry = now
        rows = await self._db_call(self.store.expire_working)
        for row in rows:
            intent_id = int(row["intent_id"])
            await self._causal_lifecycle_event(
                "PAPER_EXPIRE",
                intent_id=intent_id,
                payload={
                    "state": str(row["order_state"]),
                    "reason": str(row.get("last_error") or "working_order_closed"),
                },
            )
            await self._db_call(
                self.store.close_maker_queue,
                intent_id,
                state=str(row["order_state"]),
                reason=str(row.get("last_error") or "working_order_closed"),
            )
            if self.own_order_oms_gate is not None:
                await self._db_call(
                    self.own_order_oms_gate.store.finalize_intent,
                    intent_id,
                    execution_status=str(row["order_state"]),
                    event_id=f"paper-oms-expired:{intent_id}:{row['order_state']}",
                )
            if self.portfolio_store is not None:
                await self._db_call(
                    self.portfolio_store.release_order_reservation,
                    intent_id,
                    reason=str(row.get("last_error") or "working_order_closed"),
                )

    async def _settle_resolved_positions(self, *, force: bool) -> None:
        if self.portfolio_store is None:
            return
        if self._settlement_task is not None:
            if not self._settlement_task.done():
                return
            task = self._settlement_task
            self._settlement_task = None
            try:
                changed = task.result()
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"settlement_poll: {_exception_text(exc)}"
            else:
                if str(self.stats.last_error or "").startswith("settlement_poll:"):
                    self.stats.last_error = None
                self.stats.settled_positions += changed
                if changed:
                    await self._causal_lifecycle_event(
                        "POSITION_OPERATION_CONFIRMATION",
                        payload={"settled_positions": int(changed)},
                    )
        now = time.monotonic()
        if not force and now - self._last_settlement < self.settlement_poll_seconds:
            return
        self._last_settlement = now
        self._settlement_task = asyncio.create_task(
            self._db_call(
                self.portfolio_store.settle_resolved_positions,
                limit=100,
                require_redeemed=True,
            ),
            name="paper-live-settlement-poll",
        )

    async def _db_call(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        operation = getattr(
            func,
            "__qualname__",
            getattr(func, "__name__", type(func).__name__),
        )
        started = time.monotonic()
        executor = (
            getattr(self, "_intent_db_executor", None)
            if _INTENT_DB_AFFINITY.get()
            else None
        )
        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    executor,
                    partial(func, *args, **kwargs),
                ),
                timeout=self.db_operation_timeout_seconds,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                "db_operation_timeout:"
                f"{operation}:{self.db_operation_timeout_seconds:.3f}s"
            ) from exc
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            stats = getattr(self, "stats", None)
            if stats is not None:
                self._record_db_operation_stats(str(operation), elapsed_ms)

    async def _claim_db_call(
        self,
        func: Any,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        operation = getattr(
            func,
            "__qualname__",
            getattr(func, "__name__", type(func).__name__),
        )
        started = time.monotonic()
        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    self._claim_db_executor,
                    partial(func, *args, **kwargs),
                ),
                timeout=self.db_operation_timeout_seconds,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                "db_operation_timeout:"
                f"{operation}:{self.db_operation_timeout_seconds:.3f}s"
            ) from exc
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            self._record_db_operation_stats(str(operation), elapsed_ms)

    def _record_db_operation_stats(self, operation: str, elapsed_ms: float) -> None:
        self.stats.db_operation_last = operation
        self.stats.db_operation_last_ms = elapsed_ms
        self.stats.db_operation_max_ms = max(
            self.stats.db_operation_max_ms,
            elapsed_ms,
        )
        self.stats.db_operation_counts[operation] = (
            self.stats.db_operation_counts.get(operation, 0) + 1
        )
        self.stats.db_operation_total_ms[operation] = (
            self.stats.db_operation_total_ms.get(operation, 0.0) + elapsed_ms
        )
        self.stats.db_operation_max_ms_by_name[operation] = max(
            self.stats.db_operation_max_ms_by_name.get(operation, 0.0),
            elapsed_ms,
        )
        if elapsed_ms >= 1000:
            self.stats.db_operation_slow_count += 1

    async def _intent_db_call(
        self,
        func: Any,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        token = _INTENT_DB_AFFINITY.set(True)
        try:
            return await self._db_call(func, *args, **kwargs)
        finally:
            _INTENT_DB_AFFINITY.reset(token)

    async def _prewarm_intent_db(self) -> None:
        counts = getattr(self.store, "counts", None)
        if counts is not None:
            await self._intent_db_call(counts)

    def _sync_authority_stats(self) -> None:
        controller = self.authority_controller
        if controller is None:
            self.stats.authority_mode = "UNFENCED"
            self.stats.authority_state = "NOT_CONFIGURED"
            return
        self.stats.authority_mode = "FENCED"
        token = controller.token
        if token is None:
            if self.stats.authority_state not in {"BLOCKED", "LOST", "RELEASED"}:
                self.stats.authority_state = "NOT_HELD"
            return
        self.stats.authority_state = "HELD"
        self.stats.authority_partition_key = token.partition_key
        self.stats.authority_owner_instance_id = token.owner_instance_id
        self.stats.authority_lease_epoch = token.lease_epoch
        self.stats.authority_lease_until = token.lease_until.isoformat()
        self.stats.authority_heartbeat_at = token.heartbeat_at.isoformat()
        self.stats.authority_fencing_enforced = token.fencing_enforced

    async def _authority_heartbeat_loop(self) -> None:
        controller = self.authority_controller
        if controller is None:
            return
        while True:
            await asyncio.sleep(controller.heartbeat_seconds)
            try:
                await self._db_call(controller.heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                controller.mark_lost()
                self.stats.authority_state = "LOST"
                self.stats.last_error = f"authority_heartbeat: {_exception_text(exc)}"
                self._sync_authority_stats()
                self.stats.updated_at = _now().isoformat()
                _write_json(self.status_path, self.stats.as_dict())
                return
            self._sync_authority_stats()

    async def _shutdown_authority(self) -> None:
        task = self._authority_heartbeat_loop_task
        self._authority_heartbeat_loop_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        controller = self.authority_controller
        if controller is None:
            return
        if controller.held:
            try:
                await self._db_call(controller.release)
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"authority_release: {_exception_text(exc)}"
                self.stats.authority_state = "RELEASE_FAILED"
                return
            self.stats.authority_state = "RELEASED"
        self._sync_authority_stats()

    async def _load_risk_context(self, intent: Any) -> PaperRiskContext:
        for attempt in range(3):
            try:
                return await self._db_call(self.portfolio_store.risk_context, intent)
            except Exception as exc:
                if attempt == 2 or "statement timeout" not in str(exc).lower():
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
        raise RuntimeError("risk context retry loop exhausted")

    async def _observe_unified_admission(
        self,
        intent_id: int,
        intent: OrderIntent,
    ) -> Any:
        service = self.unified_admission_service
        if service is None:
            raise RuntimeError("unified admission service is not configured")
        exposure_before = None
        exposure_after = None
        if self.portfolio_store is not None:
            portfolio = await self._db_call(
                self.portfolio_store.portfolio_snapshot,
                intent.strategy_id,
                intent.asset_id,
            )
            exposure_before = portfolio.position_size
            requested_shares = (
                intent.size
                if str(intent.amount_unit).upper() == "SHARES"
                else intent.size / intent.limit_price
                if intent.limit_price > 0
                else Decimal(0)
            )
            exposure_after = (
                exposure_before + requested_shares
                if intent.side == "BUY"
                else exposure_before - requested_shares
            )
        return await asyncio.to_thread(
            service.decide,
            AdmissionRequest(
                request_id=f"paper-worker-order:{int(intent_id)}",
                operation=AdmissionOperation.ORDER,
                account_id=self.degradation_account_id,
                strategy_id=intent.strategy_id,
                asset_id=intent.asset_id,
                condition_id=intent.condition_id,
                market_id=intent.market_id,
                exposure_effect=order_exposure_effect(intent.side),
                exposure_before=exposure_before,
                exposure_after=exposure_after,
                observed_at=_now(),
                metadata={
                    "source": "paper_live_worker",
                    "post_only": bool(intent.post_only),
                    "time_in_force": intent.order_type,
                },
            ),
        )

    def _connected_sources(self) -> set[str]:
        return {
            source
            for source, state in self.stats.route_states.items()
            if state == "CONNECTED"
        }

    def _update_transport_state(self) -> None:
        connected = len(self._connected_sources())
        route_count = len(getattr(self, "route_services", self.clients))
        if connected == route_count:
            independently_routed = has_independent_connected_routes(
                self.stats.route_states,
                self.stats.route_proxy_urls,
            )
            self.stats.transport_state = (
                "REDUNDANT"
                if connected > 1 and independently_routed
                else "CONNECTED"
                if connected == 1
                else "DEGRADED"
            )
            self.stats.last_error = None
        elif connected:
            self.stats.transport_state = "DEGRADED"
        else:
            self.stats.transport_state = "RECONNECTING"

    async def _write_health(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_health < self.health_seconds:
            return
        self._last_health = now
        books = [
            self.service.get_book(asset_id)
            for asset_id in self.service.subscribed_token_ids
        ]
        ready = [
            book.identity.token_id for book in books if book is not None and book.ready
        ]
        execution_assets = {
            asset_id
            for asset_id, target in self.targets.items()
            if target.market_state == "LIVE" and target.execution_eligible
        }
        observed_now = _now()
        fresh = sorted(
            (
                asset_id
                for asset_id in ready
                if self.history.get(asset_id)
                and asset_id not in self.feed_mismatch_assets
                and self.history[asset_id][-1].coverage_grade not in {"C", "D"}
                and not self.history[asset_id][-1].has_gap
                and (
                    observed_now - self.history[asset_id][-1].observed_at
                ).total_seconds()
                * 1000
                <= self.engine.config.max_book_age_ms
            ),
            key=lambda asset_id: self.history[asset_id][-1].observed_at,
            reverse=True,
        )
        stale = [book for book in books if book is not None and book.status == "stale"]
        self.stats.ready_books = len(ready)
        self.stats.execution_watched_assets = len(execution_assets)
        self.stats.execution_ready_books = len(execution_assets.intersection(ready))
        self.stats.execution_fresh_books = len(execution_assets.intersection(fresh))
        self.stats.fresh_books = len(fresh)
        self.stats.stale_books = len(stale)
        self.stats.resyncing_assets = len(self.resyncing_assets)
        self.stats.stale_asset_sample = [book.identity.token_id for book in stale[:10]]
        self.stats.ready_asset_sample = ready[:10]
        self.stats.fresh_asset_sample = fresh[:10]
        self._sync_authority_stats()
        self.stats.fresh_book_sample = [
            _checkpoint_head(self.history[asset_id][-1], observed_now)
            for asset_id in fresh
        ]
        # Publish the in-memory book head before any database I/O. Live
        # preflight has sub-second freshness limits and must not inherit a slow
        # current-book or health-table write.
        self.stats.updated_at = _now().isoformat()
        _write_json(self.status_path, self.stats.as_dict())

        if (
            self._current_book_write_task is not None
            and self._current_book_write_task.done()
        ):
            try:
                self._current_book_write_task.result()
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"current_book_write: {_exception_text(exc)}"
            else:
                self._persisted_current_state.update(self._current_book_write_states)
                if str(self.stats.last_error or "").startswith("current_book_write:"):
                    self.stats.last_error = None
            self._current_book_write_task = None
            self._current_book_write_states = {}

        if self._health_counts_task is not None and self._health_counts_task.done():
            task = self._health_counts_task
            self._health_counts_task = None
            try:
                counts = task.result()
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"health_counts: {_exception_text(exc)}"
            else:
                self.stats.queued_intents = counts["queued"]
                self.stats.processing_intents = counts["processing"]
                if str(self.stats.last_error or "").startswith("health_counts:"):
                    self.stats.last_error = None
        if self._health_counts_task is None:
            self._health_counts_task = asyncio.create_task(
                self._db_call(self.store.counts),
                name="paper-live-health-counts",
            )
        current_rows: list[ArrivalBookCheckpoint] = []
        current_states: dict[str, tuple[Any, ...]] = {}
        redundant_assets: set[str] = set()
        for asset_id in ready:
            history = self.history.get(asset_id)
            target = self.targets.get(asset_id)
            if not history or target is None:
                continue
            effective = self._effective_target(asset_id, target)
            latest = history[-1]
            current = replace(
                latest,
                coverage_grade=effective.coverage_grade,
                market_state=(
                    effective.market_state
                    if effective.execution_eligible
                    else "NOT_TRADABLE"
                ),
                has_gap=effective.has_gap,
            )
            state = (
                current.source_event_end,
                current.coverage_grade,
                current.market_state,
                current.has_gap,
                self.stats.transport_state,
                asset_id in self.redundant_ready_assets,
            )
            if self._persisted_current_state.get(asset_id) != state:
                current_rows.append(current)
                current_states[asset_id] = state
            if asset_id in self.redundant_ready_assets:
                redundant_assets.add(asset_id)
        if current_rows and self._current_book_write_task is None:
            self._current_book_write_states = current_states
            self._current_book_write_task = asyncio.create_task(
                self._db_call(
                    self.store.persist_current_books,
                    current_rows,
                    transport_state=self.stats.transport_state,
                    redundant_asset_ids=redundant_assets,
                ),
                name="paper-live-current-book-write",
            )
        self.stats.updated_at = _now().isoformat()
        if str(self.stats.last_error or "").startswith("health_write:"):
            self.stats.last_error = None
        payload = self.stats.as_dict()
        payload["sampled_at"] = payload["updated_at"]
        _write_json(self.status_path, payload)
        self.health_spool.append(payload)
        if self._health_write_task is not None and self._health_write_task.done():
            task = self._health_write_task
            self._health_write_task = None
            try:
                task.result()
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"health_write: {_exception_text(exc)}"
                self.stats.updated_at = _now().isoformat()
                _write_json(self.status_path, self.stats.as_dict())
            else:
                if self._health_write_batch is not None:
                    self.health_spool.acknowledge(
                        self._health_write_batch.consumed_lines
                    )
            self._health_write_batch = None
        if self._health_write_task is None:
            batch = self.health_spool.peek(limit=1000)
            if not batch.records and batch.consumed_lines:
                self.health_spool.acknowledge(batch.consumed_lines)
                batch = self.health_spool.peek(limit=1000)
            if not batch.records:
                return
            self._health_write_batch = batch
            self._health_write_task = asyncio.create_task(
                self._db_call(
                    self.store.write_health_batch,
                    self.worker_id,
                    batch.records,
                ),
                name="paper-live-health-write",
            )

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self.health_seconds)
            try:
                await self._write_health(force=False)
                if str(self.stats.last_error or "").startswith("health_loop:"):
                    self.stats.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"health_loop: {_exception_text(exc)}"

    async def _artifact_heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.health_seconds)
            try:
                await self._persist_simulator_artifact_heartbeat(
                    status="RUNNING",
                    force=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001,S112
                # The persistence method records its own failure counters. A
                # slow control-plane write must never stop local health output.
                continue


def checkpoint_from_live_book(
    book: LocalOrderBook,
    target: LiveWatchTarget,
    *,
    observed_at: datetime,
    connection_id: str,
    message_seq: int,
) -> ArrivalBookCheckpoint:
    fingerprint = book.fingerprint()
    raw_id = (
        f"{connection_id}|{message_seq}|{book.identity.token_id}|{book.generation}|{fingerprint}|"
        f"{observed_at.isoformat()}|{target.coverage_grade}|{target.market_state}|"
        f"{int(target.execution_eligible)}|{int(target.has_gap)}"
    )
    checkpoint_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:32]
    grade = (
        target.coverage_grade
        if target.coverage_grade in {"A_PLUS", "A", "B", "C", "D"}
        else "D"
    )
    source_event = f"{connection_id}:{int(message_seq)}"
    return ArrivalBookCheckpoint(
        checkpoint_id=checkpoint_id,
        asset_id=book.identity.token_id,
        market_id=str(target.market_id),
        condition_id=target.condition_id,
        observed_at=observed_at,
        generation=book.generation,
        coverage_grade=grade,
        bids=tuple(
            PaperBookLevel(price, size)
            for price, size in sorted(book.bids.items(), reverse=True)
        ),
        asks=tuple(
            PaperBookLevel(price, size) for price, size in sorted(book.asks.items())
        ),
        market_state=target.market_state
        if target.execution_eligible
        else "NOT_TRADABLE",
        book_status="READY" if book.ready else book.status.upper(),
        has_gap=bool(target.has_gap),
        source_event_start=source_event,
        source_event_end=source_event,
    )


def _checkpoint_at(
    history: deque[ArrivalBookCheckpoint] | None, timestamp: datetime
) -> ArrivalBookCheckpoint | None:
    if not history:
        return None
    eligible = [item for item in history if item.observed_at <= timestamp]
    return max(eligible, key=lambda item: item.observed_at) if eligible else None


def _desired(item: LiveWatchTarget) -> DesiredSubscription:
    return DesiredSubscription(
        asset_id=item.asset_id,
        market_id=_int_or_zero(item.market_id),
        condition_id=item.condition_id,
        market_slug=item.market_slug,
        market_title=None,
        outcome_name=item.outcome_name,
        outcome_index=item.outcome_index,
        market_state=item.market_state,
        execution_eligible=item.execution_eligible,
        reason="paper_live_watchlist",
    )


def _gate_state(item: LiveWatchTarget) -> tuple[str, bool, str, bool]:
    return item.market_state, item.execution_eligible, item.coverage_grade, item.has_gap


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--proxy-url", default="http://127.0.0.1:17890")
    run.add_argument("--proxy-urls", default="")
    run.add_argument("--secondary-proxy-url", default="")
    run.add_argument("--secondary-proxy-urls", default="")
    run.add_argument("--external-event-socket", type=Path)
    run.add_argument("--external-subscription-file", type=Path)
    run.add_argument(
        "--external-sources",
        default="primary,secondary",
        help="comma-separated collector source families in external event mode",
    )
    run.add_argument("--external-feed-stale-seconds", type=float, default=30.0)
    run.add_argument("--ws-url", default=DEFAULT_WS_URL)
    run.add_argument(
        "--ws-backend", choices=("aiohttp", "websockets"), default="aiohttp"
    )
    run.add_argument("--max-watch-assets", type=int, default=120)
    run.add_argument("--seed-watchlist-limit", type=int, default=120)
    run.add_argument(
        "--skip-schema-init",
        action="store_true",
        help="run against a schema already prepared by the init-schema command",
    )
    run.add_argument("--seed-reconcile-seconds", type=float, default=30.0)
    run.add_argument("--watch-refresh-seconds", type=float, default=5.0)
    run.add_argument("--intent-poll-seconds", type=float, default=0.1)
    run.add_argument("--health-seconds", type=float, default=2.0)
    run.add_argument("--order-delay-ms", type=int, default=100)
    run.add_argument("--max-book-age-ms", type=int, default=2_000)
    run.add_argument(
        "--fee-bps",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_FEE_BPS", "0")),
    )
    run.add_argument(
        "--initial-cash",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_INITIAL_CASH", "10000")),
    )
    run.add_argument("--settlement-poll-seconds", type=float, default=30.0)
    run.add_argument("--feed-idle-reconnect-seconds", type=float, default=60.0)
    run.add_argument("--reconnect-seconds", type=float, default=0.5)
    run.add_argument("--secondary-start-delay-seconds", type=float, default=12.0)
    run.add_argument("--rest-proxy-url", default="")
    run.add_argument("--rest-timeout-seconds", type=float, default=10.0)
    run.add_argument("--rest-retries", type=int, default=2)
    run.add_argument(
        "--paper-security-enforce",
        action="store_true",
        help="reject live credentials or order-submit dependencies before startup",
    )
    run.add_argument(
        "--paper-security-audit-log",
        type=Path,
        default=Path(
            os.environ.get(
                "PAPER_SECURITY_AUDIT_LOG",
                "runtime_outputs/security/paper-security-audit.jsonl",
            )
        ),
    )
    run.add_argument("--rest-resync-retry-seconds", type=float, default=5.0)
    run.add_argument("--db-operation-timeout-seconds", type=float, default=2.0)
    run.add_argument(
        "--persistent-db-connections",
        action="store_true",
        help="reuse one PostgreSQL connection per DB worker thread",
    )
    run.add_argument(
        "--market-terms-ttl-seconds",
        type=int,
        default=int(os.environ.get("PAPER_LIVE_MARKET_TERMS_TTL_SECONDS", "300")),
    )
    run.add_argument("--disable-dynamic-market-terms", action="store_true")
    run.add_argument(
        "--maker-holdout-evaluation",
        type=Path,
        default=Path(
            os.environ.get(
                "PAPER_MAKER_HOLDOUT_EVALUATION",
                "reports/maker/holdout-current/evaluation.json",
            )
        ),
        help="immutable Maker holdout gate used for fail-closed model selection",
    )
    run.add_argument(
        "--maker-offline-evaluation",
        type=Path,
        default=Path(
            os.environ.get(
                "PAPER_MAKER_OFFLINE_EVALUATION",
                "runtime_outputs/maker_calibration/offline-summary-current.json",
            )
        ),
        help="public L2/OrderFilled evidence used only for research forecasts",
    )
    run.add_argument(
        "--maker-probability-calibration-artifact",
        type=Path,
        default=Path(
            os.environ.get(
                "PAPER_MAKER_PROBABILITY_CALIBRATION_ARTIFACT",
                "runtime_outputs/maker_calibration/probability-current.json",
            )
        ),
        help=(
            "validated low-probability Maker artifact; it never changes "
            "authoritative strict fills or portfolio PnL"
        ),
    )
    run.add_argument(
        "--nav-snapshot-seconds",
        type=float,
        default=float(os.environ.get("PAPER_LIVE_NAV_SNAPSHOT_SECONDS", "5")),
    )
    run.add_argument(
        "--nav-history-seconds",
        type=float,
        default=float(os.environ.get("PAPER_LIVE_NAV_HISTORY_SECONDS", "60")),
    )
    run.add_argument(
        "--risk-max-order-notional",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_RISK_MAX_ORDER_NOTIONAL", "1000")),
    )
    run.add_argument(
        "--risk-max-strategy-gross",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_RISK_MAX_STRATEGY_GROSS", "10000")),
    )
    run.add_argument(
        "--risk-max-condition",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_RISK_MAX_CONDITION", "2500")),
    )
    run.add_argument(
        "--risk-max-event",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_RISK_MAX_EVENT", "5000")),
    )
    run.add_argument(
        "--risk-max-neg-risk-group",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_RISK_MAX_NEG_RISK_GROUP", "5000")),
    )
    run.add_argument(
        "--risk-max-category",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_RISK_MAX_CATEGORY", "7500")),
    )
    run.add_argument(
        "--risk-max-daily-loss",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_RISK_MAX_DAILY_LOSS", "1000")),
    )
    run.add_argument(
        "--risk-max-open-orders",
        type=int,
        default=int(os.environ.get("PAPER_LIVE_RISK_MAX_OPEN_ORDERS", "100")),
    )
    run.add_argument(
        "--risk-max-order-rate-per-minute",
        type=int,
        default=int(os.environ.get("PAPER_LIVE_RISK_MAX_ORDER_RATE_PER_MINUTE", "120")),
    )
    run.add_argument(
        "--risk-max-visible-depth-ratio",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_LIVE_RISK_MAX_VISIBLE_DEPTH_RATIO", "1")),
    )
    operations_mode = run.add_mutually_exclusive_group()
    operations_mode.add_argument(
        "--operations-admission-shadow",
        action="store_true",
        help="observe global SLO admission decisions without changing outcomes",
    )
    operations_mode.add_argument(
        "--operations-admission-enforce",
        action="store_true",
        help="enforce GREEN/YELLOW/ORANGE/RED paper admission modes",
    )
    eligibility_mode = run.add_mutually_exclusive_group()
    eligibility_mode.add_argument(
        "--unified-admission-shadow",
        action="store_true",
        help="persist geoblock eligibility decisions without changing paper outcomes",
    )
    eligibility_mode.add_argument(
        "--unified-admission-enforce",
        action="store_true",
        help="make route eligibility authoritative for paper order intents",
    )
    run.add_argument(
        "--admission-geoblock-proxy-url",
        default=os.environ.get("PAPER_GEOBLOCK_PROXY_URL", ""),
    )
    run.add_argument(
        "--admission-geoblock-timeout-seconds",
        type=float,
        default=float(os.environ.get("PAPER_GEOBLOCK_TIMEOUT_SECONDS", "5")),
    )
    run.add_argument(
        "--admission-geoblock-ttl-seconds",
        type=float,
        default=float(os.environ.get("PAPER_GEOBLOCK_TTL_SECONDS", "60")),
    )
    run.add_argument(
        "--operations-status-path",
        type=Path,
        default=Path(
            os.environ.get(
                "PAPER_OPERATIONS_STATUS_PATH",
                "runtime_outputs/paper_operations/status.json",
            )
        ),
    )
    run.add_argument(
        "--operations-status-max-age-seconds",
        type=float,
        default=float(os.environ.get("PAPER_OPERATIONS_STATUS_MAX_AGE_SECONDS", "90")),
    )
    run.add_argument(
        "--operations-yellow-max-notional",
        type=Decimal,
        default=Decimal(os.environ.get("PAPER_OPERATIONS_YELLOW_MAX_NOTIONAL", "20")),
    )
    run.add_argument(
        "--history-size",
        type=int,
        default=32,
        help="per-asset arrival checkpoints retained for decision-time execution",
    )
    run.add_argument("--status-path", type=Path, default=DEFAULT_STATUS)
    run.add_argument(
        "--build-manifest",
        type=Path,
        default=Path(
            os.environ.get(
                "PAPER_WORKER_BUILD_MANIFEST",
                "runtime_outputs/production/paper-worker-build.json",
            )
        ),
    )
    run.add_argument("--health-spool-path", type=Path)
    run.add_argument("--shutdown-path", type=Path)
    run.add_argument("--max-seconds", type=float, default=0.0)
    run.add_argument(
        "--worker-id",
        default=os.environ.get("PAPER_WORKER_INSTANCE_ID", ""),
    )
    run.add_argument(
        "--authority-partition-key",
        default=os.environ.get("PAPER_WORKER_AUTHORITY_PARTITION_KEY", "paper-global"),
    )
    run.add_argument(
        "--authority-owner-id",
        default=os.environ.get("PAPER_WORKER_AUTHORITY_OWNER_ID", ""),
    )
    run.add_argument(
        "--authority-lease-seconds",
        type=float,
        default=float(os.environ.get("PAPER_WORKER_AUTHORITY_LEASE_SECONDS", "15")),
    )
    run.add_argument(
        "--authority-heartbeat-seconds",
        type=float,
        default=float(os.environ.get("PAPER_WORKER_AUTHORITY_HEARTBEAT_SECONDS", "5")),
    )
    run.add_argument(
        "--authority-enforce",
        action="store_true",
        default=str(os.environ.get("PAPER_WORKER_AUTHORITY_ENFORCE", "")).lower()
        in {"1", "true", "yes", "on"},
        help="enable database rejection of missing, expired, or stale worker epochs",
    )
    run.add_argument(
        "--simulator-run-id",
        default=os.environ.get("PAPER_SIMULATOR_RUN_ID", ""),
        help="stable artifact run identity; blank creates one per worker process",
    )
    venue_mode = run.add_mutually_exclusive_group()
    venue_mode.add_argument(
        "--venue-admission-shadow",
        action="store_true",
        help="persist side-by-side venue admission evidence without enforcing it",
    )
    venue_mode.add_argument(
        "--venue-admission-enforce",
        action="store_true",
        help="make modeled venue admission authoritative for paper commands",
    )
    run.add_argument(
        "--venue-heartbeat-timeout-seconds",
        type=float,
        default=float(os.environ.get("PAPER_VENUE_HEARTBEAT_TIMEOUT_SECONDS", "0")),
        help="0 disables shadow dead-man cancellation",
    )
    run.add_argument(
        "--venue-heartbeat-enforce-release",
        action="store_true",
        help="atomically cancel paper orders and release reservations on dead-man expiry",
    )
    run.add_argument(
        "--lifecycle-scheduler-shadow",
        action="store_true",
        help="compare completed paper lifecycles through the deterministic scheduler",
    )
    run.add_argument(
        "--lifecycle-scheduler-enforce-order",
        action="store_true",
        help=(
            "let the deterministic scheduler authoritatively order due paper "
            "intents; requires lifecycle shadow comparison"
        ),
    )
    overlay_mode = run.add_mutually_exclusive_group()
    overlay_mode.add_argument(
        "--durable-liquidity-overlay-shadow",
        action="store_true",
        help="record durable allocations while preserving current paper fills",
    )
    overlay_mode.add_argument(
        "--durable-liquidity-overlay-enforce",
        action="store_true",
        help="bind paper fills to durable terminal liquidity allocations",
    )
    run.add_argument(
        "--liquidity-overlay-version",
        default=os.environ.get("PAPER_LIQUIDITY_OVERLAY_VERSION", ""),
        help="stable run/version identity required for restart-safe allocation",
    )
    run.add_argument(
        "--liquidity-arrival-window-ms",
        type=int,
        default=int(os.environ.get("PAPER_LIQUIDITY_ARRIVAL_WINDOW_MS", "1000")),
    )
    oms_mode = run.add_mutually_exclusive_group()
    oms_mode.add_argument(
        "--own-order-oms-shadow",
        action="store_true",
        help="record durable self-trade admission without changing paper outcomes",
    )
    oms_mode.add_argument(
        "--own-order-oms-enforce",
        action="store_true",
        help="reject paper commands which conflict with durable own working orders",
    )
    run.add_argument(
        "--paper-account-id",
        default=os.environ.get("PAPER_OMS_ACCOUNT_ID", ""),
        help="shared account identity used across paper strategies",
    )
    run.add_argument(
        "--self-trade-policy",
        choices=tuple(item.value for item in SelfTradePolicy),
        default=SelfTradePolicy.REJECT_INCOMING.value,
    )
    run.add_argument(
        "--fill-finality-shadow",
        action="store_true",
        help=(
            "persist fills as provisional and require explicit lifecycle "
            "reconciliation for economic finality"
        ),
    )
    run.add_argument(
        "--fill-finality-auto-reconcile",
        action="store_true",
        help=(
            "confirm or void provisional paper fills from the durable paper "
            "order and ledger lifecycle"
        ),
    )
    run.add_argument(
        "--fill-finality-reconcile-seconds",
        type=float,
        default=float(os.environ.get("PAPER_FILL_FINALITY_RECONCILE_SECONDS", "2")),
    )
    sub.add_parser("init-schema")
    seed = sub.add_parser("seed-watchlist")
    seed.add_argument("--limit", type=int, default=120)
    watch = sub.add_parser("watch")
    watch.add_argument("asset_id")
    watch.add_argument("--strategy-id", default="manual")
    submit = sub.add_parser("submit")
    submit.add_argument("asset_id")
    submit.add_argument("--strategy-id", required=True)
    submit.add_argument("--client-order-id", required=True)
    submit.add_argument("--side", choices=("BUY", "SELL"), required=True)
    submit.add_argument("--tif", choices=("GTC", "GTD", "FOK", "FAK"), default="FAK")
    submit.add_argument("--limit-price", type=Decimal, required=True)
    submit.add_argument("--size", type=Decimal, required=True)
    submit.add_argument("--amount-unit", choices=("SHARES", "QUOTE"), default="SHARES")
    submit.add_argument("--post-only", action="store_true")
    submit.add_argument("--builder-code")
    submit.add_argument("--builder-taker-fee-bps", type=int, default=0)
    submit.add_argument("--builder-maker-fee-bps", type=int, default=0)
    submit.add_argument("--decision-ts")
    submit.add_argument("--expires-at")
    submit_batch = sub.add_parser("submit-batch")
    submit_batch.add_argument("--batch-id", required=True)
    submit_batch.add_argument("--config-hash", required=True)
    submit_batch.add_argument("--input", type=Path, required=True)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("intent_id", type=int)
    cancel.add_argument("--reason", default="user_cancel_requested")
    cancel.add_argument("--latency-ms", type=int, default=100)
    replace_order = sub.add_parser("replace")
    replace_order.add_argument("intent_id", type=int)
    replace_order.add_argument("--limit-price", type=Decimal, required=True)
    replace_order.add_argument("--size", type=Decimal, required=True)
    replace_order.add_argument("--latency-ms", type=int, default=100)
    replace_order.add_argument("--reason", default="user_replace_requested")
    sub.add_parser("status")
    portfolio = sub.add_parser("portfolio")
    portfolio.add_argument("--strategy-id")
    risk_control = sub.add_parser("risk-control")
    risk_control.add_argument("--strategy-id", required=True)
    action = risk_control.add_mutually_exclusive_group(required=True)
    action.add_argument("--halt", action="store_true")
    action.add_argument("--resume", action="store_true")
    risk_control.add_argument("--reason")
    finality = sub.add_parser("finality-reconcile")
    finality.add_argument("trade_id")
    finality.add_argument(
        "--outcome",
        choices=("RETRYING", "CONFIRMED", "FAILED", "VOIDED"),
        required=True,
    )
    finality.add_argument("--event-id", required=True)
    finality.add_argument("--event-ts")
    finality.add_argument("--reason", default="lifecycle_reconciliation")
    finality_status = sub.add_parser("finality-status")
    finality_status.add_argument("trade_id")
    finality_nav = sub.add_parser("finality-nav")
    finality_nav.add_argument("--account-id", required=True)
    finality_nav.add_argument("--include-provisional", action="store_true")
    clarification = sub.add_parser("clarify-market")
    clarification.add_argument("condition_id")
    clarification.add_argument("--source-event-id", required=True)
    clarification.add_argument("--clarified-at")
    clarification.add_argument("--payload-file", type=Path)
    sub.add_parser("settle")
    return parser.parse_args(argv)


def _validate_run_args(args: argparse.Namespace) -> None:
    if not str(args.authority_partition_key).strip():
        raise ValueError("paper worker authority partition is required")
    if args.authority_lease_seconds <= 0:
        raise ValueError("paper worker authority lease must be positive")
    if not 0 < args.authority_heartbeat_seconds < args.authority_lease_seconds:
        raise ValueError(
            "paper worker authority heartbeat must be positive and shorter than lease"
        )
    if args.venue_heartbeat_enforce_release and (
        not (args.venue_admission_shadow or args.venue_admission_enforce)
        or args.venue_heartbeat_timeout_seconds <= 0
    ):
        raise ValueError(
            "venue heartbeat release enforcement requires admission shadow "
            "and a positive timeout"
        )
    if args.lifecycle_scheduler_enforce_order and not args.lifecycle_scheduler_shadow:
        raise ValueError(
            "lifecycle scheduler authority requires lifecycle scheduler shadow"
        )
    if args.venue_admission_enforce and not args.lifecycle_scheduler_enforce_order:
        raise ValueError(
            "venue admission enforcement requires deterministic scheduler authority"
        )
    durable_overlay = (
        args.durable_liquidity_overlay_shadow or args.durable_liquidity_overlay_enforce
    )
    if durable_overlay and not args.lifecycle_scheduler_enforce_order:
        raise ValueError(
            "durable liquidity overlay requires deterministic scheduler authority"
        )
    if durable_overlay and not str(args.liquidity_overlay_version).strip():
        raise ValueError("durable liquidity overlay requires a stable version")
    if durable_overlay and int(args.liquidity_arrival_window_ms) <= 0:
        raise ValueError("liquidity arrival window must be positive")
    durable_oms = args.own_order_oms_shadow or args.own_order_oms_enforce
    if durable_oms and not str(args.paper_account_id).strip():
        raise ValueError("durable own-order OMS requires a shared paper account id")
    if args.own_order_oms_enforce and not args.lifecycle_scheduler_enforce_order:
        raise ValueError(
            "own-order OMS enforcement requires deterministic scheduler authority"
        )
    if args.fill_finality_auto_reconcile and not args.fill_finality_shadow:
        raise ValueError(
            "automatic fill finality reconciliation requires fill finality shadow"
        )
    if args.operations_status_max_age_seconds <= 0:
        raise ValueError("operations status max age must be positive")
    if args.operations_yellow_max_notional < 0:
        raise ValueError("operations yellow max notional cannot be negative")


def _load_worker_build_id(manifest_path: Path) -> str | None:
    try:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    build_id = str(payload.get("build_id") or "").strip().lower()
    if len(build_id) != 64 or any(char not in "0123456789abcdef" for char in build_id):
        return None
    return build_id


def main(argv: list[str] | None = None) -> int:
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    args = parse_args(argv)
    if args.command == "run":
        if args.paper_security_enforce:
            enforce_paper_security_boundary(
                environ=os.environ,
                audit_log=args.paper_security_audit_log,
                source_root=Path(__file__).resolve().parents[2],
            )
        _validate_run_args(args)
    base_connection_factory = (
        ThreadLocalPostgresConnectionFactory()
        if args.command == "run" and args.persistent_db_connections
        else postgres_connection
    )
    default_initial_cash = Decimal(os.environ.get("PAPER_LIVE_INITIAL_CASH", "10000"))
    skip_schema_init = bool(
        args.command == "run" and getattr(args, "skip_schema_init", False)
    )
    initialize_schema = args.command == "init-schema" or (
        args.command == "run" and not skip_schema_init
    )
    if initialize_schema:
        # Migrations are explicit control-plane writes. They must not borrow an
        # execution lease, and ordinary commands must not repeat DDL.
        schema_connection_factory = ControlPlanePostgresConnectionFactory(
            base_connection_factory
        )
        schema_store = LiveShadowStore(schema_connection_factory)
        schema_oms_store = PostgresOwnOrderStore(schema_connection_factory)
        schema_finality_store = PostgresFillFinalityStore(schema_connection_factory)
        schema_store.ensure_schema()
        schema_oms_store.ensure_schema()
        schema_finality_store.ensure_schema()
        PostgresPaperLedgerSink(
            schema_connection_factory,
            initial_cash=default_initial_cash,
        )
        PostgresSimulatorArtifactStore(schema_connection_factory).ensure_schema()
        PostgresLiquidityOverlayStore(schema_connection_factory).ensure_schema()
        PostgresAdmissionStore(schema_connection_factory).ensure_schema()
        AuthorityLeaseStore(schema_connection_factory).ensure_schema()
    if args.command == "init-schema":
        PostgresPersistentEventKernelStore(
            schema_connection_factory,
            partition_key=os.environ.get(
                "PAPER_WORKER_AUTHORITY_PARTITION_KEY",
                "paper-global",
            ),
        ).initialize_partition_counters()
        print(json.dumps({"status": "ready"}))
        return 0
    authority_controller = None
    worker_id = None
    worker_build_id = None
    if args.command == "run":
        worker_build_id = _load_worker_build_id(args.build_manifest)
        if args.operations_admission_enforce and worker_build_id is None:
            raise ValueError(
                "operations admission enforcement requires a valid worker build manifest"
            )
        worker_id = str(args.worker_id).strip() or f"paper-live-{uuid4().hex[:12]}"
        authority_store = AuthorityLeaseStore(base_connection_factory)
        if args.authority_enforce:
            authority_store.set_fencing_enforced(True)
        authority_handle = AuthorityLeaseHandle()
        authority_controller = AuthorityLeaseController(
            authority_store,
            partition_key=str(args.authority_partition_key),
            owner_instance_id=(str(args.authority_owner_id).strip() or worker_id),
            lease_seconds=float(args.authority_lease_seconds),
            heartbeat_seconds=float(args.authority_heartbeat_seconds),
            handle=authority_handle,
        )
        db_connection_factory = FencedPostgresConnectionFactory(
            base_connection_factory,
            authority_handle,
        )
    else:
        db_connection_factory = ControlPlanePostgresConnectionFactory(
            base_connection_factory
        )
    store = LiveShadowStore(db_connection_factory)
    oms_control_store = PostgresOwnOrderStore(db_connection_factory)
    finality_control_store = PostgresFillFinalityStore(db_connection_factory)
    if args.command == "seed-watchlist":
        print(json.dumps({"changed": store.seed_watchlist(limit=args.limit)}))
        return 0
    if args.command == "watch":
        print(
            json.dumps(
                {"watched": store.watch(args.asset_id, strategy_id=args.strategy_id)}
            )
        )
        return 0
    if args.command == "submit":
        ledger = PostgresPaperLedgerSink(
            db_connection_factory,
            initial_cash=default_initial_cash,
            ensure_schema=False,
        )
        ledger.ensure_account(args.strategy_id)
        # A new account can require a slow cross-host DB round trip. Generate
        # the implicit strategy timestamp only after that prerequisite is ready
        # so a freshly enqueued command cannot start life already stale.
        decision_ts = _parse_datetime(args.decision_ts) if args.decision_ts else _now()
        intent_id = store.submit(
            strategy_id=args.strategy_id,
            client_order_id=args.client_order_id,
            asset_id=args.asset_id,
            side=args.side,
            time_in_force=args.tif,
            limit_price=args.limit_price,
            size=args.size,
            post_only=args.post_only,
            decision_ts=decision_ts,
            amount_unit=args.amount_unit,
            builder_code=args.builder_code,
            builder_taker_fee_bps=args.builder_taker_fee_bps,
            builder_maker_fee_bps=args.builder_maker_fee_bps,
            expires_at=_parse_datetime(args.expires_at) if args.expires_at else None,
        )
        print(
            json.dumps({"intent_id": intent_id, "decision_ts": decision_ts.isoformat()})
        )
        return 0
    if args.command == "submit-batch":
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        source_orders = payload.get("orders") if isinstance(payload, dict) else payload
        if not isinstance(source_orders, list) or not source_orders:
            raise ValueError("batch input must be a non-empty list or {orders:[...]}")
        orders: list[dict[str, Any]] = []
        for raw in source_orders:
            if not isinstance(raw, dict):
                raise TypeError("each batch order must be an object")
            order = dict(raw)
            order["decision_ts"] = (
                _parse_datetime(str(order["decision_ts"]))
                if order.get("decision_ts")
                else None
            )
            if order.get("expires_at"):
                order["expires_at"] = _parse_datetime(str(order["expires_at"]))
            orders.append(order)
        ledger = PostgresPaperLedgerSink(
            db_connection_factory,
            initial_cash=default_initial_cash,
            ensure_schema=False,
        )
        for strategy_id in sorted({str(order["strategy_id"]) for order in orders}):
            ledger.ensure_account(strategy_id)
        enqueue_ts = _now()
        for order in orders:
            if order["decision_ts"] is None:
                order["decision_ts"] = enqueue_ts
        intent_ids = store.submit_batch(
            orders,
            batch_id=args.batch_id,
            config_hash=args.config_hash,
        )
        print(
            json.dumps(
                {
                    "batch_id": args.batch_id,
                    "intent_ids": intent_ids,
                    "child_count": len(intent_ids),
                    "paper_only": True,
                    "live_submission_performed": False,
                }
            )
        )
        return 0
    if args.command == "cancel":
        ledger = PostgresPaperLedgerSink(
            db_connection_factory,
            initial_cash=default_initial_cash,
            ensure_schema=False,
        )
        canceled = store.cancel(
            args.intent_id,
            reason=args.reason,
            latency_ms=max(0, int(args.latency_ms)),
        )
        current = store.load_intent(args.intent_id) or {}
        if canceled:
            oms_control_store.request_cancel_for_intent(
                args.intent_id,
                event_id=f"paper-oms-cancel-request:{args.intent_id}",
                reason=args.reason,
            )
        if canceled and str(current.get("status")) == "CANCELED":
            ledger.release_order_reservation(args.intent_id, reason=args.reason)
        print(
            json.dumps(
                {
                    "intent_id": args.intent_id,
                    "cancel_requested": canceled,
                    "canceled": str(current.get("status")) == "CANCELED",
                    "status": current.get("status"),
                    "order_state": current.get("order_state"),
                    "cancel_arrival_ts": current.get("cancel_arrival_ts"),
                },
                default=str,
            )
        )
        return 0
    if args.command == "replace":
        requested = store.replace(
            args.intent_id,
            limit_price=args.limit_price,
            size=args.size,
            latency_ms=max(0, int(args.latency_ms)),
            reason=args.reason,
        )
        current = store.load_intent(args.intent_id) or {}
        if requested:
            oms_control_store.request_cancel_for_intent(
                args.intent_id,
                event_id=f"paper-oms-replace-request:{args.intent_id}",
                reason=args.reason,
            )
        print(
            json.dumps(
                {
                    "intent_id": args.intent_id,
                    "replace_requested": requested,
                    "status": current.get("status"),
                    "order_state": current.get("order_state"),
                    "replace_arrival_ts": current.get("replace_arrival_ts"),
                },
                default=str,
            )
        )
        return 0
    if args.command == "status":
        payload = store.status()
        payload["portfolio"] = PostgresPaperLedgerSink(
            db_connection_factory,
            initial_cash=default_initial_cash,
            ensure_schema=False,
        ).summary()
        print(json.dumps(payload, default=str, indent=2))
        return 0
    if args.command == "portfolio":
        ledger = PostgresPaperLedgerSink(
            db_connection_factory,
            initial_cash=default_initial_cash,
            ensure_schema=False,
        )
        payload = ledger.summary(strategy_id=args.strategy_id)
        payload["performance_finality_policy"] = "CONFIRMED_ONLY"
        print(json.dumps(payload, default=str, indent=2))
        return 0
    if args.command == "risk-control":
        store.set_strategy_risk_control(
            args.strategy_id,
            trading_enabled=bool(args.resume),
            kill_switch=bool(args.halt),
            reason=args.reason,
        )
        print(
            json.dumps(
                {
                    "strategy_id": args.strategy_id,
                    "trading_enabled": bool(args.resume),
                    "kill_switch": bool(args.halt),
                    "reason": args.reason,
                }
            )
        )
        return 0
    if args.command == "clarify-market":
        payload = (
            json.loads(args.payload_file.read_text(encoding="utf-8"))
            if args.payload_file is not None
            else {}
        )
        if not isinstance(payload, dict):
            raise ValueError("clarification payload must be a JSON object")
        clarified_at = (
            _parse_datetime(args.clarified_at) if args.clarified_at else _now()
        )
        payload_hash = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        result = store.enqueue_market_clarification(
            condition_id=args.condition_id,
            source_event_id=args.source_event_id,
            clarified_at=clarified_at,
            payload_hash=payload_hash,
            payload=payload,
        )
        print(json.dumps(result, default=str))
        return 0
    if args.command == "settle":
        ledger = PostgresPaperLedgerSink(
            db_connection_factory,
            initial_cash=default_initial_cash,
            ensure_schema=False,
        )
        print(
            json.dumps(
                {
                    "settled_positions": ledger.settle_resolved_positions(
                        limit=1000,
                        require_redeemed=True,
                    )
                }
            )
        )
        return 0
    if args.command == "finality-reconcile":
        trade = DurablePaperFillFinality(finality_control_store).reconcile(
            args.trade_id,
            outcome=args.outcome,
            event_id=args.event_id,
            event_ts=(_parse_datetime(args.event_ts) if args.event_ts else None),
            reason=args.reason,
        )
        print(
            json.dumps(
                {
                    "trade_id": trade.fragment.trade_id,
                    "state": trade.state.value,
                    "paper_only": True,
                    "live_submission_performed": False,
                }
            )
        )
        return 0
    if args.command == "finality-nav":
        nav = finality_control_store.nav(
            account_id=args.account_id,
            confirmed_only=not args.include_provisional,
        )
        print(
            json.dumps(
                {
                    "account_id": args.account_id,
                    "finality_policy": (
                        "PROVISIONAL_AND_CONFIRMED"
                        if args.include_provisional
                        else "CONFIRMED_ONLY"
                    ),
                    "cash_delta": nav.cash_delta,
                    "position_by_asset": nav.position_by_asset,
                    "fee_delta": nav.fee_delta,
                },
                default=str,
                indent=2,
            )
        )
        return 0
    if args.command == "finality-status":
        trade = finality_control_store.trade(args.trade_id)
        journal = finality_control_store.journal(args.trade_id) if trade else ()
        print(
            json.dumps(
                {
                    "trade": asdict(trade) if trade else None,
                    "journal": [asdict(entry) for entry in journal],
                    "paper_only": True,
                    "live_submission_performed": False,
                },
                default=str,
                indent=2,
            )
        )
        return 0
    ledger_sink = PostgresPaperLedgerSink(
        db_connection_factory,
        initial_cash=args.initial_cash,
        ensure_schema=False,
    )
    config = TakerExecutionConfig(
        latency=PaperLatencyModel(order_delay_ms=max(0, int(args.order_delay_ms))),
        max_book_age_ms=max(1, int(args.max_book_age_ms)),
        fee_bps=max(Decimal(0), args.fee_bps),
    )
    risk_limits = PaperRiskLimits(
        max_order_notional=max(Decimal(0), args.risk_max_order_notional),
        max_strategy_gross_notional=max(Decimal(0), args.risk_max_strategy_gross),
        max_condition_notional=max(Decimal(0), args.risk_max_condition),
        max_event_notional=max(Decimal(0), args.risk_max_event),
        max_neg_risk_group_notional=max(Decimal(0), args.risk_max_neg_risk_group),
        max_category_notional=max(Decimal(0), args.risk_max_category),
        max_daily_loss=max(Decimal(0), args.risk_max_daily_loss),
        max_open_orders=max(1, int(args.risk_max_open_orders)),
        max_order_rate_per_minute=max(1, int(args.risk_max_order_rate_per_minute)),
        max_visible_depth_ratio=max(
            Decimal("0.00000001"), args.risk_max_visible_depth_ratio
        ),
    )
    rest_client = PolymarketPaperClobClient(
        proxy_url=(
            args.rest_proxy_url
            or (
                None
                if args.external_event_socket
                else args.secondary_proxy_url or args.proxy_url or None
            )
        ),
        timeout_seconds=max(1.0, args.rest_timeout_seconds),
        max_retries=max(0, args.rest_retries),
    )
    liquidity_overlay = None
    if args.durable_liquidity_overlay_shadow or args.durable_liquidity_overlay_enforce:
        overlay_store = PostgresLiquidityOverlayStore(db_connection_factory)
        if not skip_schema_init:
            overlay_store.ensure_schema()
        durable_overlay = DurablePaperLiquidityOverlay(
            store=overlay_store,
            overlay_version=str(args.liquidity_overlay_version),
            arrival_window_ns=int(args.liquidity_arrival_window_ms) * 1_000_000,
        )
        liquidity_overlay = (
            ShadowComparingPaperLiquidityOverlay(durable_overlay)
            if args.durable_liquidity_overlay_shadow
            else durable_overlay
        )
    own_order_oms_gate = None
    if args.own_order_oms_shadow or args.own_order_oms_enforce:
        own_order_oms_gate = DurablePaperOmsGate(
            store=oms_control_store,
            account_id=str(args.paper_account_id),
            policy=SelfTradePolicy(str(args.self_trade_policy)),
        )
    fill_finality_shadow = (
        DurablePaperFillFinality(finality_control_store)
        if args.fill_finality_shadow
        else None
    )
    taker_engine = TakerOnlyPaperExecutionEngine(
        config,
        liquidity_overlay=liquidity_overlay,
    )
    artifact_store = PostgresSimulatorArtifactStore(db_connection_factory)
    if not skip_schema_init:
        artifact_store.ensure_schema()
    simulator_run_id = str(args.simulator_run_id).strip() or (
        f"paper-live:{_now().strftime('%Y%m%dT%H%M%SZ')}:{uuid4().hex[:12]}"
    )
    persistent_event_kernel = PostgresPersistentEventKernelStore(
        db_connection_factory,
        partition_key=str(args.authority_partition_key),
    )
    venue_admission_shadow = None
    if args.venue_admission_shadow or args.venue_admission_enforce:
        venue_admission_shadow = VenueAdmissionShadow(
            run_id=simulator_run_id,
            gateway=VenueGateway(
                GatewayConfig(
                    heartbeat=HeartbeatConfig(
                        required=args.venue_heartbeat_timeout_seconds > 0,
                        timeout_ns=max(
                            0,
                            int(args.venue_heartbeat_timeout_seconds * 1_000_000_000),
                        ),
                    )
                )
            ),
            artifact_sink=artifact_store,
            reservation_release_sink=(
                ledger_sink if args.venue_heartbeat_enforce_release else None
            ),
        )
    lifecycle_scheduler_shadow = (
        PaperLifecycleSchedulerShadow(
            artifact_sink=artifact_store,
            run_prefix=f"{simulator_run_id}:lifecycle",
        )
        if args.lifecycle_scheduler_shadow and artifact_store is not None
        else None
    )
    unified_admission_service = None
    if args.unified_admission_shadow or args.unified_admission_enforce:
        unified_admission_service = UnifiedAdmissionService(
            store=PostgresAdmissionStore(db_connection_factory),
            geoblock_provider=PolymarketGeoblockClient(
                proxy_url=args.admission_geoblock_proxy_url or None,
                timeout_seconds=args.admission_geoblock_timeout_seconds,
                ttl_seconds=args.admission_geoblock_ttl_seconds,
            ),
        )
    service = LivePaperShadowService(
        store=store,
        client=(
            None
            if args.external_event_socket
            else PolymarketMarketWsClient(
                ws_url=args.ws_url,
                proxy_url=args.proxy_url,
                ping_interval=20,
                ping_timeout=None,
                reconnect_seconds=max(0.2, args.reconnect_seconds),
                backend=args.ws_backend,
            )
        ),
        secondary_client=(
            PolymarketMarketWsClient(
                ws_url=args.ws_url,
                proxy_url=args.secondary_proxy_url,
                ping_interval=20,
                ping_timeout=None,
                reconnect_seconds=max(0.2, args.reconnect_seconds),
                backend=args.ws_backend,
            )
            if args.secondary_proxy_url and not args.external_event_socket
            else None
        ),
        external_event_socket=args.external_event_socket,
        external_subscription_file=args.external_subscription_file,
        external_sources=tuple(
            item.strip()
            for item in str(args.external_sources).split(",")
            if item.strip()
        ),
        external_feed_stale_seconds=args.external_feed_stale_seconds,
        rest_client=rest_client,
        primary_proxy_urls=_proxy_pool(args.proxy_url, args.proxy_urls),
        secondary_proxy_urls=_proxy_pool(
            args.secondary_proxy_url, args.secondary_proxy_urls
        )
        if args.secondary_proxy_url
        else None,
        engine=taker_engine,
        execution_kernel=ProfessionalPaperExecutionKernel(
            taker_engine,
            risk_gate=CentralPaperRiskGate(risk_limits),
        ),
        maker_model_domain_resolver=MakerModelDomainResolver.from_paths(
            args.maker_holdout_evaluation,
            args.maker_offline_evaluation,
            args.maker_probability_calibration_artifact,
        ),
        market_terms_resolver=(
            None
            if args.disable_dynamic_market_terms
            else CachedPolymarketTermsResolver(
                rest_client,
                store,
                ttl_seconds=args.market_terms_ttl_seconds,
            )
        ),
        portfolio_store=ledger_sink,
        venue_admission_shadow=venue_admission_shadow,
        venue_admission_enforce=args.venue_admission_enforce,
        lifecycle_scheduler_shadow=lifecycle_scheduler_shadow,
        lifecycle_scheduler_enforce_order=(args.lifecycle_scheduler_enforce_order),
        global_liquidity_overlay_verified=(args.durable_liquidity_overlay_enforce),
        own_order_oms_gate=own_order_oms_gate,
        own_order_oms_enforce=args.own_order_oms_enforce,
        fill_finality_shadow=fill_finality_shadow,
        fill_finality_auto_reconcile=args.fill_finality_auto_reconcile,
        fill_finality_reconcile_seconds=args.fill_finality_reconcile_seconds,
        simulator_artifact_store=artifact_store,
        simulator_run_id=simulator_run_id,
        operations_admission_shadow=args.operations_admission_shadow,
        operations_admission_enforce=args.operations_admission_enforce,
        operations_status_path=args.operations_status_path,
        operations_status_max_age_seconds=(args.operations_status_max_age_seconds),
        operations_yellow_max_notional=args.operations_yellow_max_notional,
        unified_admission_service=unified_admission_service,
        unified_admission_shadow=args.unified_admission_shadow,
        unified_admission_enforce=args.unified_admission_enforce,
        max_watch_assets=args.max_watch_assets,
        seed_watchlist_limit=args.seed_watchlist_limit,
        seed_reconcile_seconds=args.seed_reconcile_seconds,
        watch_refresh_seconds=args.watch_refresh_seconds,
        intent_poll_seconds=args.intent_poll_seconds,
        health_seconds=args.health_seconds,
        settlement_poll_seconds=args.settlement_poll_seconds,
        feed_idle_reconnect_seconds=args.feed_idle_reconnect_seconds,
        secondary_start_delay_seconds=args.secondary_start_delay_seconds,
        rest_resync_retry_seconds=args.rest_resync_retry_seconds,
        db_operation_timeout_seconds=args.db_operation_timeout_seconds,
        nav_snapshot_seconds=args.nav_snapshot_seconds,
        nav_history_seconds=args.nav_history_seconds,
        history_size=args.history_size,
        status_path=args.status_path,
        health_spool_path=args.health_spool_path,
        shutdown_path=args.shutdown_path,
        worker_id=worker_id,
        build_id=worker_build_id,
        authority_controller=authority_controller,
        persistent_event_kernel=persistent_event_kernel,
    )
    try:
        stats = asyncio.run(service.run(max_seconds=args.max_seconds))
    except KeyboardInterrupt:
        print(json.dumps({"status": "STOPPED", "reason": "signal"}), flush=True)
        return 0
    print(json.dumps(stats.as_dict(), default=str, indent=2))
    return 0


def _apply_current_event(service: RealtimeOrderBookService, event: NormalizedBookEvent):
    book = service.get_book(event.token_id)
    if (
        not isinstance(event, NormalizedBookSnapshot)
        and book is not None
        and book.last_event_ts_ms is not None
        and event.event_ts_ms < book.last_event_ts_ms
    ):
        return None
    return service.process_event(event)


def _venue_authority_action(venue_result: Any | None) -> str:
    if venue_result is None:
        return "DEFER"
    if bool(venue_result.gateway_terminally_denied):
        return "REJECT"
    if bool(venue_result.gateway_queued) or not bool(venue_result.gateway_accepted_now):
        return "DEFER"
    return "ADMIT"


async def _close_ws_client(client: Any, *, abort: bool) -> None:
    try:
        await client.close(abort=abort)
    except TypeError:
        await client.close()


def _checkpoint_head(
    checkpoint: ArrivalBookCheckpoint, observed_now: datetime
) -> dict[str, Any]:
    return {
        "asset_id": checkpoint.asset_id,
        "checkpoint_id": checkpoint.checkpoint_id,
        "observed_at": checkpoint.observed_at.isoformat(),
        "book_age_ms": checkpoint.age_ms(observed_now),
        "generation": checkpoint.generation,
        "coverage_grade": checkpoint.coverage_grade,
        "market_state": checkpoint.market_state,
        "book_status": checkpoint.book_status,
        "has_gap": checkpoint.has_gap,
        "best_bid": format(checkpoint.bids[0].price, "f") if checkpoint.bids else None,
        "best_ask": format(checkpoint.asks[0].price, "f") if checkpoint.asks else None,
        "source_event_end": checkpoint.source_event_end,
    }


def _rest_event_ts_ms(payload: dict[str, Any]) -> int:
    try:
        timestamp = int(payload.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    return timestamp if timestamp > 0 else int(time.time() * 1000)


def _market_causal_event_type(message: dict[str, Any]) -> str:
    event_type = str(message.get("event_type") or message.get("type") or "").lower()
    if event_type == "book":
        return "BOOK_SNAPSHOT"
    if event_type == "price_change":
        return "BOOK_DELTA"
    if event_type == "tick_size_change":
        return "TICK_SIZE_CHANGE"
    if event_type == "last_trade_price":
        return "EXTERNAL_TRADE"
    if event_type in {"connection_gap", "connection_recovered", "connection_heartbeat"}:
        return "VENUE_STATE_CHANGED"
    return "MARKET_DATA"


def _market_causal_asset_ids(message: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    direct = message.get("asset_id") or message.get("token_id")
    if direct:
        values.append(str(direct))
    for key in ("price_changes", "changes"):
        rows = message.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            asset_id = row.get("asset_id") or row.get("token_id")
            if asset_id:
                values.append(str(asset_id))
    return tuple(dict.fromkeys(values))


def _paper_terminal_event_type(status: str) -> str:
    normalized = str(status).upper()
    if normalized == "WORKING":
        return "PAPER_WORKING"
    if normalized in {"CANCELLED", "CANCELED"}:
        return "PAPER_CANCEL"
    if normalized in {"EXPIRED", "GTD_EXPIRED"}:
        return "PAPER_EXPIRE"
    return "PAPER_REJECT"


def _event_signature(event: NormalizedBookEvent) -> str:
    if isinstance(event, NormalizedBookSnapshot):
        payload = {
            "type": "book",
            "bids": [(str(price), str(size)) for price, size in event.bids],
            "asks": [(str(price), str(size)) for price, size in event.asks],
        }
    else:
        assert isinstance(event, NormalizedBookDelta)
        payload = {
            "type": "price_change",
            "side": event.side,
            "price": str(event.price),
            "size": str(event.size),
        }
    payload["source_hash"] = event.source_hash
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{event.token_id}|{event.event_ts_ms}|{payload_hash}"


def _trade_event_signature(event: NormalizedTradeEvent) -> str:
    payload = {
        "asset_id": event.token_id,
        "price": str(event.price),
        "size": str(event.size),
        "side": event.aggressor_side,
        "timestamp": event.event_ts_ms,
        "transaction_hash": event.transaction_hash,
    }
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"maker-trade:{event.token_id}:{payload_hash}"


def _maker_execution_result(
    plan: MakerQueueAdvancePlan,
    *,
    price: Decimal,
    config: TakerExecutionConfig,
) -> PaperExecutionResult:
    fill_size = plan.incremental_fill_size
    audit_key = hashlib.sha256(
        f"maker|{plan.intent_id}|{plan.event_id}".encode()
    ).hexdigest()
    charge = calculate_fill_fee(
        plan.intent,
        price,
        fill_size,
        config,
        fill_id=f"{audit_key}:0",
        liquidity_role=LiquidityRole.MAKER,
    )
    fidelity = dict(plan.fidelity)
    domain = fidelity.get("maker_model_domain")
    domain_payload = dict(domain) if isinstance(domain, dict) else {}
    fidelity.update(
        {
            "execution_role": "MAKER",
            "maker_fill_evidence": "last_trade_price",
            "maker_trade_event_id": plan.event_id,
            "maker_queue_model_version": plan.next_state.queue_model_version,
            "maker_calibrated_in_domain": bool(
                domain_payload.get("calibrated_in_domain", False)
            ),
            "calibration_domain_status": str(
                domain_payload.get("domain_status") or "STRICT_UNCALIBRATED"
            ),
        }
    )
    return PaperExecutionResult(
        audit_key=audit_key,
        status="FILLED" if plan.remaining_size <= 0 else "PARTIAL",
        reason="maker_strict_trade_evidence_fill",
        intent=plan.intent,
        arrival_ts=plan.event_ts,
        decision_checkpoint_id=None,
        arrival_checkpoint_id=plan.arrival_checkpoint_id,
        book_generation=plan.book_generation,
        coverage_grade=plan.coverage_grade,
        book_age_ms=None,
        fills=(
            PaperTakerFill(
                price=price,
                size=fill_size,
                fee=charge.total_fee,
                level_index=0,
                fee_charge_id=charge.fee_charge_id,
                fee_schedule_id=charge.fee_schedule_id,
                platform_fee_rate=charge.platform_fee_rate,
                platform_fee_exponent=charge.platform_fee_exponent,
                platform_fee=charge.platform_fee,
                builder_fee=charge.builder_fee,
                builder_fee_rate_bps=charge.builder_fee_rate_bps,
                rounding_policy=charge.rounding_policy,
                economics_regime_id=charge.economics_regime_id,
                fee_source=charge.source,
            ),
        ),
        requested_amount=plan.intent.size,
        amount_unit=plan.intent.amount_unit,
        filled_size=fill_size,
        remaining_size=plan.remaining_size,
        filled_notional=price * fill_size,
        remaining_amount=plan.remaining_size,
        avg_fill_price=price,
        total_fee=charge.total_fee,
        slippage=None,
        source_manifest_ids=(),
        source_files=(),
        source_event_start=plan.event_id,
        source_event_end=plan.event_id,
        rest_audit_at=None,
        model_version=plan.next_state.queue_model_version,
        config_hash=hashlib.sha256(
            (f"{config.config_hash}|{plan.next_state.queue_model_version}").encode()
        ).hexdigest()[:20],
        fidelity=fidelity,
    )


def _execution_result_from_payload(payload: dict[str, Any]) -> PaperExecutionResult:
    """Rehydrate one durable JSON result for idempotent accounting recovery."""
    return paper_execution_result_from_payload(payload)


def _proxy_pool(primary: str | None, extras: str) -> list[str | None]:
    values: list[str | None] = [primary]
    values.extend(item.strip() for item in str(extras or "").split(",") if item.strip())
    return list(dict.fromkeys(values)) or [None]


def _rotate_client_proxy(
    client: PolymarketMarketWsClient,
    proxy_urls: list[str | None],
    reconnects: int,
) -> str | None:
    if not proxy_urls:
        return getattr(client, "proxy_url", None)
    active = proxy_urls[int(reconnects) % len(proxy_urls)]
    client.proxy_url = active
    return active


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [
        values[index : index + max(1, size)]
        for index in range(0, len(values), max(1, size))
    ]


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _exception_text(exc: Exception) -> str:
    detail = str(exc).strip() or repr(exc)
    return f"{exc.__class__.__name__}: {detail[:500]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_to_ns(value: datetime) -> int:
    normalized = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = normalized - epoch
    return (
        delta.days * 86_400_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.astimezone(timezone.utc)
        if parsed.tzinfo
        else parsed.replace(tzinfo=timezone.utc)
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, default=str, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _log_shutdown_phase(phase: str) -> None:
    print(
        json.dumps(
            {
                "event": "paper_live_shutdown",
                "phase": str(phase),
                "at": _now().isoformat(),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
