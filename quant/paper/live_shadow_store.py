"""Postgres control plane for the live taker-only paper shadow."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from quant.core.db import postgres_connection
from quant.execution.models.maker_queue import (
    MakerQueueEngine,
    MakerQueueState,
    QueueModel,
)

from .execution_profile import ExecutionProfileDecision
from .live_order_lifecycle import result_transitions
from .market_terms import PaperMarketTerms
from .taker_execution import (
    ArrivalBookCheckpoint,
    OrderIntent,
    PaperBookLevel,
    PaperExecutionResult,
    validate_gtd_expiration,
)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_execution_market_catalog (
        asset_id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        market_slug TEXT,
        market_title TEXT,
        outcome_name TEXT NOT NULL DEFAULT 'UNKNOWN',
        outcome_index INTEGER NOT NULL DEFAULT 0,
        event_id TEXT,
        category TEXT NOT NULL DEFAULT 'unknown',
        enable_neg_risk BOOLEAN NOT NULL DEFAULT FALSE,
        market_state TEXT NOT NULL,
        execution_eligible BOOLEAN NOT NULL DEFAULT FALSE,
        active BOOLEAN NOT NULL DEFAULT FALSE,
        closed BOOLEAN NOT NULL DEFAULT FALSE,
        resolved BOOLEAN NOT NULL DEFAULT FALSE,
        archived BOOLEAN NOT NULL DEFAULT FALSE,
        deprecated BOOLEAN NOT NULL DEFAULT FALSE,
        coverage_grade TEXT NOT NULL DEFAULT 'D',
        has_gap BOOLEAN NOT NULL DEFAULT TRUE,
        last_receive_ts TIMESTAMPTZ,
        current_tick_size NUMERIC,
        min_order_size NUMERIC,
        end_date TIMESTAMPTZ,
        raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        event_title TEXT,
        event_volume NUMERIC,
        winning_asset_id TEXT,
        resolution_status TEXT,
        resolution_source TEXT,
        resolved_time TIMESTAMPTZ,
        source_updated_at TIMESTAMPTZ,
        synced_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_live_watchlist (
        asset_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL DEFAULT 'manual',
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        reason TEXT,
        refresh_requested_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "ALTER TABLE quant.paper_live_watchlist ADD COLUMN IF NOT EXISTS refresh_requested_at TIMESTAMPTZ",
    """
    CREATE TABLE IF NOT EXISTS quant.paper_live_target_assignments (
        worker_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        removed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (worker_id, asset_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_live_order_intents (
        intent_id BIGSERIAL PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        side TEXT NOT NULL,
        time_in_force TEXT NOT NULL,
        limit_price NUMERIC NOT NULL,
        size NUMERIC NOT NULL,
        amount_unit TEXT NOT NULL DEFAULT 'SHARES',
        tick_size NUMERIC,
        min_order_size NUMERIC,
        fee_rate NUMERIC,
        fee_exponent NUMERIC,
        fee_taker_only BOOLEAN NOT NULL DEFAULT TRUE,
        fee_schedule_id TEXT,
        fee_schedule_source TEXT,
        economics_regime_id TEXT,
        builder_code TEXT,
        builder_taker_fee_bps INTEGER NOT NULL DEFAULT 0,
        builder_maker_fee_bps INTEGER NOT NULL DEFAULT 0,
        post_only BOOLEAN NOT NULL DEFAULT FALSE,
        decision_ts TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'QUEUED',
        order_state TEXT NOT NULL DEFAULT 'CREATED',
        remaining_size NUMERIC,
        remaining_amount NUMERIC,
        worker_id TEXT,
        claimed_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        result_audit_key TEXT,
        result JSONB,
        last_error TEXT,
        submit_request_ts TIMESTAMPTZ,
        submit_arrival_ts TIMESTAMPTZ,
        cancel_request_ts TIMESTAMPTZ,
        cancel_arrival_ts TIMESTAMPTZ,
        cancel_ack_ts TIMESTAMPTZ,
        replace_request_ts TIMESTAMPTZ,
        replace_arrival_ts TIMESTAMPTZ,
        replace_limit_price NUMERIC,
        replace_size NUMERIC,
        replace_intent_id BIGINT,
        replaced_from_intent_id BIGINT,
        batch_id TEXT,
        queue_priority_epoch INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (strategy_id, client_order_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_order_events (
        event_id BIGSERIAL PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        intent_id BIGINT NOT NULL,
        strategy_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        reason TEXT,
        result_audit_key TEXT,
        checkpoint_id TEXT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        event_ts TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_market_terms (
        asset_id TEXT PRIMARY KEY,
        condition_id TEXT NOT NULL,
        fee_rate_bps INTEGER NOT NULL CHECK (fee_rate_bps >= 0),
        fee_rate NUMERIC NOT NULL CHECK (fee_rate >= 0),
        fee_exponent NUMERIC NOT NULL CHECK (fee_exponent >= 0),
        fee_taker_only BOOLEAN NOT NULL,
        itode BOOLEAN NOT NULL DEFAULT FALSE,
        seconds_delay INTEGER NOT NULL DEFAULT 0,
        taker_delay_ms INTEGER NOT NULL DEFAULT 0,
        delay_source TEXT NOT NULL DEFAULT 'clob_market_no_delay',
        source TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_market_clarifications (
        clarification_id BIGSERIAL PRIMARY KEY,
        condition_id TEXT NOT NULL,
        source_event_id TEXT NOT NULL UNIQUE,
        clarified_at TIMESTAMPTZ NOT NULL,
        payload_hash TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        asset_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
        canceled_intent_ids BIGINT[] NOT NULL DEFAULT ARRAY[]::bigint[],
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_market_clarification_commands (
        command_id BIGSERIAL PRIMARY KEY,
        condition_id TEXT NOT NULL,
        source_event_id TEXT NOT NULL UNIQUE,
        clarified_at TIMESTAMPTZ NOT NULL,
        payload_hash TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'PENDING',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        worker_id TEXT,
        claimed_at TIMESTAMPTZ,
        applied_at TIMESTAMPTZ,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CHECK (status IN ('PENDING','PROCESSING','APPLIED','FAILED')),
        CHECK (attempts >= 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_market_clarification_commands_due_idx
    ON quant.paper_market_clarification_commands (status,next_attempt_at,command_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_fee_schedules (
        fee_schedule_id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        effective_from TIMESTAMPTZ NOT NULL,
        effective_until TIMESTAMPTZ,
        platform_fee_rate NUMERIC NOT NULL CHECK (platform_fee_rate >= 0),
        platform_fee_exponent NUMERIC NOT NULL CHECK (platform_fee_exponent >= 0),
        platform_taker_only BOOLEAN NOT NULL,
        rounding_policy TEXT NOT NULL,
        economics_regime_id TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_fee_schedules_asset_effective_idx
    ON quant.paper_fee_schedules (asset_id,effective_from DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_risk_decisions (
        intent_id BIGINT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        status TEXT NOT NULL,
        reasons TEXT[] NOT NULL DEFAULT '{}',
        reduce_only BOOLEAN NOT NULL,
        order_notional NUMERIC NOT NULL,
        metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
        decided_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_execution_profile_decisions (
        intent_id BIGINT PRIMARY KEY,
        decision_hash TEXT NOT NULL,
        profile TEXT NOT NULL,
        execution_allowed BOOLEAN NOT NULL,
        resolver_version TEXT NOT NULL,
        execution_model_version TEXT NOT NULL,
        execution_config_hash TEXT NOT NULL,
        fee_schedule_version TEXT NOT NULL,
        latency_model_version TEXT NOT NULL,
        queue_model_version TEXT NOT NULL,
        book_checkpoint_id TEXT,
        book_generation BIGINT,
        coverage_grade TEXT,
        calibration_domain TEXT NOT NULL,
        depth_haircut NUMERIC NOT NULL,
        reason_codes TEXT[] NOT NULL DEFAULT '{}',
        decision JSONB NOT NULL,
        decided_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CHECK (depth_haircut >= 0 AND depth_haircut <= 1)
    )
    """,
    """ALTER TABLE quant.paper_execution_profile_decisions
       ADD COLUMN IF NOT EXISTS execution_config_hash TEXT NOT NULL DEFAULT 'legacy-unbound'""",
    """
    CREATE TABLE IF NOT EXISTS quant.paper_strategy_risk_controls (
        strategy_id TEXT PRIMARY KEY,
        trading_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        kill_switch BOOLEAN NOT NULL DEFAULT FALSE,
        reason TEXT,
        limits JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_paired_probes (
        probe_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        paper_intent_id BIGINT,
        mode TEXT NOT NULL DEFAULT 'no-submit',
        status TEXT NOT NULL DEFAULT 'PENDING',
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        decision_ts TIMESTAMPTZ NOT NULL,
        arrival_ts TIMESTAMPTZ,
        paper_prediction JSONB NOT NULL DEFAULT '{}'::jsonb,
        live_lifecycle JSONB NOT NULL DEFAULT '{}'::jsonb,
        orderfilled_ex_self JSONB NOT NULL DEFAULT '{}'::jsonb,
        bucket_context JSONB NOT NULL DEFAULT '{}'::jsonb,
        audit JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (strategy_id, client_order_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_live_book_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        generation BIGINT NOT NULL,
        coverage_grade TEXT NOT NULL,
        market_state TEXT NOT NULL,
        book_status TEXT NOT NULL,
        has_gap BOOLEAN NOT NULL,
        bids JSONB NOT NULL,
        asks JSONB NOT NULL,
        source_connection_id TEXT,
        source_message_seq BIGINT,
        book_fingerprint TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_live_current_books (
        asset_id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        generation BIGINT NOT NULL,
        coverage_grade TEXT NOT NULL,
        market_state TEXT NOT NULL,
        book_status TEXT NOT NULL,
        has_gap BOOLEAN NOT NULL,
        best_bid NUMERIC,
        best_ask NUMERIC,
        bids JSONB NOT NULL,
        asks JSONB NOT NULL,
        source_connection_id TEXT,
        source_message_seq BIGINT,
        book_fingerprint TEXT,
        transport_state TEXT NOT NULL,
        redundant_feed_match BOOLEAN NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_live_maker_trade_events (
        event_id TEXT PRIMARY KEY,
        worker_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        price NUMERIC NOT NULL,
        size NUMERIC NOT NULL,
        aggressor_side TEXT NOT NULL CHECK (aggressor_side IN ('BUY','SELL')),
        event_ts TIMESTAMPTZ NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        transaction_hash TEXT,
        processing_state TEXT NOT NULL DEFAULT 'PENDING',
        disposition_reason TEXT,
        processed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "ALTER TABLE quant.paper_live_maker_trade_events ADD COLUMN IF NOT EXISTS processing_state TEXT NOT NULL DEFAULT 'PENDING'",
    "ALTER TABLE quant.paper_live_maker_trade_events ADD COLUMN IF NOT EXISTS disposition_reason TEXT",
    "ALTER TABLE quant.paper_live_maker_trade_events ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ",
    """
    CREATE TABLE IF NOT EXISTS quant.maker_research_queue_states (
        paper_order_id TEXT NOT NULL,
        queue_model TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        side TEXT NOT NULL,
        price_tick NUMERIC NOT NULL,
        queue_model_version TEXT NOT NULL,
        displayed_size_at_accept NUMERIC NOT NULL,
        own_orders_ahead NUMERIC NOT NULL,
        estimated_external_queue_ahead NUMERIC NOT NULL,
        accepted_order_size NUMERIC NOT NULL,
        cumulative_trade_volume_at_price NUMERIC NOT NULL DEFAULT 0,
        cumulative_cancel_ahead_estimate NUMERIC NOT NULL DEFAULT 0,
        cumulative_filled_size NUMERIC NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'WORKING',
        accepted_checkpoint_id TEXT,
        accepted_at TIMESTAMPTZ NOT NULL,
        book_generation BIGINT,
        queue_epoch BIGINT NOT NULL DEFAULT 0,
        last_event_id TEXT,
        last_event_ts TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (paper_order_id, queue_model),
        CHECK (queue_model IN ('RISK_AVERSE_QUEUE','PROBABILISTIC_QUEUE'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS maker_research_queue_open_level_idx
    ON quant.maker_research_queue_states (
        asset_id, side, price_tick, state
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.maker_research_queue_events (
        paper_order_id TEXT NOT NULL,
        queue_model TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_kind TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        book_generation BIGINT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (paper_order_id, queue_model, event_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS maker_research_queue_events_time_idx
    ON quant.maker_research_queue_events (event_ts, event_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_live_shadow_health (
        worker_id TEXT PRIMARY KEY,
        transport_state TEXT NOT NULL,
        watched_assets INTEGER NOT NULL DEFAULT 0,
        ready_books INTEGER NOT NULL DEFAULT 0,
        execution_watched_assets INTEGER,
        execution_ready_books INTEGER,
        execution_fresh_books INTEGER,
        fresh_books INTEGER NOT NULL DEFAULT 0,
        stale_books INTEGER NOT NULL DEFAULT 0,
        queued_intents INTEGER NOT NULL DEFAULT 0,
        processing_intents INTEGER NOT NULL DEFAULT 0,
        completed_intents BIGINT NOT NULL DEFAULT 0,
        rejected_intents BIGINT NOT NULL DEFAULT 0,
        websocket_messages BIGINT NOT NULL DEFAULT 0,
        reconnects BIGINT NOT NULL DEFAULT 0,
        feed_mismatch_assets INTEGER NOT NULL DEFAULT 0,
        route_states JSONB NOT NULL DEFAULT '{}'::jsonb,
        route_proxy_urls JSONB NOT NULL DEFAULT '{}'::jsonb,
        route_messages JSONB NOT NULL DEFAULT '{}'::jsonb,
        route_reconnects JSONB NOT NULL DEFAULT '{}'::jsonb,
        backpressure_status TEXT NOT NULL DEFAULT 'DEFER_OR_REJECT',
        backpressure_reasons JSONB NOT NULL DEFAULT '["starting"]'::jsonb,
        last_message_at TIMESTAMPTZ,
        last_error TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_live_shadow_health_samples (
        sample_id BIGSERIAL PRIMARY KEY,
        worker_id TEXT NOT NULL,
        transport_state TEXT NOT NULL,
        watched_assets INTEGER NOT NULL,
        ready_books INTEGER NOT NULL,
        execution_watched_assets INTEGER,
        execution_ready_books INTEGER,
        execution_fresh_books INTEGER,
        fresh_books INTEGER NOT NULL,
        stale_books INTEGER NOT NULL,
        queued_intents INTEGER NOT NULL,
        processing_intents INTEGER NOT NULL,
        completed_intents BIGINT NOT NULL,
        rejected_intents BIGINT NOT NULL,
        websocket_messages BIGINT NOT NULL,
        reconnects BIGINT NOT NULL,
        settled_positions BIGINT NOT NULL DEFAULT 0,
        feed_mismatch_assets INTEGER NOT NULL DEFAULT 0,
        route_states JSONB NOT NULL DEFAULT '{}'::jsonb,
        route_proxy_urls JSONB NOT NULL DEFAULT '{}'::jsonb,
        route_messages JSONB NOT NULL DEFAULT '{}'::jsonb,
        route_reconnects JSONB NOT NULL DEFAULT '{}'::jsonb,
        backpressure_status TEXT NOT NULL DEFAULT 'DEFER_OR_REJECT',
        backpressure_reasons JSONB NOT NULL DEFAULT '["starting"]'::jsonb,
        last_message_at TIMESTAMPTZ,
        last_error TEXT,
        sampled_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_live_intents_status ON quant.paper_live_order_intents (status, decision_ts, intent_id)",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_paired_probes_status ON quant.paper_paired_probes (status, decision_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_paired_probes_asset_time ON quant.paper_paired_probes (asset_id, decision_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_live_checkpoints_asset_time ON quant.paper_live_book_checkpoints (asset_id, observed_at DESC)",
    """CREATE INDEX IF NOT EXISTS idx_quant_paper_live_maker_trades_asset_time
       ON quant.paper_live_maker_trade_events (asset_id, event_ts DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_quant_paper_live_maker_trades_received
       ON quant.paper_live_maker_trade_events (received_at DESC)""",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_live_watchlist_seed ON quant.paper_live_watchlist (strategy_id, enabled, reason)",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_live_target_assignments_active ON quant.paper_live_target_assignments (worker_id, active, asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_execution_catalog_live ON quant.paper_execution_market_catalog (execution_eligible, market_state, active, closed, resolved)",
    """
    ALTER TABLE quant.paper_live_current_books SET (
        fillfactor=50,
        autovacuum_vacuum_scale_factor=0,
        autovacuum_vacuum_threshold=1000,
        autovacuum_analyze_scale_factor=0,
        autovacuum_analyze_threshold=1000
    )
    """,
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS fresh_books INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS execution_watched_assets INTEGER",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS execution_ready_books INTEGER",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS execution_fresh_books INTEGER",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS settled_positions BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS feed_mismatch_assets INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS route_states JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS route_proxy_urls JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS route_messages JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS route_reconnects JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE quant.paper_live_shadow_health ADD COLUMN IF NOT EXISTS backpressure_status TEXT NOT NULL DEFAULT 'DEFER_OR_REJECT'",
    """ALTER TABLE quant.paper_live_shadow_health
       ADD COLUMN IF NOT EXISTS backpressure_reasons JSONB NOT NULL DEFAULT '["starting"]'::jsonb""",
    "ALTER TABLE quant.paper_live_shadow_health_samples ADD COLUMN IF NOT EXISTS settled_positions BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_live_shadow_health_samples ADD COLUMN IF NOT EXISTS execution_watched_assets INTEGER",
    "ALTER TABLE quant.paper_live_shadow_health_samples ADD COLUMN IF NOT EXISTS execution_ready_books INTEGER",
    "ALTER TABLE quant.paper_live_shadow_health_samples ADD COLUMN IF NOT EXISTS execution_fresh_books INTEGER",
    "ALTER TABLE quant.paper_live_shadow_health_samples ADD COLUMN IF NOT EXISTS route_messages JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE quant.paper_live_shadow_health_samples ADD COLUMN IF NOT EXISTS route_reconnects JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE quant.paper_live_shadow_health_samples ADD COLUMN IF NOT EXISTS backpressure_status TEXT NOT NULL DEFAULT 'DEFER_OR_REJECT'",
    """ALTER TABLE quant.paper_live_shadow_health_samples
       ADD COLUMN IF NOT EXISTS backpressure_reasons JSONB NOT NULL DEFAULT '["starting"]'::jsonb""",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS event_id TEXT",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'unknown'",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS enable_neg_risk BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS winning_asset_id TEXT",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS resolution_status TEXT",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS resolution_source TEXT",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS resolved_time TIMESTAMPTZ",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS market_title TEXT",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS end_date TIMESTAMPTZ",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS event_title TEXT",
    "ALTER TABLE quant.paper_execution_market_catalog ADD COLUMN IF NOT EXISTS event_volume NUMERIC",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS tick_size NUMERIC",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS min_order_size NUMERIC",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS amount_unit TEXT NOT NULL DEFAULT 'SHARES'",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS fee_rate NUMERIC",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS fee_exponent NUMERIC",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS fee_taker_only BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS fee_schedule_id TEXT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS fee_schedule_source TEXT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS economics_regime_id TEXT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS builder_code TEXT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS builder_taker_fee_bps INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS builder_maker_fee_bps INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS order_state TEXT NOT NULL DEFAULT 'CREATED'",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS remaining_size NUMERIC",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS remaining_amount NUMERIC",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS submit_request_ts TIMESTAMPTZ",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS submit_arrival_ts TIMESTAMPTZ",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS cancel_request_ts TIMESTAMPTZ",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS cancel_arrival_ts TIMESTAMPTZ",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS cancel_ack_ts TIMESTAMPTZ",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS replace_request_ts TIMESTAMPTZ",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS replace_arrival_ts TIMESTAMPTZ",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS replace_limit_price NUMERIC",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS replace_size NUMERIC",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS replace_intent_id BIGINT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS replaced_from_intent_id BIGINT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS batch_id TEXT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS queue_priority_epoch INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS venue_regime_id TEXT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS venue_regime_source_hash TEXT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS venue_regime_bound_at TIMESTAMPTZ",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS venue_taker_delay_ms INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS venue_delay_source TEXT",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS venue_itode BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE quant.paper_live_order_intents ADD COLUMN IF NOT EXISTS venue_seconds_delay INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_market_terms ADD COLUMN IF NOT EXISTS itode BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE quant.paper_market_terms ADD COLUMN IF NOT EXISTS seconds_delay INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_market_terms ADD COLUMN IF NOT EXISTS taker_delay_ms INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_market_terms ADD COLUMN IF NOT EXISTS delay_source TEXT NOT NULL DEFAULT 'clob_market_no_delay'",
    """
    UPDATE quant.paper_live_order_intents
    SET order_state=CASE
            WHEN status='COMPLETED' THEN COALESCE(result->>'status', 'COMPLETED')
            WHEN status='FAILED' THEN 'REJECTED'
            WHEN status IN ('CANCELED','EXPIRED','WORKING') THEN status
            WHEN status='PROCESSING' THEN 'ACCEPTED'
            ELSE order_state
        END,
        remaining_size=COALESCE(
            remaining_size,
            CASE WHEN result ? 'remaining_size'
                 THEN (result->>'remaining_size')::numeric
                 ELSE size END
        ),
        remaining_amount=COALESCE(
            remaining_amount,
            CASE WHEN result ? 'remaining_amount'
                 THEN (result->>'remaining_amount')::numeric
                 ELSE NULL END
        )
    WHERE order_state='CREATED' OR remaining_size IS NULL
    """,
    """
    INSERT INTO quant.paper_order_events (
        idempotency_key, intent_id, strategy_id, client_order_id,
        event_type, from_state, to_state, reason, result_audit_key,
        checkpoint_id, payload, event_ts
    )
    SELECT 'paper-order:' || intent_id || ':migration-created',
           intent_id, strategy_id, client_order_id,
           'CREATED', NULL, 'CREATED', 'historical_intent_migration',
           NULL, NULL, '{"migration":true}'::jsonb, created_at
    FROM quant.paper_live_order_intents
    ON CONFLICT (idempotency_key) DO NOTHING
    """,
    """
    INSERT INTO quant.paper_order_events (
        idempotency_key, intent_id, strategy_id, client_order_id,
        event_type, from_state, to_state, reason, result_audit_key,
        checkpoint_id, payload, event_ts
    )
    SELECT 'paper-order:' || intent_id || ':migration-terminal',
           intent_id, strategy_id, client_order_id,
           order_state, 'UNKNOWN_HISTORICAL', order_state,
           'historical_terminal_state_migration', result_audit_key,
           result->>'arrival_checkpoint_id', '{"migration":true}'::jsonb,
           COALESCE(completed_at, updated_at)
    FROM quant.paper_live_order_intents
    WHERE status IN ('COMPLETED','FAILED','CANCELED','EXPIRED')
    ON CONFLICT (idempotency_key) DO NOTHING
    """,
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_order_events_intent_time ON quant.paper_order_events (intent_id, event_ts, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_risk_decisions_strategy_time ON quant.paper_risk_decisions (strategy_id, decided_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_shadow_health_samples_time ON quant.paper_live_shadow_health_samples (sampled_at DESC)",
)


@dataclass(frozen=True)
class LiveWatchTarget:
    asset_id: str
    market_id: str
    condition_id: str
    market_slug: str | None
    outcome_name: str
    outcome_index: int
    market_state: str
    execution_eligible: bool
    coverage_grade: str
    has_gap: bool
    refresh_nonce: str | None = None


@dataclass(frozen=True)
class QueuedIntent:
    intent_id: int
    intent: OrderIntent
    execution_profile: ExecutionProfileDecision | None = None


@dataclass(frozen=True)
class MakerQueueAdvancePlan:
    intent_id: int
    intent: OrderIntent
    event_id: str
    event_ts: datetime
    prior_last_event_id: str | None
    prior_last_event_ts: datetime | None
    prior_queue_epoch: int
    next_state: MakerQueueState
    incremental_fill_size: Decimal
    cumulative_filled_size: Decimal
    remaining_size: Decimal
    arrival_checkpoint_id: str | None
    coverage_grade: str | None
    book_generation: int | None
    fidelity: dict[str, Any]
    rebased: bool = False


@dataclass(frozen=True)
class MakerResearchBookEvent:
    event_id: str
    event_kind: str
    asset_id: str
    event_ts: datetime
    book_generation: int
    side: str | None = None
    price: Decimal | None = None
    previous_size: Decimal | None = None
    displayed_size: Decimal | None = None
    bids: tuple[tuple[Decimal, Decimal], ...] = ()
    asks: tuple[tuple[Decimal, Decimal], ...] = ()

    def displayed_at(self, *, side: str, price: Decimal) -> Decimal:
        levels = self.bids if str(side).upper() == "BUY" else self.asks
        if self.event_kind == "BOOK_SNAPSHOT":
            return sum(
                (size for level_price, size in levels if level_price == price),
                Decimal("0"),
            )
        if (
            self.price == price
            and str(self.side or "").upper() == str(side).upper()
            and self.displayed_size is not None
        ):
            return max(Decimal("0"), self.displayed_size)
        return Decimal("0")


class LiveShadowStore:
    def __init__(self, connection_factory: Any = postgres_connection) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)
                conn.commit()
        from quant.simulator.run_artifact_store import (
            PostgresSimulatorArtifactStore,
        )

        PostgresSimulatorArtifactStore(self.connection_factory).ensure_schema()
        from quant.simulator.regime import PostgresVenueRegimeStore

        regime_store = PostgresVenueRegimeStore(self.connection_factory)
        regime_store.ensure_schema()
        regime_store.sync_calibration_regimes()
        from .persistent_event_kernel import PostgresPersistentEventKernelStore

        PostgresPersistentEventKernelStore(
            self.connection_factory,
            partition_key="schema-bootstrap",
        ).ensure_schema()

    def persist_maker_trade_event(
        self,
        *,
        event_id: str,
        worker_id: str,
        asset_id: str,
        price: Decimal,
        size: Decimal,
        aggressor_side: str,
        event_ts: datetime,
        transaction_hash: str | None = None,
    ) -> bool:
        """Persist one normalized public trade exactly once across redundant feeds."""

        side = str(aggressor_side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("aggressor_side must be BUY or SELL")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_live_maker_trade_events (
                    event_id,worker_id,asset_id,price,size,aggressor_side,
                    event_ts,transaction_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    str(event_id),
                    str(worker_id),
                    str(asset_id),
                    Decimal(price),
                    Decimal(size),
                    side,
                    event_ts,
                    str(transaction_hash) if transaction_hash else None,
                ),
            )
            inserted = cur.rowcount == 1
            conn.commit()
            return inserted

    def prune_maker_trade_events(
        self,
        *,
        before: datetime,
        limit: int = 10_000,
    ) -> int:
        """Bound hot maker evidence without touching the durable L2 archive."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH doomed AS (
                    SELECT ctid
                    FROM quant.paper_live_maker_trade_events
                    WHERE event_ts < %s
                    ORDER BY event_ts
                    LIMIT %s
                )
                DELETE FROM quant.paper_live_maker_trade_events existing
                USING doomed
                WHERE existing.ctid=doomed.ctid
                """,
                (before, max(1, int(limit))),
            )
            deleted = int(cur.rowcount or 0)
            conn.commit()
            return deleted

    def maker_trade_event_state(self, event_id: str) -> str | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT processing_state
                FROM quant.paper_live_maker_trade_events
                WHERE event_id=%s
                """,
                (str(event_id),),
            )
            row = cur.fetchone()
            return str(row["processing_state"]) if row else None

    def mark_maker_trade_event_processed(
        self,
        event_id: str,
        *,
        state: str,
        reason: str,
    ) -> bool:
        normalized = str(state).upper()
        if normalized not in {"APPLIED", "SKIPPED_UNSAFE"}:
            raise ValueError("maker trade processing state must be terminal")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_maker_trade_events
                SET processing_state=%s,disposition_reason=%s,
                    processed_at=clock_timestamp()
                WHERE event_id=%s AND processing_state='PENDING'
                """,
                (normalized, str(reason), str(event_id)),
            )
            changed = int(cur.rowcount or 0) == 1
            conn.commit()
            return changed

    def bind_venue_regime(
        self,
        intent_id: int,
        *,
        venue: str = "POLYMARKET",
    ) -> dict[str, Any]:
        """Atomically freeze the point-in-time venue contract on one intent."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT intent_id,decision_ts,venue_regime_id,
                       venue_regime_source_hash,venue_regime_bound_at
                FROM quant.paper_live_order_intents
                WHERE intent_id=%s
                FOR UPDATE
                """,
                (int(intent_id),),
            )
            intent = cur.fetchone()
            if intent is None:
                raise LookupError(f"paper intent {intent_id} does not exist")
            if intent["venue_regime_id"] is not None:
                cur.execute(
                    """
                    SELECT regime_id,source_hash,venue,valid_from,valid_to
                    FROM quant.venue_regime_snapshots
                    WHERE regime_id=%s AND source_hash=%s
                    """,
                    (
                        str(intent["venue_regime_id"]),
                        str(intent["venue_regime_source_hash"]),
                    ),
                )
                frozen = cur.fetchone()
                if frozen is None:
                    raise RuntimeError(
                        "bound venue regime snapshot is missing or changed"
                    )
                return {
                    **dict(frozen),
                    "bound_at": intent["venue_regime_bound_at"],
                }
            cur.execute(
                """
                SELECT regime_id,source_hash,venue,valid_from,valid_to
                FROM quant.venue_regime_snapshots
                WHERE venue=%s AND valid_from<=%s
                  AND (valid_to IS NULL OR valid_to>%s)
                ORDER BY valid_from DESC
                """,
                (str(venue), intent["decision_ts"], intent["decision_ts"]),
            )
            regimes = cur.fetchall()
            if len(regimes) != 1:
                raise LookupError(
                    f"expected one {venue} regime at "
                    f"{intent['decision_ts'].isoformat()}, found {len(regimes)}"
                )
            regime = dict(regimes[0])
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET venue_regime_id=%s,venue_regime_source_hash=%s,
                    venue_regime_bound_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE intent_id=%s AND venue_regime_id IS NULL
                RETURNING venue_regime_bound_at
                """,
                (regime["regime_id"], regime["source_hash"], int(intent_id)),
            )
            bound = cur.fetchone()
            if bound is None:
                raise RuntimeError("venue regime binding lost its row lock")
            _insert_order_event(
                cur,
                idempotency_key=f"paper-order:{int(intent_id)}:venue-regime",
                intent_id=int(intent_id),
                event_type="VENUE_REGIME_BOUND",
                from_state="SUBMIT_QUEUED",
                to_state="SUBMIT_QUEUED",
                reason=str(regime["regime_id"]),
                payload={
                    "regime_id": regime["regime_id"],
                    "source_hash": regime["source_hash"],
                    "venue": regime["venue"],
                    "valid_from": regime["valid_from"],
                    "valid_to": regime["valid_to"],
                    "resolved_at_decision_ts": intent["decision_ts"],
                },
            )
            conn.commit()
            return {**regime, "bound_at": bound["venue_regime_bound_at"]}

    def seed_watchlist(
        self, *, limit: int, strategy_id: str = "paper-live-seed"
    ) -> int:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS acquired",
                (f"quant.paper_live_watchlist:{strategy_id}",),
            )
            if not bool(cur.fetchone()["acquired"]):
                conn.rollback()
                return 0
            requested = max(1, int(limit))
            cur.execute(
                """
                SELECT w.asset_id
                FROM quant.paper_live_watchlist w
                JOIN quant.paper_execution_market_catalog r USING (asset_id)
                WHERE r.active=TRUE AND r.closed=FALSE AND r.resolved=FALSE
                  AND r.archived=FALSE AND r.deprecated=FALSE
                  AND r.market_state='LIVE' AND r.execution_eligible=TRUE
                  AND w.strategy_id=%s AND w.enabled=TRUE
                  AND w.reason IN ('canary_execution_seed','coverage_execution_seed')
                ORDER BY w.created_at, w.asset_id
                LIMIT %s
                """,
                (strategy_id, requested),
            )
            selected_asset_ids = [str(row["asset_id"]) for row in cur.fetchall()]
            missing = requested - len(selected_asset_ids)
            if missing > 0:
                cur.execute(
                    """
                    SELECT r.asset_id
                    FROM quant.paper_execution_market_catalog r
                    WHERE r.market_state='LIVE' AND r.execution_eligible=TRUE
                      AND r.active=TRUE AND r.closed=FALSE AND r.resolved=FALSE
                      AND NOT (r.asset_id=ANY(%s::text[]))
                      AND NOT EXISTS (
                          SELECT 1 FROM quant.paper_live_watchlist existing
                          WHERE existing.asset_id=r.asset_id AND existing.enabled=TRUE
                      )
                    ORDER BY
                      CASE
                        WHEN r.coverage_grade IN ('A_PLUS','A','B')
                         AND r.has_gap=FALSE THEN 0
                        ELSE 1
                      END,
                      r.last_receive_ts DESC NULLS LAST, r.asset_id
                    LIMIT %s
                    """,
                    (selected_asset_ids, missing),
                )
                selected_asset_ids.extend(
                    str(row["asset_id"]) for row in cur.fetchall()
                )
            cur.execute(
                """
                INSERT INTO quant.paper_live_watchlist (asset_id, strategy_id, enabled, reason)
                SELECT selected.asset_id, %s, TRUE, 'coverage_execution_seed'
                FROM unnest(%s::text[]) AS selected(asset_id)
                ON CONFLICT (asset_id) DO UPDATE SET
                    enabled=TRUE, strategy_id=EXCLUDED.strategy_id,
                    reason=EXCLUDED.reason, updated_at=clock_timestamp()
                WHERE (
                    quant.paper_live_watchlist.strategy_id=%s
                    OR quant.paper_live_watchlist.reason IN (
                       'canary_execution_seed', 'coverage_execution_seed'
                    )
                    OR quant.paper_live_watchlist.enabled=FALSE
                ) AND (
                       quant.paper_live_watchlist.enabled IS DISTINCT FROM TRUE
                    OR quant.paper_live_watchlist.strategy_id IS DISTINCT FROM EXCLUDED.strategy_id
                    OR quant.paper_live_watchlist.reason IS DISTINCT FROM EXCLUDED.reason
                )
                """,
                (strategy_id, selected_asset_ids, strategy_id),
            )
            changed = int(cur.rowcount or 0)
            cur.execute(
                """
                UPDATE quant.paper_live_watchlist w
                SET enabled=FALSE, updated_at=clock_timestamp()
                WHERE w.strategy_id=%s
                  AND w.enabled=TRUE
                  AND w.reason IN ('canary_execution_seed','coverage_execution_seed')
                  AND NOT (w.asset_id=ANY(%s::text[]))
                """,
                (strategy_id, selected_asset_ids),
            )
            changed += int(cur.rowcount or 0)
            conn.commit()
        return changed

    def watch(
        self, asset_id: str, *, strategy_id: str, reason: str = "manual_watch"
    ) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_live_watchlist (asset_id, strategy_id, enabled, reason)
                SELECT r.asset_id, %s, TRUE, %s
                FROM quant.paper_execution_market_catalog r
                WHERE r.asset_id=%s
                ON CONFLICT (asset_id) DO UPDATE SET
                    enabled=TRUE, strategy_id=EXCLUDED.strategy_id,
                    reason=EXCLUDED.reason, updated_at=clock_timestamp()
                """,
                (strategy_id, reason, str(asset_id)),
            )
            changed = int(cur.rowcount or 0) > 0
            conn.commit()
        return changed

    def watch_batch(
        self,
        asset_ids: Iterable[str],
        *,
        strategy_id: str,
        reason: str = "manual_watch",
    ) -> int:
        ids = list(
            dict.fromkeys(str(item).strip() for item in asset_ids if str(item).strip())
        )
        if not ids:
            return 0
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_live_watchlist (asset_id, strategy_id, enabled, reason)
                SELECT r.asset_id, %s, TRUE, %s
                FROM quant.paper_execution_market_catalog r
                WHERE r.asset_id=ANY(%s::text[])
                ON CONFLICT (asset_id) DO UPDATE SET
                    enabled=TRUE, strategy_id=EXCLUDED.strategy_id,
                    reason=EXCLUDED.reason, updated_at=clock_timestamp()
                """,
                (str(strategy_id), str(reason), ids),
            )
            changed = int(cur.rowcount or 0)
            conn.commit()
            return changed

    def ensure_calibration_watch_batch(
        self,
        asset_ids: Iterable[str],
        *,
        strategy_id: str,
        reason: str,
    ) -> int:
        """Add calibration targets without taking ownership from another strategy."""

        ids = list(
            dict.fromkeys(str(item).strip() for item in asset_ids if str(item).strip())
        )
        if not ids:
            return 0
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_live_watchlist (
                    asset_id, strategy_id, enabled, reason
                )
                SELECT catalog.asset_id, %s, TRUE, %s
                FROM quant.paper_execution_market_catalog catalog
                WHERE catalog.asset_id=ANY(%s::text[])
                ON CONFLICT (asset_id) DO UPDATE SET
                    enabled=TRUE,
                    strategy_id=CASE
                        WHEN quant.paper_live_watchlist.enabled=FALSE
                          OR quant.paper_live_watchlist.strategy_id='paper-live-seed'
                            THEN EXCLUDED.strategy_id
                        ELSE quant.paper_live_watchlist.strategy_id
                    END,
                    reason=CASE
                        WHEN EXCLUDED.reason='live_probe_candidate'
                            THEN EXCLUDED.reason
                        WHEN quant.paper_live_watchlist.enabled=FALSE
                          OR quant.paper_live_watchlist.strategy_id='paper-live-seed'
                            THEN EXCLUDED.reason
                        ELSE quant.paper_live_watchlist.reason
                    END,
                    updated_at=clock_timestamp()
                """,
                (str(strategy_id), str(reason), ids),
            )
            changed = int(cur.rowcount or 0)
            conn.commit()
            return changed

    def synchronize_calibration_watch_batch(
        self,
        asset_ids: Iterable[str],
        *,
        strategy_id: str,
        reason: str,
    ) -> dict[str, int]:
        """Replace one ephemeral calibration candidate set without touching owners.

        Candidate discovery is periodic.  Keeping every historical candidate
        enabled eventually starves newly discovered assets behind the worker's
        bounded watch limit.  This method owns only rows carrying the exact
        calibration strategy/reason pair; intents, positions, active probes and
        rows owned by another strategy are left alone.
        """

        ids = list(
            dict.fromkeys(str(item).strip() for item in asset_ids if str(item).strip())
        )
        owner = str(strategy_id).strip()
        watch_reason = str(reason).strip()
        if not owner or not watch_reason:
            raise ValueError("calibration watch strategy_id and reason are required")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"quant.paper_live_watchlist:{owner}:{watch_reason}",),
            )
            if ids:
                cur.execute(
                    """
                    INSERT INTO quant.paper_live_watchlist (
                        asset_id, strategy_id, enabled, reason
                    )
                    SELECT catalog.asset_id, %s, TRUE, %s
                    FROM quant.paper_execution_market_catalog catalog
                    WHERE catalog.asset_id=ANY(%s::text[])
                    ON CONFLICT (asset_id) DO UPDATE SET
                        enabled=TRUE,
                        strategy_id=CASE
                            WHEN quant.paper_live_watchlist.enabled=FALSE
                              OR (
                                  quant.paper_live_watchlist.strategy_id LIKE %s
                                  AND quant.paper_live_watchlist.reason=%s
                              )
                              OR quant.paper_live_watchlist.strategy_id='paper-live-seed'
                                THEN EXCLUDED.strategy_id
                            ELSE quant.paper_live_watchlist.strategy_id
                        END,
                        reason=CASE
                            WHEN quant.paper_live_watchlist.enabled=FALSE
                              OR (
                                  quant.paper_live_watchlist.strategy_id LIKE %s
                                  AND quant.paper_live_watchlist.reason=%s
                              )
                              OR quant.paper_live_watchlist.strategy_id='paper-live-seed'
                                THEN EXCLUDED.reason
                            ELSE quant.paper_live_watchlist.reason
                        END,
                        updated_at=clock_timestamp()
                    """,
                    (
                        owner,
                        watch_reason,
                        ids,
                        f"{owner}%",
                        watch_reason,
                        f"{owner}%",
                        watch_reason,
                    ),
                )
                upserted = int(cur.rowcount or 0)
            else:
                upserted = 0
            cur.execute(
                """
                UPDATE quant.paper_live_watchlist w
                SET enabled=FALSE, updated_at=clock_timestamp()
                WHERE w.strategy_id LIKE %s
                  AND w.reason=%s
                  AND w.enabled=TRUE
                  AND NOT (w.asset_id=ANY(%s::text[]))
                  AND NOT EXISTS (
                      SELECT 1
                      FROM quant.paper_live_order_intents intent
                      WHERE intent.asset_id=w.asset_id
                        AND intent.status IN ('QUEUED','PROCESSING','WORKING')
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM quant.paper_positions position
                      WHERE position.asset_id=w.asset_id
                        AND position.quantity > 0
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM quant.paper_calibration_pnl_positions calibration
                      WHERE calibration.asset_id=w.asset_id
                        AND (
                            calibration.real_quantity > 0
                            OR calibration.paper_quantity > 0
                        )
                  )
                """,
                (f"{owner}%", watch_reason, ids),
            )
            disabled = int(cur.rowcount or 0)
            conn.commit()
        return {"upserted": upserted, "disabled": disabled}

    def request_book_refresh(self, asset_id: str) -> bool:
        """Touch an existing watch target so the live service resubscribes it."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_watchlist
                SET refresh_requested_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE asset_id=%s AND enabled=TRUE
                RETURNING asset_id
                """,
                (str(asset_id),),
            )
            changed = cur.fetchone() is not None
            conn.commit()
            return changed

    def refresh_official_probe_market_gate(
        self,
        *,
        asset_id: str,
        condition_id: str,
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Refresh one stale catalog gate from a fresh official CLOB response."""

        market_state = str(snapshot.get("market_state") or "").upper()
        execution_eligible = bool(snapshot.get("execution_eligible"))
        if market_state not in {"LIVE", "CLOSING"}:
            raise ValueError("official probe market state is invalid")
        if execution_eligible and market_state != "LIVE":
            raise ValueError("only a LIVE official market can be execution eligible")
        evidence = {
            "source": "official_clob_live_probe",
            "asset_id": str(asset_id),
            "condition_id": str(condition_id),
            "market_state": market_state,
            "execution_eligible": execution_eligible,
            "tick_size": snapshot.get("tick_size"),
            "min_order_size": snapshot.get("min_order_size"),
            "observed_at": snapshot.get("observed_at"),
        }
        evidence["payload_hash"] = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_execution_market_catalog
                SET market_state=%s,
                    execution_eligible=%s,
                    active=CASE WHEN %s='LIVE' THEN TRUE ELSE active END,
                    closed=CASE WHEN %s='LIVE' THEN FALSE ELSE closed END,
                    resolved=CASE WHEN %s='LIVE' THEN FALSE ELSE resolved END,
                    current_tick_size=COALESCE(%s::numeric,current_tick_size),
                    min_order_size=COALESCE(%s::numeric,min_order_size),
                    end_date=COALESCE(%s::timestamptz,end_date),
                    raw_metadata=COALESCE(raw_metadata,'{}'::jsonb)
                        || jsonb_build_object('_official_probe_gate',%s::jsonb),
                    source_updated_at=clock_timestamp(),
                    synced_at=clock_timestamp()
                WHERE asset_id=%s AND condition_id=%s
                RETURNING asset_id,condition_id,market_state,execution_eligible,
                          current_tick_size,min_order_size,source_updated_at
                """,
                (
                    market_state,
                    execution_eligible,
                    market_state,
                    market_state,
                    market_state,
                    snapshot.get("tick_size"),
                    snapshot.get("min_order_size"),
                    snapshot.get("end_date"),
                    json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                    str(asset_id),
                    str(condition_id),
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError("official probe asset is absent from the Paper catalog")
            conn.commit()
            return {**dict(row), "evidence": evidence}

    def request_book_refresh_batch(self, asset_ids: Iterable[str]) -> list[str]:
        """Touch several watch targets in one transaction."""

        ids = list(
            dict.fromkeys(str(item).strip() for item in asset_ids if str(item).strip())
        )
        if not ids:
            return []
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_watchlist
                SET refresh_requested_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE asset_id=ANY(%s::text[]) AND enabled=TRUE
                RETURNING asset_id
                """,
                (ids,),
            )
            changed = [str(row["asset_id"]) for row in cur.fetchall()]
            conn.commit()
            return changed

    def replace_target_assignments(
        self,
        *,
        worker_id: str,
        asset_ids: Iterable[str],
    ) -> None:
        """Persist the exact asset set currently owned by one paper worker."""

        ids = list(
            dict.fromkeys(str(item).strip() for item in asset_ids if str(item).strip())
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_target_assignments
                SET active=FALSE, removed_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE worker_id=%s AND active=TRUE
                  AND NOT (asset_id=ANY(%s::text[]))
                """,
                (str(worker_id), ids),
            )
            if ids:
                cur.execute(
                    """
                    INSERT INTO quant.paper_live_target_assignments (
                        worker_id, asset_id, active, assigned_at,
                        removed_at, updated_at
                    )
                    SELECT %s, selected.asset_id, TRUE, clock_timestamp(),
                           NULL, clock_timestamp()
                    FROM unnest(%s::text[]) AS selected(asset_id)
                    ON CONFLICT (worker_id, asset_id) DO UPDATE SET
                        active=TRUE,
                        assigned_at=CASE
                            WHEN quant.paper_live_target_assignments.active
                                THEN quant.paper_live_target_assignments.assigned_at
                            ELSE clock_timestamp()
                        END,
                        removed_at=NULL,
                        updated_at=CASE
                            WHEN quant.paper_live_target_assignments.active
                                THEN quant.paper_live_target_assignments.updated_at
                            ELSE clock_timestamp()
                        END
                    WHERE quant.paper_live_target_assignments.active=FALSE
                    """,
                    (str(worker_id), ids),
                )
            conn.commit()

    def load_watch_targets(self, *, limit: int) -> list[LiveWatchTarget]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH requested AS (
                    SELECT w.asset_id, w.strategy_id, w.reason, w.updated_at,
                           CASE
                               WHEN w.reason IN ('intent_asset', 'manual_watch')
                                 OR w.strategy_id <> 'paper-live-seed' THEN 2
                               ELSE 3
                           END AS priority
                    FROM quant.paper_live_watchlist w
                    WHERE w.enabled=TRUE
                    UNION ALL
                    SELECT i.asset_id, i.strategy_id, 'active_intent', i.updated_at, 0
                    FROM quant.paper_live_order_intents i
                    WHERE i.status IN ('QUEUED', 'PROCESSING', 'WORKING')
                    UNION ALL
                    SELECT p.asset_id, p.strategy_id, 'open_position', p.updated_at, 1
                    FROM quant.paper_positions p
                    WHERE p.quantity > 0
                    UNION ALL
                    SELECT p.asset_id, 'calibration-holdout',
                           'calibration_open_position', p.updated_at, 1
                    FROM quant.paper_calibration_pnl_positions p
                    WHERE p.real_quantity > 0 OR p.paper_quantity > 0
                ), desired AS (
                    SELECT DISTINCT ON (asset_id)
                           asset_id, strategy_id, reason, updated_at, priority
                    FROM requested
                    ORDER BY asset_id, priority, updated_at DESC
                )
                SELECT w.asset_id,
                       r.market_id,
                       COALESCE(r.condition_id, '') AS condition_id,
                       r.market_slug, COALESCE(r.outcome_name, 'UNKNOWN') AS outcome_name,
                       COALESCE(r.outcome_index, 0) AS outcome_index,
                       COALESCE(r.market_state, 'DISCOVERED') AS market_state,
                       COALESCE(r.execution_eligible, FALSE) AS execution_eligible,
                       COALESCE(r.coverage_grade, 'D') AS coverage_grade,
                       COALESCE(r.has_gap, TRUE) AS has_gap,
                       refresh.refresh_requested_at::text AS refresh_nonce
                FROM desired w
                JOIN quant.paper_execution_market_catalog r USING (asset_id)
                LEFT JOIN quant.paper_live_watchlist refresh USING (asset_id)
                WHERE COALESCE(r.active, FALSE)=TRUE
                  AND COALESCE(r.closed, FALSE)=FALSE
                  AND COALESCE(r.resolved, FALSE)=FALSE
                  AND COALESCE(r.archived, FALSE)=FALSE
                  AND COALESCE(r.deprecated, FALSE)=FALSE
                  AND (
                      (
                          r.market_state='LIVE'
                          AND COALESCE(r.execution_eligible, FALSE)=TRUE
                      )
                      OR w.reason IN (
                          'active_intent', 'open_position',
                          'calibration_open_position',
                          'live_probe_candidate'
                      )
                  )
                ORDER BY w.priority,
                         COALESCE(r.execution_eligible, FALSE) DESC,
                         w.updated_at DESC, w.asset_id
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
            return [LiveWatchTarget(**dict(row)) for row in cur.fetchall()]

    def submit(
        self,
        *,
        strategy_id: str,
        client_order_id: str,
        asset_id: str,
        side: str,
        time_in_force: str,
        limit_price: Decimal,
        size: Decimal,
        post_only: bool,
        decision_ts: datetime,
        amount_unit: str = "SHARES",
        fee_rate: Decimal | None = None,
        fee_exponent: Decimal | None = None,
        fee_taker_only: bool = True,
        builder_code: str | None = None,
        builder_taker_fee_bps: int = 0,
        builder_maker_fee_bps: int = 0,
        expires_at: datetime | None = None,
        initial_status: str = "QUEUED",
    ) -> int:
        return self.submit_batch(
            [
                {
                    "strategy_id": strategy_id,
                    "client_order_id": client_order_id,
                    "asset_id": asset_id,
                    "side": side,
                    "time_in_force": time_in_force,
                    "limit_price": limit_price,
                    "size": size,
                    "post_only": post_only,
                    "decision_ts": decision_ts,
                    "amount_unit": amount_unit,
                    "fee_rate": fee_rate,
                    "fee_exponent": fee_exponent,
                    "fee_taker_only": fee_taker_only,
                    "builder_code": builder_code,
                    "builder_taker_fee_bps": builder_taker_fee_bps,
                    "builder_maker_fee_bps": builder_maker_fee_bps,
                    "expires_at": expires_at,
                    "initial_status": initial_status,
                }
            ]
        )[0]

    def submit_batch(
        self,
        submissions: Iterable[dict[str, Any]],
        *,
        batch_id: str | None = None,
        config_hash: str | None = None,
    ) -> list[int]:
        """Atomically enqueue multiple paper intents.

        This is intentionally a paper control-plane operation.  It lets a
        multi-leg strategy make both legs visible to the live shadow worker in
        one commit without implying atomic fills at the simulated exchange.
        """

        orders = [dict(item) for item in submissions]
        if not orders:
            raise ValueError("at least one paper submission is required")
        durable_batch = batch_id is not None or config_hash is not None
        if durable_batch and (
            not str(batch_id or "").strip() or not str(config_hash or "").strip()
        ):
            raise ValueError("durable paper batch requires batch_id and config_hash")
        normalized: list[dict[str, Any]] = []
        for order in orders:
            side = str(order.get("side") or "").upper()
            tif = str(order.get("time_in_force") or "").upper()
            unit = str(order.get("amount_unit") or "SHARES").upper()
            initial_status = str(order.get("initial_status") or "QUEUED").upper()
            limit_price = Decimal(str(order.get("limit_price")))
            size = Decimal(str(order.get("size")))
            if side not in {"BUY", "SELL"} or tif not in {"GTC", "GTD", "FOK", "FAK"}:
                raise ValueError("invalid side or time_in_force")
            if unit not in {"SHARES", "QUOTE"}:
                raise ValueError("amount_unit must be SHARES or QUOTE")
            if initial_status not in {"QUEUED", "PENDING_OWNERSHIP"}:
                raise ValueError("initial_status must be QUEUED or PENDING_OWNERSHIP")
            if side == "SELL" and unit != "SHARES":
                raise ValueError("SELL amount_unit must be SHARES")
            if tif in {"GTC", "GTD"} and unit != "SHARES":
                raise ValueError("GTC/GTD amount_unit must be SHARES")
            if size <= 0 or not Decimal("0") < limit_price < Decimal("1"):
                raise ValueError(
                    "size must be positive and limit_price must be between 0 and 1"
                )
            required = ("strategy_id", "client_order_id", "asset_id", "decision_ts")
            missing = [key for key in required if order.get(key) in (None, "")]
            if missing:
                raise ValueError(
                    f"paper submission missing required fields: {', '.join(missing)}"
                )
            if tif == "GTD":
                expiration_error = validate_gtd_expiration(
                    order["decision_ts"],
                    order.get("expires_at"),
                )
                if expiration_error is not None:
                    raise ValueError(expiration_error)
            normalized.append(
                {
                    **order,
                    "side": side,
                    "time_in_force": tif,
                    "amount_unit": unit,
                    "initial_status": initial_status,
                    "limit_price": limit_price,
                    "size": size,
                    "post_only": bool(order.get("post_only", False)),
                    "fee_taker_only": bool(order.get("fee_taker_only", True)),
                    "builder_taker_fee_bps": int(
                        order.get("builder_taker_fee_bps") or 0
                    ),
                    "builder_maker_fee_bps": int(
                        order.get("builder_maker_fee_bps") or 0
                    ),
                }
            )
            if not 0 <= normalized[-1]["builder_taker_fee_bps"] <= 100:
                raise ValueError("builder taker fee must be within 0..100 bps")
            if not 0 <= normalized[-1]["builder_maker_fee_bps"] <= 50:
                raise ValueError("builder maker fee must be within 0..50 bps")
        if (
            durable_batch
            and len({str(order["strategy_id"]) for order in normalized}) != 1
        ):
            raise ValueError("durable paper batch requires one strategy/account")

        intent_ids: list[int] = []
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for order in normalized:
                cur.execute(
                    """
                    INSERT INTO quant.paper_live_watchlist (asset_id, strategy_id, enabled, reason)
                    SELECT asset_id, %s, TRUE, 'intent_asset'
                    FROM quant.paper_execution_market_catalog WHERE asset_id=%s
                    ON CONFLICT (asset_id) DO UPDATE SET
                        enabled=TRUE, strategy_id=EXCLUDED.strategy_id,
                        reason=EXCLUDED.reason, updated_at=clock_timestamp()
                    """,
                    (str(order["strategy_id"]), str(order["asset_id"])),
                )
                cur.execute(
                    """
                    INSERT INTO quant.paper_live_order_intents (
                        strategy_id, client_order_id, market_id, condition_id, asset_id,
                        side, time_in_force, limit_price, size, amount_unit, tick_size, min_order_size,
                        fee_rate, fee_exponent, fee_taker_only,
                        builder_code,builder_taker_fee_bps,builder_maker_fee_bps,
                        post_only, decision_ts, expires_at, remaining_size, status
                    )
                    SELECT %s, %s,
                           r.market_id,
                           r.condition_id, r.asset_id,
                           %s, %s, %s, %s, %s, r.current_tick_size, r.min_order_size,
                           %s, %s, %s,
                           %s, %s, %s,
                           %s, %s, %s, %s, %s
                    FROM quant.paper_execution_market_catalog r
                    WHERE r.asset_id=%s
                    ON CONFLICT (strategy_id, client_order_id) DO UPDATE SET
                        updated_at=quant.paper_live_order_intents.updated_at
                    RETURNING intent_id
                    """,
                    (
                        str(order["strategy_id"]),
                        str(order["client_order_id"]),
                        order["side"],
                        order["time_in_force"],
                        order["limit_price"],
                        order["size"],
                        order["amount_unit"],
                        order.get("fee_rate"),
                        order.get("fee_exponent"),
                        order["fee_taker_only"],
                        order.get("builder_code"),
                        order["builder_taker_fee_bps"],
                        order["builder_maker_fee_bps"],
                        order["post_only"],
                        order["decision_ts"],
                        order.get("expires_at"),
                        order["size"],
                        order["initial_status"],
                        str(order["asset_id"]),
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError(f"unknown registry asset_id: {order['asset_id']}")
                intent_id = int(row["intent_id"])
                intent_ids.append(intent_id)
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{intent_id}:created",
                    intent_id=intent_id,
                    event_type="CREATED",
                    from_state=None,
                    to_state="CREATED",
                    reason="paper_intent_submitted",
                )
            if durable_batch:
                selected_batch_id = str(batch_id)
                selected_config_hash = str(config_hash)
                strategy_id = str(normalized[0]["strategy_id"])
                cur.execute(
                    """
                    SELECT account_id,strategy_id,child_count,config_hash
                    FROM quant.paper_order_batches
                    WHERE batch_id=%s
                    FOR UPDATE
                    """,
                    (selected_batch_id,),
                )
                existing_batch = cur.fetchone()
                expected_identity = (
                    strategy_id,
                    strategy_id,
                    len(intent_ids),
                    selected_config_hash,
                )
                if existing_batch is None:
                    cur.execute(
                        """
                        INSERT INTO quant.paper_order_batches (
                            batch_id,account_id,strategy_id,submitted_ts_ns,
                            arrival_ts_ns,child_count,terminal_count,batch_state,
                            config_hash
                        ) VALUES (%s,%s,%s,%s,NULL,%s,0,'QUEUED',%s)
                        """,
                        (
                            selected_batch_id,
                            strategy_id,
                            strategy_id,
                            min(
                                int(order["decision_ts"].timestamp() * 1_000_000_000)
                                for order in normalized
                            ),
                            len(intent_ids),
                            selected_config_hash,
                        ),
                    )
                elif tuple(existing_batch.values()) != expected_identity:
                    raise ValueError("paper batch identity changed on replay")

                for child_index, (intent_id, order) in enumerate(
                    zip(intent_ids, normalized, strict=True)
                ):
                    cur.execute(
                        """
                        UPDATE quant.paper_live_order_intents
                        SET batch_id=%s, updated_at=clock_timestamp()
                        WHERE intent_id=%s AND (batch_id IS NULL OR batch_id=%s)
                        RETURNING intent_id
                        """,
                        (selected_batch_id, intent_id, selected_batch_id),
                    )
                    if cur.fetchone() is None:
                        raise ValueError(
                            "paper intent already belongs to another batch"
                        )
                    command_id = f"paper-shadow:{intent_id}"
                    cur.execute(
                        """
                        INSERT INTO quant.paper_order_batch_children (
                            batch_id,child_index,intent_id,command_id,order_id,
                            disposition,command_state,reason,ledger_eligible,
                            ledger_status,result_json
                        ) VALUES (
                            %s,%s,%s,%s,%s,'PENDING','CREATED',
                            'paper_batch_enqueued',FALSE,'WAITING_ADMISSION',
                            %s::jsonb
                        )
                        ON CONFLICT (batch_id,child_index) DO UPDATE SET
                            updated_at=clock_timestamp()
                        WHERE quant.paper_order_batch_children.intent_id=EXCLUDED.intent_id
                          AND quant.paper_order_batch_children.command_id=EXCLUDED.command_id
                        """,
                        (
                            selected_batch_id,
                            child_index,
                            intent_id,
                            command_id,
                            str(order["client_order_id"]),
                            json.dumps(
                                {
                                    "intent_id": intent_id,
                                    "strategy_id": strategy_id,
                                    "client_order_id": str(order["client_order_id"]),
                                    "paper_batch": True,
                                }
                            ),
                        ),
                    )
                    if int(cur.rowcount or 0) != 1:
                        raise ValueError("paper batch child identity changed on replay")
            conn.commit()
        return intent_ids

    def record_batch_admission(
        self,
        intent_id: int,
        *,
        paper_admitted: bool,
        venue_shadow: Any | None,
    ) -> bool:
        """Attach independent paper/gateway admission evidence to one batch child."""

        disposition = "PAPER_ADMITTED" if paper_admitted else "PAPER_DENIED"
        command_state = (
            str(getattr(venue_shadow, "command_state", "") or "RISK_ACCEPTED")
            if paper_admitted
            else "LOCAL_DENIED"
        )
        reason = str(
            getattr(venue_shadow, "reason", "")
            or ("paper_risk_accepted" if paper_admitted else "paper_risk_denied")
        )
        payload = {
            "paper_admitted": bool(paper_admitted),
            "gateway_disposition": getattr(venue_shadow, "disposition", None),
            "gateway_command_state": getattr(venue_shadow, "command_state", None),
            "gateway_agreement": getattr(venue_shadow, "agreement", None),
            "gateway_reason": getattr(venue_shadow, "reason", None),
        }
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_order_batch_children
                SET disposition=%s,command_state=%s,reason=%s,
                    ledger_eligible=%s,
                    ledger_status=CASE
                        WHEN ledger_status IN ('APPLIED','NO_EFFECT')
                        THEN ledger_status
                        WHEN %s THEN 'PENDING' ELSE 'NOT_ELIGIBLE'
                    END,
                    result_json=result_json || %s::jsonb,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s
                RETURNING batch_id
                """,
                (
                    disposition,
                    command_state,
                    reason,
                    bool(paper_admitted),
                    bool(paper_admitted),
                    json.dumps({"admission": payload}, default=str),
                    int(intent_id),
                ),
            )
            row = cur.fetchone()
            if row is not None:
                self._refresh_batch_state(cur, str(row["batch_id"]))
            conn.commit()
            return row is not None

    def record_batch_execution(
        self,
        intent_id: int,
        result: PaperExecutionResult,
    ) -> bool:
        successful_fill = result.status in {"FILLED", "PARTIAL"} and bool(result.fills)
        ledger_status = "APPLIED" if successful_fill else "NO_EFFECT"
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_order_batch_children
                SET execution_status=%s,audit_key=%s,ledger_status=%s,
                    result_json=result_json || %s::jsonb,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s
                RETURNING batch_id
                """,
                (
                    result.status,
                    result.audit_key,
                    ledger_status,
                    json.dumps(
                        {
                            "execution": {
                                "status": result.status,
                                "audit_key": result.audit_key,
                                "filled_size": str(result.filled_size),
                                "filled_notional": str(result.filled_notional),
                                "total_fee": str(result.total_fee),
                            }
                        }
                    ),
                    int(intent_id),
                ),
            )
            row = cur.fetchone()
            if row is not None:
                self._refresh_batch_state(cur, str(row["batch_id"]))
            conn.commit()
            return row is not None

    def record_batch_failure(self, intent_id: int, error: str) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_order_batch_children
                SET execution_status='WORKER_FAILED_UNKNOWN',
                    ledger_status='RECONCILIATION_REQUIRED',
                    reason=%s,
                    result_json=result_json || %s::jsonb,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s
                RETURNING batch_id
                """,
                (
                    str(error)[:1000],
                    json.dumps(
                        {
                            "worker_failure": str(error)[:1000],
                            "blind_retry_allowed": False,
                        }
                    ),
                    int(intent_id),
                ),
            )
            row = cur.fetchone()
            if row is not None:
                self._refresh_batch_state(cur, str(row["batch_id"]))
            conn.commit()
            return row is not None

    @staticmethod
    def _refresh_batch_state(cur: Any, batch_id: str) -> None:
        cur.execute(
            """
            WITH state AS (
                SELECT count(*) AS child_count,
                       count(*) FILTER (
                           WHERE execution_status IS NOT NULL
                       ) AS terminal_count,
                       bool_or(
                           ledger_status='RECONCILIATION_REQUIRED'
                       ) AS needs_reconciliation,
                       bool_or(ledger_status='APPLIED') AS has_applied,
                       bool_or(ledger_status='NO_EFFECT') AS has_no_effect
                FROM quant.paper_order_batch_children
                WHERE batch_id=%s
            )
            UPDATE quant.paper_order_batches b
            SET terminal_count=state.terminal_count,
                batch_state=CASE
                    WHEN state.needs_reconciliation
                    THEN 'RECONCILIATION_REQUIRED'
                    WHEN state.terminal_count < state.child_count
                    THEN 'PROCESSING'
                    WHEN state.has_applied AND state.has_no_effect
                    THEN 'PARTIAL_RESULT'
                    ELSE 'COMPLETED'
                END,
                updated_at=clock_timestamp()
            FROM state
            WHERE b.batch_id=%s
            """,
            (str(batch_id), str(batch_id)),
        )

    def load_intent(self, intent_id: int) -> dict[str, Any] | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.paper_live_order_intents WHERE intent_id=%s",
                (int(intent_id),),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def load_market_terms(
        self,
        asset_id: str,
        *,
        now: datetime,
    ) -> PaperMarketTerms | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT asset_id, condition_id, fee_rate_bps, fee_rate,
                       fee_exponent, fee_taker_only, itode, seconds_delay,
                       taker_delay_ms, delay_source, source,
                       observed_at, expires_at
                FROM quant.paper_market_terms
                WHERE asset_id=%s AND expires_at>%s
                """,
                (str(asset_id), now),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return PaperMarketTerms(
            asset_id=str(row["asset_id"]),
            condition_id=str(row["condition_id"]),
            fee_rate_bps=int(row["fee_rate_bps"]),
            fee_rate=Decimal(str(row["fee_rate"])),
            fee_exponent=Decimal(str(row["fee_exponent"])),
            fee_taker_only=bool(row["fee_taker_only"]),
            itode=bool(row["itode"]),
            seconds_delay=int(row["seconds_delay"]),
            taker_delay_ms=int(row["taker_delay_ms"]),
            delay_source=str(row["delay_source"]),
            source=str(row["source"]),
            observed_at=row["observed_at"],
            expires_at=row["expires_at"],
        )

    def upsert_market_terms(self, terms: PaperMarketTerms) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_market_terms (
                    asset_id, condition_id, fee_rate_bps, fee_rate,
                    fee_exponent, fee_taker_only, itode, seconds_delay,
                    taker_delay_ms, delay_source, source, observed_at, expires_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id) DO UPDATE SET
                    condition_id=EXCLUDED.condition_id,
                    fee_rate_bps=EXCLUDED.fee_rate_bps,
                    fee_rate=EXCLUDED.fee_rate,
                    fee_exponent=EXCLUDED.fee_exponent,
                    fee_taker_only=EXCLUDED.fee_taker_only,
                    itode=EXCLUDED.itode,
                    seconds_delay=EXCLUDED.seconds_delay,
                    taker_delay_ms=EXCLUDED.taker_delay_ms,
                    delay_source=EXCLUDED.delay_source,
                    source=EXCLUDED.source,
                    observed_at=EXCLUDED.observed_at,
                    expires_at=EXCLUDED.expires_at,
                    updated_at=clock_timestamp()
                """,
                (
                    terms.asset_id,
                    terms.condition_id,
                    terms.fee_rate_bps,
                    terms.fee_rate,
                    terms.fee_exponent,
                    terms.fee_taker_only,
                    terms.itode,
                    terms.seconds_delay,
                    terms.taker_delay_ms,
                    terms.delay_source,
                    terms.source,
                    terms.observed_at,
                    terms.expires_at,
                ),
            )
            schedule = terms.fee_schedule()
            cur.execute(
                """
                INSERT INTO quant.paper_fee_schedules (
                    fee_schedule_id,asset_id,condition_id,effective_from,
                    effective_until,platform_fee_rate,platform_fee_exponent,
                    platform_taker_only,rounding_policy,economics_regime_id,source
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (fee_schedule_id) DO NOTHING
                """,
                (
                    schedule.schedule_id,
                    schedule.asset_id,
                    schedule.condition_id,
                    schedule.effective_from,
                    schedule.effective_until,
                    schedule.platform_fee_rate,
                    schedule.platform_fee_exponent,
                    schedule.platform_taker_only,
                    schedule.rounding_policy,
                    schedule.regime_id,
                    schedule.source,
                ),
            )
            conn.commit()

    def persist_intent_market_terms(
        self,
        intent_id: int,
        terms: PaperMarketTerms,
    ) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET fee_rate=%s, fee_exponent=%s, fee_taker_only=%s,
                    fee_schedule_id=%s,fee_schedule_source=%s,
                    economics_regime_id=%s,
                    venue_taker_delay_ms=%s,venue_delay_source=%s,
                    venue_itode=%s,venue_seconds_delay=%s,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s
                """,
                (
                    terms.fee_rate,
                    terms.fee_exponent,
                    terms.fee_taker_only,
                    terms.schedule_id,
                    terms.source,
                    terms.schedule_id,
                    terms.taker_delay_ms,
                    terms.delay_source,
                    terms.itode,
                    terms.seconds_delay,
                    int(intent_id),
                ),
            )
            _insert_order_event(
                cur,
                idempotency_key=(
                    f"paper-order:{int(intent_id)}:market-terms:"
                    f"{terms.observed_at.isoformat()}"
                ),
                intent_id=int(intent_id),
                event_type="MARKET_TERMS_RESOLVED",
                from_state="SUBMIT_QUEUED",
                to_state="SUBMIT_QUEUED",
                reason=terms.source,
                payload={
                    "fee_rate_bps": terms.fee_rate_bps,
                    "fee_rate": terms.fee_rate,
                    "fee_exponent": terms.fee_exponent,
                    "fee_taker_only": terms.fee_taker_only,
                    "fee_schedule_id": terms.schedule_id,
                    "economics_regime_id": terms.schedule_id,
                    "itode": terms.itode,
                    "seconds_delay": terms.seconds_delay,
                    "taker_delay_ms": terms.taker_delay_ms,
                    "delay_source": terms.delay_source,
                    "expires_at": terms.expires_at,
                },
            )
            conn.commit()

    def persist_risk_decision(
        self,
        intent_id: int,
        decision: Any,
    ) -> None:
        payload = decision.as_dict()
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_risk_decisions (
                    intent_id, strategy_id, client_order_id, status, reasons,
                    reduce_only, order_notional, metrics
                )
                SELECT i.intent_id, i.strategy_id, i.client_order_id,
                       %s, %s, %s, %s, %s::jsonb
                FROM quant.paper_live_order_intents i
                WHERE i.intent_id=%s
                ON CONFLICT (intent_id) DO UPDATE SET
                    status=EXCLUDED.status,
                    reasons=EXCLUDED.reasons,
                    reduce_only=EXCLUDED.reduce_only,
                    order_notional=EXCLUDED.order_notional,
                    metrics=EXCLUDED.metrics,
                    decided_at=clock_timestamp()
                """,
                (
                    str(decision.status),
                    list(decision.reasons),
                    bool(decision.reduce_only),
                    decision.order_notional,
                    json.dumps(_json_value(payload)),
                    int(intent_id),
                ),
            )
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET order_state=%s, updated_at=clock_timestamp()
                WHERE intent_id=%s AND status='PROCESSING'
                """,
                (
                    "RISK_ACCEPTED" if decision.accepted else "REJECTED",
                    int(intent_id),
                ),
            )
            _insert_order_event(
                cur,
                idempotency_key=f"paper-order:{int(intent_id)}:risk-decision",
                intent_id=int(intent_id),
                event_type="RISK_DECISION",
                from_state="SUBMIT_QUEUED",
                to_state=("RISK_ACCEPTED" if decision.accepted else "REJECTED"),
                reason=",".join(decision.reasons) if decision.reasons else "accepted",
                payload=payload,
            )
            conn.commit()

    def freeze_execution_profile_decision(
        self,
        decision: ExecutionProfileDecision,
    ) -> ExecutionProfileDecision:
        """Persist the first profile decision for an intent and never rewrite it."""

        if not decision.hash_is_valid:
            raise ValueError("execution profile decision hash is invalid")
        payload = decision.as_dict()
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_execution_profile_decisions (
                    intent_id,decision_hash,profile,execution_allowed,
                    resolver_version,execution_model_version,execution_config_hash,
                    fee_schedule_version,latency_model_version,queue_model_version,
                    book_checkpoint_id,
                    book_generation,coverage_grade,calibration_domain,depth_haircut,
                    reason_codes,decision
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                )
                ON CONFLICT (intent_id) DO NOTHING
                """,
                (
                    decision.intent_id,
                    decision.decision_hash,
                    decision.profile.value,
                    decision.execution_allowed,
                    decision.resolver_version,
                    decision.execution_model_version,
                    decision.execution_config_hash,
                    decision.fee_schedule_version,
                    decision.latency_model_version,
                    decision.queue_model_version,
                    decision.book_checkpoint_id,
                    decision.book_generation,
                    decision.coverage_grade,
                    decision.calibration_domain,
                    decision.depth_haircut,
                    list(decision.reason_codes),
                    json.dumps(_json_value(payload)),
                ),
            )
            inserted = int(cur.rowcount or 0) == 1
            cur.execute(
                """
                SELECT decision
                FROM quant.paper_execution_profile_decisions
                WHERE intent_id=%s
                """,
                (decision.intent_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("execution profile decision was not persisted")
            frozen = ExecutionProfileDecision.from_dict(dict(row["decision"]))
            if not frozen.hash_is_valid:
                raise RuntimeError("persisted execution profile decision hash mismatch")
            if inserted:
                _insert_order_event(
                    cur,
                    idempotency_key=(
                        f"paper-order:{decision.intent_id}:execution-profile-frozen"
                    ),
                    intent_id=decision.intent_id,
                    event_type="EXECUTION_PROFILE_FROZEN",
                    from_state="RISK_ACCEPTED",
                    to_state="RISK_ACCEPTED",
                    reason=frozen.profile.value,
                    checkpoint_id=frozen.book_checkpoint_id,
                    payload=payload,
                )
            conn.commit()
            return frozen

    def load_execution_profile_decision(
        self,
        intent_id: int,
    ) -> ExecutionProfileDecision | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT decision
                FROM quant.paper_execution_profile_decisions
                WHERE intent_id=%s
                """,
                (int(intent_id),),
            )
            row = cur.fetchone()
        if row is None:
            return None
        decision = ExecutionProfileDecision.from_dict(dict(row["decision"]))
        if not decision.hash_is_valid:
            raise RuntimeError("persisted execution profile decision hash mismatch")
        return decision

    def persist_execution_tca(
        self,
        intent_id: int,
        result: Any,
        *,
        decision_checkpoint: Any | None,
        arrival_checkpoint: Any | None,
        risk_decision: Any,
    ) -> dict[str, Any]:
        from quant.simulator.analytics import build_paper_tca_artifact
        from quant.simulator.run_artifact_store import PostgresSimulatorArtifactStore

        artifact = build_paper_tca_artifact(
            intent_id=int(intent_id),
            result=result,
            decision_checkpoint=decision_checkpoint,
            arrival_checkpoint=arrival_checkpoint,
            risk_decision=risk_decision,
        )
        PostgresSimulatorArtifactStore(self.connection_factory).persist_paper_tca(
            artifact
        )
        return artifact.as_dict()

    def mark_submit_arrival(
        self,
        intent_id: int,
        *,
        request_ts: datetime,
        arrival_ts: datetime,
    ) -> bool:
        if arrival_ts < request_ts:
            raise ValueError("submit arrival precedes request")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET order_state='VENUE_ACCEPTED',
                    submit_request_ts=COALESCE(submit_request_ts,%s),
                    submit_arrival_ts=%s,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s
                  AND status='PROCESSING'
                  AND (
                    cancel_arrival_ts IS NULL
                    OR cancel_arrival_ts > %s
                  )
                RETURNING intent_id
                """,
                (request_ts, arrival_ts, int(intent_id), arrival_ts),
            )
            changed = cur.fetchone() is not None
            if changed:
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{intent_id}:submit-inflight",
                    intent_id=int(intent_id),
                    event_type="SUBMIT_INFLIGHT",
                    from_state="RISK_ACCEPTED",
                    to_state="SUBMIT_INFLIGHT",
                    reason="paper_submit_latency_scheduled",
                    payload={"submit_arrival_ts": arrival_ts},
                    event_ts=request_ts,
                )
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{intent_id}:venue-accepted",
                    intent_id=int(intent_id),
                    event_type="VENUE_ACCEPTED",
                    from_state="SUBMIT_INFLIGHT",
                    to_state="VENUE_ACCEPTED",
                    reason="paper_submit_arrived",
                    event_ts=arrival_ts,
                )
            conn.commit()
        return changed

    def mark_venue_delay(
        self,
        intent_id: int,
        *,
        delay_started_at: datetime,
        delay_until: datetime,
        delay_source: str,
    ) -> bool:
        if delay_until <= delay_started_at:
            raise ValueError("venue delay must end after it starts")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_state,cancel_request_ts,cancel_arrival_ts
                FROM quant.paper_live_order_intents
                WHERE intent_id=%s AND status='PROCESSING'
                FOR UPDATE
                """,
                (int(intent_id),),
            )
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return False
            cancel_arrival = row["cancel_arrival_ts"]
            if cancel_arrival is not None and cancel_arrival <= delay_started_at:
                conn.commit()
                return False
            cancel_rejected = cancel_arrival is not None
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET order_state='VENUE_DELAYED',
                    submit_request_ts=%s,submit_arrival_ts=%s,
                    cancel_request_ts=NULL,cancel_arrival_ts=NULL,cancel_ack_ts=NULL,
                    last_error=NULL,updated_at=clock_timestamp()
                WHERE intent_id=%s AND status='PROCESSING'
                """,
                (delay_started_at, delay_until, int(intent_id)),
            )
            _insert_order_event(
                cur,
                idempotency_key=f"paper-order:{intent_id}:venue-delay",
                intent_id=int(intent_id),
                event_type="VENUE_DELAYED",
                from_state=str(row["order_state"]),
                to_state="VENUE_DELAYED",
                reason=str(delay_source),
                payload={
                    "delay_started_at": delay_started_at,
                    "delay_until": delay_until,
                    "cancel_rejected": cancel_rejected,
                },
                event_ts=delay_started_at,
            )
            if cancel_rejected:
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{intent_id}:delay-cancel-rejected",
                    intent_id=int(intent_id),
                    event_type="CANCEL_REJECTED",
                    from_state="VENUE_DELAYED",
                    to_state="VENUE_DELAYED",
                    reason="venue_delay_uncancelable",
                    event_ts=delay_started_at,
                )
            conn.commit()
            return True

    def set_strategy_risk_control(
        self,
        strategy_id: str,
        *,
        trading_enabled: bool,
        kill_switch: bool,
        reason: str | None = None,
    ) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_strategy_risk_controls (
                    strategy_id, trading_enabled, kill_switch, reason
                ) VALUES (%s,%s,%s,%s)
                ON CONFLICT (strategy_id) DO UPDATE SET
                    trading_enabled=EXCLUDED.trading_enabled,
                    kill_switch=EXCLUDED.kill_switch,
                    reason=EXCLUDED.reason,
                    updated_at=clock_timestamp()
                """,
                (
                    str(strategy_id),
                    bool(trading_enabled),
                    bool(kill_switch),
                    reason,
                ),
            )
            conn.commit()

    def load_book_checkpoint(self, checkpoint_id: str | None) -> dict[str, Any] | None:
        if not checkpoint_id:
            return None
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.paper_live_book_checkpoints WHERE checkpoint_id=%s",
                (str(checkpoint_id),),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def load_book_checkpoint_model(
        self,
        checkpoint_id: str | None,
    ) -> ArrivalBookCheckpoint | None:
        row = self.load_book_checkpoint(checkpoint_id)
        return _checkpoint_from_row(row) if row is not None else None

    def recover_abandoned(
        self,
        *,
        older_than_seconds: int = 60,
        batch_id: str | None = None,
    ) -> int:
        """Recover stale claims without replaying already-audited executions.

        A worker can stop after the idempotent execution ledger commits but
        before the intent control row is completed.  Those rows have durable
        audit evidence and must be terminalized from it, not submitted again.
        """

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.intent_id,i.strategy_id,i.client_order_id,i.batch_id,
                       i.order_state,
                       to_jsonb(a) AS durable_audit,
                       p.audit_key AS applied_audit_key
                FROM quant.paper_live_order_intents i
                LEFT JOIN LATERAL (
                    SELECT audit.*
                    FROM quant.paper_taker_order_audits audit
                    WHERE audit.strategy_id=i.strategy_id
                      AND audit.client_order_id=i.client_order_id
                    ORDER BY audit.created_at DESC,audit.audit_key
                    LIMIT 1
                ) a ON TRUE
                LEFT JOIN quant.paper_portfolio_applied_results p
                  ON p.audit_key=a.audit_key
                WHERE i.status='PROCESSING'
                  AND i.claimed_at < now() - make_interval(secs => %s)
                  AND (%s::text IS NULL OR i.batch_id=%s)
                ORDER BY i.intent_id
                FOR UPDATE OF i
                """,
                (
                    max(1, int(older_than_seconds)),
                    str(batch_id) if batch_id is not None else None,
                    str(batch_id) if batch_id is not None else None,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            touched_batches: set[str] = set()
            for row in rows:
                intent_id = int(row["intent_id"])
                selected_batch_id = (
                    str(row["batch_id"]) if row.get("batch_id") else None
                )
                if selected_batch_id is not None:
                    touched_batches.add(selected_batch_id)
                audit = row.get("durable_audit")
                if audit is not None:
                    audit_payload = dict(audit)
                    audit_key = str(audit_payload["audit_key"])
                    applied = str(row.get("applied_audit_key") or "") == audit_key
                    requires_reconciliation = (
                        not applied or _audit_has_working_remainder(audit_payload)
                    )
                    terminal_state = _recovered_order_state(audit_payload)
                    control_status = (
                        "FAILED" if requires_reconciliation else "COMPLETED"
                    )
                    if requires_reconciliation:
                        terminal_state = "RECONCILIATION_REQUIRED"
                    result_payload = _result_payload_from_audit(audit_payload)
                    cur.execute(
                        """
                        UPDATE quant.paper_live_order_intents
                        SET status=%s,worker_id=NULL,claimed_at=NULL,
                            order_state=%s,completed_at=clock_timestamp(),
                            result_audit_key=%s,result=%s::jsonb,
                            remaining_size=%s,remaining_amount=%s,
                            last_error=%s,updated_at=clock_timestamp()
                        WHERE intent_id=%s AND status='PROCESSING'
                        """,
                        (
                            control_status,
                            terminal_state,
                            audit_key,
                            json.dumps(result_payload, default=str),
                            audit_payload.get("remaining_size"),
                            result_payload.get("remaining_amount"),
                            (
                                None
                                if not requires_reconciliation
                                else "worker_recovery_requires_reconciliation"
                            ),
                            intent_id,
                        ),
                    )
                    _insert_order_event(
                        cur,
                        idempotency_key=(
                            f"paper-order:{intent_id}:recovered-terminal:{audit_key}"
                        ),
                        intent_id=intent_id,
                        event_type="WORKER_RECOVERED_TERMINAL",
                        from_state=str(row.get("order_state") or "SUBMIT_QUEUED"),
                        to_state=terminal_state,
                        reason=(
                            "worker_recovered_from_durable_audit"
                            if not requires_reconciliation
                            else "worker_recovery_requires_reconciliation"
                        ),
                        result_audit_key=audit_key,
                        checkpoint_id=audit_payload.get("arrival_checkpoint_id"),
                        payload={
                            "blind_retry_allowed": False,
                            "ledger_applied": applied,
                            "requires_reconciliation": requires_reconciliation,
                        },
                    )
                    cur.execute(
                        """
                        UPDATE quant.paper_order_batch_children
                        SET command_state=%s,reason=%s,execution_status=%s,
                            audit_key=%s,ledger_status=%s,
                            result_json=result_json || %s::jsonb,
                            updated_at=clock_timestamp()
                        WHERE intent_id=%s
                        """,
                        (
                            (
                                "TERMINAL"
                                if not requires_reconciliation
                                else "OUTCOME_UNKNOWN"
                            ),
                            (
                                "worker_recovered_from_durable_audit"
                                if not requires_reconciliation
                                else "worker_recovery_requires_reconciliation"
                            ),
                            (
                                str(audit_payload.get("status") or "UNKNOWN")
                                if not requires_reconciliation
                                else "WORKER_FAILED_UNKNOWN"
                            ),
                            audit_key,
                            (
                                "APPLIED"
                                if not requires_reconciliation
                                and Decimal(str(audit_payload.get("filled_size") or 0))
                                > 0
                                else "NO_EFFECT"
                                if not requires_reconciliation
                                else "RECONCILIATION_REQUIRED"
                            ),
                            json.dumps(
                                {
                                    "worker_recovery": {
                                        "audit_key": audit_key,
                                        "blind_retry_allowed": False,
                                        "ledger_applied": applied,
                                        "requires_reconciliation": (
                                            requires_reconciliation
                                        ),
                                    }
                                }
                            ),
                            intent_id,
                        ),
                    )
                    continue

                cur.execute(
                    """
                    UPDATE quant.paper_live_order_intents
                    SET status='QUEUED',worker_id=NULL,claimed_at=NULL,
                        order_state='CREATED',
                        last_error='worker_recovered_abandoned_intent',
                        updated_at=clock_timestamp()
                    WHERE intent_id=%s AND status='PROCESSING'
                    """,
                    (intent_id,),
                )
                _insert_order_event(
                    cur,
                    idempotency_key=(
                        f"paper-order:{intent_id}:recovered:"
                        f"{datetime.now().isoformat()}"
                    ),
                    intent_id=intent_id,
                    event_type="WORKER_RECOVERED",
                    from_state="ACCEPTED",
                    to_state="CREATED",
                    reason="worker_recovered_abandoned_intent",
                )
            for selected_batch_id in touched_batches:
                self._refresh_batch_state(cur, selected_batch_id)
            conn.commit()
        return len(rows)

    def claim(
        self,
        *,
        worker_id: str,
        limit: int = 50,
        batch_id: str | None = None,
    ) -> list[QueuedIntent]:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH picked AS (
                    SELECT intent_id, order_state AS previous_state
                    FROM quant.paper_live_order_intents
                    WHERE status='QUEUED'
                      AND (%s::text IS NULL OR batch_id=%s)
                    ORDER BY decision_ts, intent_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE quant.paper_live_order_intents i
                SET status='PROCESSING', worker_id=%s, claimed_at=clock_timestamp(),
                    order_state='SUBMIT_QUEUED',
                    updated_at=clock_timestamp()
                FROM picked
                WHERE i.intent_id=picked.intent_id
                RETURNING i.*, picked.previous_state
                """,
                (
                    str(batch_id) if batch_id is not None else None,
                    str(batch_id) if batch_id is not None else None,
                    max(1, int(limit)),
                    worker_id,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                _insert_order_event(
                    cur,
                    idempotency_key=(
                        f"paper-order:{row['intent_id']}:accepted:"
                        f"{row['claimed_at'].isoformat()}"
                    ),
                    intent_id=int(row["intent_id"]),
                    event_type="SUBMIT_QUEUED",
                    from_state=str(row.get("previous_state") or "CREATED"),
                    to_state="SUBMIT_QUEUED",
                    reason=f"claimed_by:{worker_id}",
                )
            profiles: dict[int, ExecutionProfileDecision] = {}
            if rows:
                cur.execute(
                    """
                    SELECT intent_id,decision
                    FROM quant.paper_execution_profile_decisions
                    WHERE intent_id=ANY(%s)
                    """,
                    ([int(row["intent_id"]) for row in rows],),
                )
                for profile_row in cur.fetchall():
                    profile = ExecutionProfileDecision.from_dict(
                        dict(profile_row["decision"])
                    )
                    if not profile.hash_is_valid:
                        raise RuntimeError(
                            "persisted execution profile decision hash mismatch"
                        )
                    profiles[int(profile_row["intent_id"])] = profile
            conn.commit()
        return [
            QueuedIntent(
                int(row["intent_id"]),
                _intent_from_row(row),
                profiles.get(int(row["intent_id"])),
            )
            for row in rows
        ]

    def persist_checkpoints(self, checkpoints: Iterable[ArrivalBookCheckpoint]) -> None:
        rows = list(
            {
                item.checkpoint_id: item for item in checkpoints if item is not None
            }.values()
        )
        if not rows:
            return
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO quant.paper_live_book_checkpoints (
                    checkpoint_id, asset_id, market_id, condition_id, observed_at,
                    generation, coverage_grade, market_state, book_status, has_gap,
                    bids, asks, source_connection_id, source_message_seq, book_fingerprint
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)
                ON CONFLICT (checkpoint_id) DO NOTHING
                """,
                [_checkpoint_values(item) for item in rows],
            )
            conn.commit()

    def initialize_maker_queue(
        self,
        intent_id: int,
        result: PaperExecutionResult,
        checkpoint: ArrivalBookCheckpoint,
        *,
        model_version: str = "paper_maker_queue_strict_ws_trade_v2",
        model_domain_decision: dict[str, Any] | None = None,
    ) -> bool:
        if (
            not result.intent.post_only
            or result.intent.amount_unit != "SHARES"
            or result.status not in {"WORKING", "PARTIAL"}
            or result.remaining_size <= 0
        ):
            return False
        levels = checkpoint.bids if result.intent.side == "BUY" else checkpoint.asks
        displayed = sum(
            (
                level.size
                for level in levels
                if level.price == result.intent.limit_price
            ),
            Decimal("0"),
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(sum(i.remaining_size), 0) AS own_ahead,
                       COALESCE((
                           SELECT current_intent.queue_priority_epoch
                           FROM quant.paper_live_order_intents current_intent
                           WHERE current_intent.intent_id=%s
                       ), 0) AS queue_epoch
                FROM quant.maker_queue_states q
                JOIN quant.paper_live_order_intents i
                  ON i.intent_id::text=q.paper_order_id
                WHERE q.asset_id=%s AND q.side=%s AND q.price_tick=%s
                  AND q.state IN ('WORKING','PARTIAL')
                  AND i.status='WORKING'
                  AND (
                      q.accepted_at < %s OR
                      (q.accepted_at = %s AND i.intent_id < %s)
                  )
                """,
                (
                    int(intent_id),
                    result.intent.asset_id,
                    result.intent.side,
                    result.intent.limit_price,
                    result.arrival_ts,
                    result.arrival_ts,
                    int(intent_id),
                ),
            )
            row = cur.fetchone()
            own_ahead = Decimal(str(row["own_ahead"] if row else 0))
            queue_epoch = int(row["queue_epoch"] if row else 0)
            cur.execute(
                """
                INSERT INTO quant.maker_queue_states (
                    paper_order_id, asset_id, side, price_tick, queue_model_version,
                    displayed_size_at_accept, own_orders_ahead,
                    estimated_external_queue_ahead, accepted_order_size,
                    cumulative_filled_size, state, accepted_checkpoint_id,
                    accepted_at, last_event_id, accepted_book_generation,
                    queue_epoch, last_event_ts, model_domain_decision,
                    model_domain_decision_hash
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,0,'WORKING',%s,%s,%s,%s,%s,%s,
                    %s::jsonb,%s
                )
                ON CONFLICT (paper_order_id) DO NOTHING
                """,
                (
                    str(intent_id),
                    result.intent.asset_id,
                    result.intent.side,
                    result.intent.limit_price,
                    model_version,
                    displayed,
                    own_ahead,
                    displayed,
                    result.remaining_size,
                    checkpoint.checkpoint_id,
                    result.arrival_ts,
                    f"accepted:{checkpoint.checkpoint_id}",
                    checkpoint.generation,
                    queue_epoch,
                    result.arrival_ts,
                    json.dumps(model_domain_decision or {}, sort_keys=True),
                    (model_domain_decision or {}).get("decision_hash"),
                ),
            )
            inserted = int(cur.rowcount or 0) > 0
            for queue_model in (
                QueueModel.RISK_AVERSE_QUEUE,
                QueueModel.PROBABILISTIC_QUEUE,
            ):
                cur.execute(
                    """
                    INSERT INTO quant.maker_research_queue_states (
                        paper_order_id, queue_model, asset_id, side, price_tick,
                        queue_model_version, displayed_size_at_accept,
                        own_orders_ahead, estimated_external_queue_ahead,
                        accepted_order_size, accepted_checkpoint_id, accepted_at,
                        book_generation, queue_epoch, last_event_id, last_event_ts
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    )
                    ON CONFLICT (paper_order_id, queue_model) DO NOTHING
                    """,
                    (
                        str(intent_id),
                        queue_model.value,
                        result.intent.asset_id,
                        result.intent.side,
                        result.intent.limit_price,
                        f"paper_maker_research_l2_v1:{queue_model.value}",
                        displayed,
                        own_ahead,
                        displayed,
                        result.remaining_size,
                        checkpoint.checkpoint_id,
                        result.arrival_ts,
                        checkpoint.generation,
                        queue_epoch,
                        f"accepted:{checkpoint.checkpoint_id}",
                        result.arrival_ts,
                    ),
                )
            if inserted:
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{intent_id}:maker-queue-accepted",
                    intent_id=int(intent_id),
                    event_type="MAKER_QUEUE_ACCEPTED",
                    from_state="WORKING",
                    to_state="WORKING",
                    reason="post_only_order_entered_strict_trade_queue",
                    checkpoint_id=checkpoint.checkpoint_id,
                    payload={
                        "displayed_size_at_accept": displayed,
                        "own_orders_ahead": own_ahead,
                        "accepted_order_size": result.remaining_size,
                        "queue_model_version": model_version,
                        "accepted_book_generation": checkpoint.generation,
                        "queue_epoch": queue_epoch,
                        "model_domain_decision": model_domain_decision or {},
                    },
                    event_ts=result.arrival_ts,
                )
            conn.commit()
            return inserted

    def recover_maker_queues(
        self,
        *,
        model_version: str = "paper_maker_queue_strict_ws_trade_v2",
    ) -> int:
        """Restore missing queue rows for durable post-only WORKING orders."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.*, c.checkpoint_id AS accepted_checkpoint,
                       c.bids AS accepted_bids, c.asks AS accepted_asks,
                       c.generation AS checkpoint_book_generation
                FROM quant.paper_live_order_intents i
                LEFT JOIN quant.maker_queue_states q
                  ON q.paper_order_id=i.intent_id::text
                LEFT JOIN quant.paper_live_book_checkpoints c
                  ON c.checkpoint_id=(i.result->>'arrival_checkpoint_id')
                WHERE i.status='WORKING' AND i.post_only
                  AND i.amount_unit='SHARES' AND i.remaining_size > 0
                  AND q.paper_order_id IS NULL
                ORDER BY COALESCE(i.submit_arrival_ts, i.updated_at), i.intent_id
                """
            )
            rows = [dict(row) for row in cur.fetchall()]
            restored = 0
            for row in rows:
                checkpoint_id = str(row.get("accepted_checkpoint") or "").strip()
                if not checkpoint_id:
                    continue
                levels = (
                    row.get("accepted_bids")
                    if str(row["side"]).upper() == "BUY"
                    else row.get("accepted_asks")
                )
                displayed = _json_level_size(levels, Decimal(str(row["limit_price"])))
                accepted_at = row.get("submit_arrival_ts") or row.get("updated_at")
                cur.execute(
                    """
                    SELECT COALESCE(sum(i.remaining_size), 0) AS own_ahead
                    FROM quant.maker_queue_states q
                    JOIN quant.paper_live_order_intents i
                      ON i.intent_id::text=q.paper_order_id
                    WHERE q.asset_id=%s AND q.side=%s AND q.price_tick=%s
                      AND q.state IN ('WORKING','PARTIAL')
                      AND i.status='WORKING'
                      AND (
                          q.accepted_at < %s OR
                          (q.accepted_at = %s AND i.intent_id < %s)
                      )
                    """,
                    (
                        row["asset_id"],
                        row["side"],
                        row["limit_price"],
                        accepted_at,
                        accepted_at,
                        row["intent_id"],
                    ),
                )
                ahead_row = cur.fetchone()
                own_ahead = Decimal(str(ahead_row["own_ahead"] if ahead_row else 0))
                cur.execute(
                    """
                    INSERT INTO quant.maker_queue_states (
                        paper_order_id, asset_id, side, price_tick,
                        queue_model_version, displayed_size_at_accept,
                        own_orders_ahead, estimated_external_queue_ahead,
                        accepted_order_size, cumulative_filled_size, state,
                        accepted_checkpoint_id, accepted_at, last_event_id,
                        accepted_book_generation, queue_epoch, last_event_ts
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,0,'WORKING',%s,%s,%s,%s,%s,%s
                    )
                    ON CONFLICT (paper_order_id) DO NOTHING
                    """,
                    (
                        str(row["intent_id"]),
                        row["asset_id"],
                        row["side"],
                        row["limit_price"],
                        model_version,
                        displayed,
                        own_ahead,
                        displayed,
                        row["remaining_size"],
                        checkpoint_id,
                        accepted_at,
                        f"accepted:{checkpoint_id}",
                        row.get("checkpoint_book_generation"),
                        int(row.get("queue_priority_epoch") or 0),
                        accepted_at,
                    ),
                )
                if int(cur.rowcount or 0) <= 0:
                    continue
                restored += 1
                _insert_order_event(
                    cur,
                    idempotency_key=(
                        f"paper-order:{row['intent_id']}:maker-queue-recovered"
                    ),
                    intent_id=int(row["intent_id"]),
                    event_type="MAKER_QUEUE_RECOVERED",
                    from_state="WORKING",
                    to_state="WORKING",
                    reason="restored_missing_durable_maker_queue",
                    checkpoint_id=checkpoint_id,
                    payload={
                        "displayed_size_at_accept": displayed,
                        "own_orders_ahead": own_ahead,
                        "accepted_order_size": row["remaining_size"],
                        "queue_model_version": model_version,
                        "accepted_book_generation": row.get(
                            "checkpoint_book_generation"
                        ),
                        "queue_epoch": int(row.get("queue_priority_epoch") or 0),
                    },
                )
            conn.commit()
            return restored

    def recover_maker_research_queues(self) -> int:
        """Create conservative research rows for durable strict Maker queues."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.maker_research_queue_states (
                    paper_order_id, queue_model, asset_id, side, price_tick,
                    queue_model_version, displayed_size_at_accept,
                    own_orders_ahead, estimated_external_queue_ahead,
                    accepted_order_size, cumulative_filled_size, state,
                    accepted_checkpoint_id, accepted_at, book_generation,
                    queue_epoch, last_event_id, last_event_ts
                )
                SELECT q.paper_order_id, model.queue_model, q.asset_id, q.side,
                       q.price_tick,
                       'paper_maker_research_l2_v1:' || model.queue_model,
                       q.displayed_size_at_accept, q.own_orders_ahead,
                       q.estimated_external_queue_ahead,
                       q.accepted_order_size, q.cumulative_filled_size,
                       'NEEDS_REBASE', q.accepted_checkpoint_id,
                       COALESCE(q.accepted_at, i.submit_arrival_ts, i.updated_at),
                       NULL, q.queue_epoch + 1,
                       'recovered:needs_book_rebase', clock_timestamp()
                FROM quant.maker_queue_states q
                JOIN quant.paper_live_order_intents i
                  ON i.intent_id::text=q.paper_order_id
                CROSS JOIN (
                    VALUES ('RISK_AVERSE_QUEUE'), ('PROBABILISTIC_QUEUE')
                ) AS model(queue_model)
                WHERE q.state IN ('WORKING','PARTIAL')
                  AND i.status='WORKING'
                ON CONFLICT (paper_order_id, queue_model) DO NOTHING
                """
            )
            restored = int(cur.rowcount or 0)
            conn.commit()
            return restored

    def load_open_maker_research_levels(
        self,
    ) -> set[tuple[str, str, Decimal]]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT asset_id,side,price_tick
                FROM quant.maker_research_queue_states
                WHERE state IN ('WORKING','PARTIAL','NEEDS_REBASE')
                """
            )
            return {
                (
                    str(row["asset_id"]),
                    str(row["side"]).upper(),
                    Decimal(str(row["price_tick"])),
                )
                for row in cur.fetchall()
            }

    def apply_maker_research_book_events(
        self,
        events: Iterable[MakerResearchBookEvent],
    ) -> dict[str, int]:
        """Advance non-authoritative queue models from the existing L2 feed."""

        rows = tuple(events)
        counts = {"events": 0, "states": 0, "rebases": 0, "duplicates": 0}
        if not rows:
            return counts
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for event in rows:
                params: list[Any] = [event.asset_id]
                level_filter = ""
                if event.event_kind == "BOOK_DELTA":
                    if event.side is None or event.price is None:
                        continue
                    level_filter = " AND side=%s AND price_tick=%s"
                    params.extend((event.side, event.price))
                cur.execute(
                    f"""
                    SELECT * FROM quant.maker_research_queue_states
                    WHERE asset_id=%s
                      AND state IN ('WORKING','PARTIAL','NEEDS_REBASE')
                      {level_filter}
                    ORDER BY accepted_at,paper_order_id,queue_model
                    FOR UPDATE
                    """,
                    tuple(params),
                )
                state_rows = [dict(row) for row in cur.fetchall()]
                counts["events"] += 1
                for row in state_rows:
                    cur.execute(
                        """
                        INSERT INTO quant.maker_research_queue_events (
                            paper_order_id,queue_model,event_id,event_kind,
                            event_ts,book_generation,payload
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                        ON CONFLICT (paper_order_id,queue_model,event_id) DO NOTHING
                        """,
                        (
                            row["paper_order_id"],
                            row["queue_model"],
                            event.event_id,
                            event.event_kind,
                            event.event_ts,
                            event.book_generation,
                            json.dumps(
                                {
                                    "side": event.side,
                                    "price": event.price,
                                    "previous_size": event.previous_size,
                                    "displayed_size": event.displayed_size,
                                },
                                default=str,
                                sort_keys=True,
                            ),
                        ),
                    )
                    if int(cur.rowcount or 0) <= 0:
                        counts["duplicates"] += 1
                        continue
                    state = _maker_research_state(row)
                    engine = MakerQueueEngine(QueueModel(str(row["queue_model"])))
                    event_ts_ns = _datetime_to_ns(event.event_ts)
                    needs_rebase = (
                        str(row["state"]) == "NEEDS_REBASE"
                        or state.book_generation != int(event.book_generation)
                    )
                    if needs_rebase:
                        state = engine.rebase(
                            state,
                            book_generation=event.book_generation,
                            displayed_external_queue=event.displayed_at(
                                side=state.side,
                                price=state.price_tick,
                            ),
                            event_id=event.event_id,
                            event_ts_ns=event_ts_ns,
                        )
                        counts["rebases"] += 1
                    elif (
                        event.event_kind == "BOOK_DELTA"
                        and event.previous_size is not None
                        and event.displayed_size is not None
                        and event.previous_size > event.displayed_size
                    ):
                        state = engine.on_book_decrease(
                            state,
                            decrease=event.previous_size - event.displayed_size,
                            event_id=event.event_id,
                            event_ts_ns=event_ts_ns,
                        )
                    _update_maker_research_state(
                        cur,
                        row=row,
                        state=state,
                        cumulative_filled_size=Decimal(
                            str(row["cumulative_filled_size"] or 0)
                        ),
                    )
                    counts["states"] += 1
            conn.commit()
        return counts

    def apply_maker_research_trade(
        self,
        *,
        asset_id: str,
        price: Decimal,
        size: Decimal,
        aggressor_side: str,
        event_id: str,
        event_ts: datetime,
        current_book_generation: int | None,
        displayed_size_at_price: Decimal | None,
    ) -> dict[str, int]:
        """Advance research fills without mutating an order, ledger, or PnL."""

        passive_side = "SELL" if aggressor_side.upper() == "BUY" else "BUY"
        counts = {"states": 0, "rebases": 0, "duplicates": 0, "fills": 0}
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.maker_research_queue_states
                WHERE asset_id=%s AND side=%s AND price_tick=%s
                  AND state IN ('WORKING','PARTIAL','NEEDS_REBASE')
                ORDER BY accepted_at,paper_order_id,queue_model
                FOR UPDATE
                """,
                (str(asset_id), passive_side, price),
            )
            for row_value in cur.fetchall():
                row = dict(row_value)
                cur.execute(
                    """
                    INSERT INTO quant.maker_research_queue_events (
                        paper_order_id,queue_model,event_id,event_kind,
                        event_ts,book_generation,payload
                    ) VALUES (%s,%s,%s,'TRADE',%s,%s,%s::jsonb)
                    ON CONFLICT (paper_order_id,queue_model,event_id) DO NOTHING
                    """,
                    (
                        row["paper_order_id"],
                        row["queue_model"],
                        event_id,
                        event_ts,
                        current_book_generation,
                        json.dumps(
                            {
                                "aggressor_side": aggressor_side,
                                "price": price,
                                "size": size,
                            },
                            default=str,
                            sort_keys=True,
                        ),
                    ),
                )
                if int(cur.rowcount or 0) <= 0:
                    counts["duplicates"] += 1
                    continue
                state = _maker_research_state(row)
                engine = MakerQueueEngine(QueueModel(str(row["queue_model"])))
                event_ts_ns = _datetime_to_ns(event_ts)
                needs_rebase = (
                    str(row["state"]) == "NEEDS_REBASE"
                    or (
                        current_book_generation is not None
                        and state.book_generation != int(current_book_generation)
                    )
                )
                if needs_rebase:
                    if (
                        current_book_generation is None
                        or displayed_size_at_price is None
                    ):
                        continue
                    state = engine.rebase(
                        state,
                        book_generation=current_book_generation,
                        displayed_external_queue=displayed_size_at_price,
                        event_id=f"rebase:{event_id}",
                        event_ts_ns=event_ts_ns,
                    )
                    counts["rebases"] += 1
                prior_filled = Decimal(str(row["cumulative_filled_size"] or 0))
                advance = engine.advance_trade(
                    state,
                    aggressor_side=aggressor_side,
                    volume=size,
                    event_id=event_id,
                    event_ts_ns=event_ts_ns,
                    cumulative_filled_size=prior_filled,
                )
                research_state = (
                    "FILLED"
                    if advance.cumulative_filled_size >= state.order_size
                    else "PARTIAL"
                    if advance.cumulative_filled_size > 0
                    else "WORKING"
                )
                _update_maker_research_state(
                    cur,
                    row={**row, "state": research_state},
                    state=advance.state,
                    cumulative_filled_size=advance.cumulative_filled_size,
                )
                counts["states"] += 1
                counts["fills"] += int(advance.incremental_fill_size > 0)
            conn.commit()
        return counts

    def plan_maker_trade(
        self,
        *,
        asset_id: str,
        price: Decimal,
        size: Decimal,
        aggressor_side: str,
        event_id: str,
        event_ts: datetime,
        current_book_generation: int | None = None,
        current_checkpoint_id: str | None = None,
        current_checkpoint_observed_at: datetime | None = None,
        displayed_size_at_price: Decimal | None = None,
    ) -> list[MakerQueueAdvancePlan]:
        passive_side = "SELL" if aggressor_side.upper() == "BUY" else "BUY"
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT q.*, i.*, c.coverage_grade AS accepted_coverage_grade,
                       c.generation AS checkpoint_book_generation,
                       c.checkpoint_id AS accepted_checkpoint,
                       COALESCE(i.result->'fidelity', '{}'::jsonb) AS accepted_fidelity
                FROM quant.maker_queue_states q
                JOIN quant.paper_live_order_intents i
                  ON i.intent_id::text=q.paper_order_id
                LEFT JOIN quant.paper_live_book_checkpoints c
                  ON c.checkpoint_id=q.accepted_checkpoint_id
                WHERE q.asset_id=%s AND q.side=%s AND q.price_tick=%s
                  AND q.state IN ('WORKING','PARTIAL') AND i.status='WORKING'
                  AND q.accepted_at <= %s
                  AND (i.cancel_arrival_ts IS NULL OR i.cancel_arrival_ts > %s)
                  AND (i.replace_arrival_ts IS NULL OR i.replace_arrival_ts > %s)
                  AND (
                      i.result_audit_key IS NULL OR EXISTS (
                          SELECT 1 FROM quant.paper_portfolio_applied_results applied
                          WHERE applied.audit_key=i.result_audit_key
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM quant.paper_order_events e
                      WHERE e.idempotency_key=(
                          'paper-order:' || i.intent_id::text || ':maker-trade:' || %s
                      )
                  )
                ORDER BY i.intent_id
                """,
                (
                    str(asset_id),
                    passive_side,
                    price,
                    event_ts,
                    event_ts,
                    event_ts,
                    str(event_id),
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
        plans: list[MakerQueueAdvancePlan] = []
        engine = MakerQueueEngine(QueueModel.STRICT_TRADE_EVIDENCE)
        for row in rows:
            state = MakerQueueState(
                paper_order_id=str(row["paper_order_id"]),
                asset_id=str(row["asset_id"]),
                side=str(row["side"]),
                price_tick=Decimal(str(row["price_tick"])),
                queue_model_version=str(row["queue_model_version"]),
                displayed_size_at_accept=Decimal(str(row["displayed_size_at_accept"])),
                own_orders_ahead=Decimal(str(row["own_orders_ahead"])),
                estimated_external_queue_ahead=Decimal(
                    str(row["estimated_external_queue_ahead"])
                ),
                order_size=Decimal(str(row["accepted_order_size"])),
                cumulative_trade_volume_at_price=Decimal(
                    str(row["cumulative_trade_volume_at_price"])
                ),
                cumulative_cancel_ahead_estimate=Decimal(
                    str(row["cumulative_cancel_ahead_estimate"])
                ),
                last_event_id=str(row["last_event_id"] or ""),
                book_generation=(
                    int(row["accepted_book_generation"])
                    if row.get("accepted_book_generation") is not None
                    else int(row["checkpoint_book_generation"])
                    if row.get("checkpoint_book_generation") is not None
                    else None
                ),
                queue_epoch=int(row.get("queue_epoch") or 0),
                last_event_ts_ns=(
                    _datetime_to_ns(row["last_event_ts"])
                    if row.get("last_event_ts") is not None
                    else None
                ),
            )
            rebased = False
            if current_book_generation is not None and state.book_generation != int(
                current_book_generation
            ):
                baseline_ts = current_checkpoint_observed_at or event_ts
                state = engine.rebase(
                    state,
                    book_generation=int(current_book_generation),
                    displayed_external_queue=(
                        displayed_size_at_price
                        if displayed_size_at_price is not None
                        else state.displayed_size_at_accept
                    ),
                    event_id=(
                        f"rebase:{current_checkpoint_id}"
                        if current_checkpoint_id
                        else f"rebase:generation:{current_book_generation}"
                    ),
                    event_ts_ns=_datetime_to_ns(baseline_ts),
                )
                rebased = True
            advance = engine.advance_trade(
                state,
                aggressor_side=aggressor_side,
                volume=size,
                event_id=event_id,
                event_ts_ns=_datetime_to_ns(event_ts),
                cumulative_filled_size=Decimal(str(row["cumulative_filled_size"])),
            )
            if not rebased and advance.state == state:
                # A unique but late trade is observable evidence, not a queue
                # advance. Keep it in the raw trade table without manufacturing
                # an order lifecycle event.
                continue
            remaining_before = Decimal(str(row["remaining_size"] or 0))
            fill_size = min(remaining_before, advance.incremental_fill_size)
            plans.append(
                MakerQueueAdvancePlan(
                    intent_id=int(row["intent_id"]),
                    intent=_intent_from_row(row),
                    event_id=str(event_id),
                    event_ts=event_ts,
                    prior_last_event_id=str(row["last_event_id"] or "") or None,
                    prior_last_event_ts=row.get("last_event_ts"),
                    prior_queue_epoch=int(row.get("queue_epoch") or 0),
                    next_state=advance.state,
                    incremental_fill_size=fill_size,
                    cumulative_filled_size=Decimal(str(row["cumulative_filled_size"]))
                    + fill_size,
                    remaining_size=max(Decimal("0"), remaining_before - fill_size),
                    arrival_checkpoint_id=(
                        str(row["accepted_checkpoint"])
                        if row.get("accepted_checkpoint")
                        else None
                    ),
                    coverage_grade=(
                        str(row["accepted_coverage_grade"])
                        if row.get("accepted_coverage_grade")
                        else None
                    ),
                    book_generation=(
                        advance.state.book_generation
                        if advance.state.book_generation is not None
                        else None
                    ),
                    fidelity={
                        **dict(row.get("accepted_fidelity") or {}),
                        "maker_model_domain": dict(
                            row.get("model_domain_decision") or {}
                        ),
                        "maker_queue_epoch": advance.state.queue_epoch,
                        "maker_queue_rebased": rebased,
                    },
                    rebased=rebased,
                )
            )
        return plans

    def load_unapplied_results(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return durable execution results whose account mutation is still pending."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.intent_id,i.result_audit_key,i.result
                FROM quant.paper_live_order_intents i
                LEFT JOIN quant.paper_portfolio_applied_results applied
                  ON applied.audit_key=i.result_audit_key
                WHERE i.result_audit_key IS NOT NULL
                  AND i.result IS NOT NULL
                  AND applied.audit_key IS NULL
                  AND i.status IN ('WORKING','COMPLETED','FAILED')
                ORDER BY i.updated_at,i.intent_id
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
            return [dict(row) for row in cur.fetchall()]

    def commit_maker_trade(
        self,
        plan: MakerQueueAdvancePlan,
        result: PaperExecutionResult | None = None,
    ) -> bool:
        if (result is None) != (plan.incremental_fill_size <= 0):
            raise ValueError("maker result must exist exactly when a fill exists")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT last_event_id,last_event_ts,queue_epoch
                FROM quant.maker_queue_states
                WHERE paper_order_id=%s FOR UPDATE
                """,
                (str(plan.intent_id),),
            )
            current = cur.fetchone()
            current_last = str(current["last_event_id"] or "") if current else ""
            current_last_ts = current["last_event_ts"] if current else None
            current_epoch = int(current["queue_epoch"] or 0) if current else 0
            if (
                current is None
                or current_last != str(plan.prior_last_event_id or "")
                or current_last_ts != plan.prior_last_event_ts
                or current_epoch != int(plan.prior_queue_epoch)
            ):
                conn.commit()
                return False
            inserted = _insert_order_event(
                cur,
                idempotency_key=(
                    f"paper-order:{plan.intent_id}:maker-trade:{plan.event_id}"
                ),
                intent_id=plan.intent_id,
                event_type=(
                    "MAKER_FILL"
                    if plan.incremental_fill_size > 0
                    else "MAKER_QUEUE_ADVANCE"
                ),
                from_state="WORKING",
                to_state="CONFIRMED" if plan.remaining_size <= 0 else "WORKING",
                reason=(
                    "strict_trade_evidence_filled_order"
                    if plan.incremental_fill_size > 0
                    else "strict_trade_evidence_consumed_queue_ahead"
                ),
                checkpoint_id=plan.arrival_checkpoint_id,
                payload={
                    "trade_event_id": plan.event_id,
                    "trade_volume_at_price": plan.next_state.cumulative_trade_volume_at_price,
                    "incremental_fill_size": plan.incremental_fill_size,
                    "cumulative_filled_size": plan.cumulative_filled_size,
                    "queue_ahead_after": plan.next_state.effective_queue_ahead,
                    "queue_epoch": plan.next_state.queue_epoch,
                    "book_generation": plan.next_state.book_generation,
                    "rebased": plan.rebased,
                },
                event_ts=plan.event_ts,
            )
            if not inserted:
                conn.commit()
                return False
            cur.execute(
                """
                UPDATE quant.maker_queue_states
                SET displayed_size_at_accept=%s,
                    estimated_external_queue_ahead=%s,
                    cumulative_trade_volume_at_price=%s,
                    cumulative_cancel_ahead_estimate=%s,
                    cumulative_filled_size=%s, last_event_id=%s,
                    accepted_book_generation=%s, queue_epoch=%s,
                    last_event_ts=%s,
                    state=%s, updated_at=clock_timestamp()
                WHERE paper_order_id=%s
                """,
                (
                    plan.next_state.displayed_size_at_accept,
                    plan.next_state.estimated_external_queue_ahead,
                    plan.next_state.cumulative_trade_volume_at_price,
                    plan.next_state.cumulative_cancel_ahead_estimate,
                    plan.cumulative_filled_size,
                    plan.next_state.last_event_id,
                    plan.next_state.book_generation,
                    plan.next_state.queue_epoch,
                    _ns_to_datetime(plan.next_state.last_event_ts_ns),
                    "FILLED"
                    if plan.remaining_size <= 0
                    else "PARTIAL"
                    if plan.incremental_fill_size > 0
                    else "WORKING",
                    str(plan.intent_id),
                ),
            )
            if result is not None:
                working = result.remaining_size > 0
                transitions = result_transitions(result)
                previous_state = "WORKING"
                cur.execute(
                    """
                    UPDATE quant.paper_live_order_intents
                    SET status=CASE WHEN %s THEN 'WORKING' ELSE 'COMPLETED' END,
                        order_state=CASE WHEN %s THEN 'WORKING' ELSE 'CONFIRMED' END,
                        completed_at=CASE WHEN %s THEN NULL ELSE clock_timestamp() END,
                        result_audit_key=%s, result=%s::jsonb, last_error=NULL,
                        remaining_size=%s, remaining_amount=%s,
                        updated_at=clock_timestamp()
                    WHERE intent_id=%s AND status='WORKING'
                    """,
                    (
                        working,
                        working,
                        working,
                        result.audit_key,
                        json.dumps(result.as_dict()),
                        result.remaining_size,
                        result.remaining_amount,
                        plan.intent_id,
                    ),
                )
                if int(cur.rowcount or 0) != 1:
                    raise RuntimeError(
                        "maker intent was not WORKING during fill commit"
                    )
                for index, transition in enumerate(transitions):
                    _insert_order_event(
                        cur,
                        idempotency_key=(
                            f"paper-order:{plan.intent_id}:result:{result.audit_key}:"
                            f"{index}:{transition.to_state}"
                        ),
                        intent_id=plan.intent_id,
                        event_type=transition.event_type,
                        from_state=previous_state,
                        to_state=transition.to_state,
                        reason=transition.reason,
                        result_audit_key=result.audit_key,
                        checkpoint_id=result.arrival_checkpoint_id,
                        payload={
                            "filled_size": result.filled_size,
                            "remaining_size": result.remaining_size,
                            "filled_notional": result.filled_notional,
                            "remaining_amount": result.remaining_amount,
                            "simulation_confirmation_mode": "IMMEDIATE",
                            "maker_trade_event_id": plan.event_id,
                        },
                        event_ts=result.arrival_ts,
                    )
                    previous_state = transition.to_state
            conn.commit()
            return True

    def close_maker_queue(self, intent_id: int, *, state: str, reason: str) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT q.asset_id, q.side, q.price_tick, i.remaining_size
                FROM quant.maker_queue_states q
                JOIN quant.paper_live_order_intents i
                  ON i.intent_id::text=q.paper_order_id
                WHERE q.paper_order_id=%s AND q.state IN ('WORKING','PARTIAL')
                FOR UPDATE OF q
                """,
                (str(intent_id),),
            )
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return False
            released = max(Decimal("0"), Decimal(str(row["remaining_size"] or 0)))
            cur.execute(
                """
                UPDATE quant.maker_queue_states
                SET state=%s, updated_at=clock_timestamp()
                WHERE paper_order_id=%s
                """,
                (str(state).upper(), str(intent_id)),
            )
            cur.execute(
                """
                UPDATE quant.maker_research_queue_states
                SET state=%s, updated_at=clock_timestamp()
                WHERE paper_order_id=%s
                  AND state IN ('WORKING','PARTIAL','NEEDS_REBASE')
                """,
                (str(state).upper(), str(intent_id)),
            )
            cur.execute(
                """
                UPDATE quant.maker_queue_states q
                SET own_orders_ahead=GREATEST(0, q.own_orders_ahead-%s),
                    updated_at=clock_timestamp()
                WHERE q.asset_id=%s AND q.side=%s AND q.price_tick=%s
                  AND q.paper_order_id::bigint > %s
                  AND q.state IN ('WORKING','PARTIAL')
                """,
                (
                    released,
                    row["asset_id"],
                    row["side"],
                    row["price_tick"],
                    int(intent_id),
                ),
            )
            cur.execute(
                """
                UPDATE quant.maker_research_queue_states q
                SET own_orders_ahead=GREATEST(0, q.own_orders_ahead-%s),
                    updated_at=clock_timestamp()
                WHERE q.asset_id=%s AND q.side=%s AND q.price_tick=%s
                  AND q.paper_order_id::bigint > %s
                  AND q.state IN ('WORKING','PARTIAL','NEEDS_REBASE')
                """,
                (
                    released,
                    row["asset_id"],
                    row["side"],
                    row["price_tick"],
                    int(intent_id),
                ),
            )
            _insert_order_event(
                cur,
                idempotency_key=f"paper-order:{intent_id}:maker-queue-close:{state}",
                intent_id=int(intent_id),
                event_type="MAKER_QUEUE_CLOSED",
                from_state="WORKING",
                to_state=str(state).upper(),
                reason=reason,
                payload={"released_own_queue_ahead": released},
            )
            conn.commit()
            return True

    def persist_current_books(
        self,
        checkpoints: Iterable[ArrivalBookCheckpoint],
        *,
        transport_state: str,
        redundant_asset_ids: Iterable[str],
    ) -> None:
        rows = list(
            {item.asset_id: item for item in checkpoints if item is not None}.values()
        )
        if not rows:
            return
        redundant = {str(item) for item in redundant_asset_ids}
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO quant.paper_live_current_books (
                    asset_id, market_id, condition_id, observed_at, generation,
                    coverage_grade, market_state, book_status, has_gap,
                    best_bid, best_ask, bids, asks, source_connection_id,
                    source_message_seq, book_fingerprint, transport_state,
                    redundant_feed_match
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,
                    %s,%s,%s,%s,%s
                )
                ON CONFLICT (asset_id) DO UPDATE SET
                    market_id=EXCLUDED.market_id,
                    condition_id=EXCLUDED.condition_id,
                    observed_at=EXCLUDED.observed_at,
                    generation=EXCLUDED.generation,
                    coverage_grade=EXCLUDED.coverage_grade,
                    market_state=EXCLUDED.market_state,
                    book_status=EXCLUDED.book_status,
                    has_gap=EXCLUDED.has_gap,
                    best_bid=EXCLUDED.best_bid,
                    best_ask=EXCLUDED.best_ask,
                    bids=EXCLUDED.bids,
                    asks=EXCLUDED.asks,
                    source_connection_id=EXCLUDED.source_connection_id,
                    source_message_seq=EXCLUDED.source_message_seq,
                    book_fingerprint=EXCLUDED.book_fingerprint,
                    transport_state=EXCLUDED.transport_state,
                    redundant_feed_match=EXCLUDED.redundant_feed_match,
                    updated_at=clock_timestamp()
                WHERE EXCLUDED.observed_at >= quant.paper_live_current_books.observed_at
                """,
                [
                    (
                        item.asset_id,
                        item.market_id,
                        item.condition_id,
                        item.observed_at,
                        item.generation,
                        item.coverage_grade,
                        item.market_state,
                        item.book_status,
                        item.has_gap,
                        item.bids[0].price if item.bids else None,
                        item.asks[0].price if item.asks else None,
                        json.dumps(
                            [[str(level.price), str(level.size)] for level in item.bids]
                        ),
                        json.dumps(
                            [[str(level.price), str(level.size)] for level in item.asks]
                        ),
                        *_source_parts(item.source_event_end),
                        item.checkpoint_id,
                        str(transport_state),
                        item.asset_id in redundant,
                    )
                    for item in rows
                ],
            )
            conn.commit()

    def complete(self, intent_id: int, result: PaperExecutionResult) -> None:
        working = (
            result.intent.order_type in {"GTC", "GTD"}
            and result.status in {"WORKING", "PARTIAL"}
            and result.remaining_size > 0
        )
        control_status = "WORKING" if working else "COMPLETED"
        transitions = result_transitions(result)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_state, cancel_request_ts, cancel_arrival_ts
                FROM quant.paper_live_order_intents
                WHERE intent_id=%s
                FOR UPDATE
                """,
                (int(intent_id),),
            )
            current = cur.fetchone()
            previous_state = (
                str(current["order_state"]) if current is not None else "UNKNOWN"
            )
            pending_cancel = bool(
                current is not None
                and current["cancel_request_ts"] is not None
                and control_status == "WORKING"
            )
            order_state = (
                "CANCEL_INFLIGHT" if pending_cancel else transitions[-1].to_state
            )
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET status=%s, order_state=%s,
                    completed_at=CASE WHEN %s='COMPLETED' THEN clock_timestamp() ELSE NULL END,
                    result_audit_key=%s, result=%s::jsonb, last_error=NULL,
                    remaining_size=%s, remaining_amount=%s,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s
                RETURNING order_state
                """,
                (
                    control_status,
                    order_state,
                    control_status,
                    result.audit_key,
                    json.dumps(result.as_dict()),
                    result.remaining_size,
                    result.remaining_amount,
                    int(intent_id),
                ),
            )
            row = cur.fetchone()
            if row is not None:
                for index, transition in enumerate(transitions):
                    _insert_order_event(
                        cur,
                        idempotency_key=(
                            f"paper-order:{intent_id}:result:{result.audit_key}:"
                            f"{index}:{transition.to_state}"
                        ),
                        intent_id=int(intent_id),
                        event_type=transition.event_type,
                        from_state=previous_state,
                        to_state=transition.to_state,
                        reason=transition.reason,
                        result_audit_key=result.audit_key,
                        checkpoint_id=result.arrival_checkpoint_id,
                        payload={
                            "filled_size": result.filled_size,
                            "remaining_size": result.remaining_size,
                            "filled_notional": result.filled_notional,
                            "remaining_amount": result.remaining_amount,
                            "simulation_confirmation_mode": (
                                "IMMEDIATE" if result.filled_size > 0 else None
                            ),
                        },
                        event_ts=result.arrival_ts,
                    )
                    previous_state = transition.to_state
                if pending_cancel:
                    _insert_order_event(
                        cur,
                        idempotency_key=(
                            f"paper-order:{intent_id}:result:{result.audit_key}:"
                            "cancel-inflight-remainder"
                        ),
                        intent_id=int(intent_id),
                        event_type="CANCEL_INFLIGHT",
                        from_state=previous_state,
                        to_state="CANCEL_INFLIGHT",
                        reason="cancel_pending_for_working_remainder",
                        result_audit_key=result.audit_key,
                        checkpoint_id=result.arrival_checkpoint_id,
                        payload={
                            "remaining_size": result.remaining_size,
                            "remaining_amount": result.remaining_amount,
                            "cancel_arrival_ts": current["cancel_arrival_ts"],
                        },
                        event_ts=result.arrival_ts,
                    )
            conn.commit()

    def expire_working(self) -> list[dict[str, Any]]:
        """Close expired/non-tradable resting paper orders without simulating maker fills."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT i.intent_id,
                           CASE
                             WHEN i.time_in_force='GTD'
                                  AND i.expires_at - interval '1 minute' <= now()
                               THEN 'EXPIRED'
                             ELSE 'CANCELED'
                           END AS target_state,
                           CASE
                             WHEN i.time_in_force='GTD'
                                  AND i.expires_at - interval '1 minute' <= now()
                               THEN 'gtd_expired'
                             ELSE 'market_no_longer_tradable'
                           END AS reason
                    FROM quant.paper_live_order_intents i
                    JOIN quant.paper_execution_market_catalog r USING (asset_id)
                    WHERE i.status='WORKING'
                      AND (
                           (i.time_in_force='GTD'
                            AND i.expires_at - interval '1 minute' <= now())
                        OR r.active=FALSE OR r.closed=TRUE OR r.resolved=TRUE
                        OR r.archived=TRUE OR r.deprecated=TRUE
                        OR r.market_state IN ('CLOSING','RESOLVED','ARCHIVED')
                      )
                    FOR UPDATE OF i SKIP LOCKED
                )
                UPDATE quant.paper_live_order_intents i
                SET status=c.target_state, order_state=c.target_state,
                    completed_at=clock_timestamp(), last_error=c.reason,
                    updated_at=clock_timestamp()
                FROM candidates c
                WHERE i.intent_id=c.intent_id
                RETURNING i.intent_id, i.order_state, i.last_error
                """
            )
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{row['intent_id']}:{str(row['order_state']).lower()}",
                    intent_id=int(row["intent_id"]),
                    event_type=str(row["order_state"]),
                    from_state="WORKING",
                    to_state=str(row["order_state"]),
                    reason=str(row["last_error"]),
                )
            conn.commit()
        return rows

    def enqueue_market_clarification(
        self,
        *,
        condition_id: str,
        source_event_id: str,
        clarified_at: datetime,
        payload_hash: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected_condition = str(condition_id).strip()
        selected_event = str(source_event_id).strip()
        selected_hash = str(payload_hash).strip()
        if not selected_condition or not selected_event or not selected_hash:
            raise ValueError(
                "condition_id, source_event_id and payload_hash are required"
            )
        encoded_payload = json.dumps(
            payload or {}, sort_keys=True, separators=(",", ":")
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_market_clarification_commands (
                    condition_id,source_event_id,clarified_at,payload_hash,payload
                ) VALUES (%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (source_event_id) DO NOTHING
                RETURNING command_id,status
                """,
                (
                    selected_condition,
                    selected_event,
                    clarified_at,
                    selected_hash,
                    encoded_payload,
                ),
            )
            row = cur.fetchone()
            duplicate = row is None
            if row is None:
                cur.execute(
                    """
                    SELECT command_id,condition_id,source_event_id,clarified_at,
                           payload_hash,status,attempts,next_attempt_at,last_error
                    FROM quant.paper_market_clarification_commands
                    WHERE source_event_id=%s
                    """,
                    (selected_event,),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(
                        "clarification command disappeared after conflict"
                    )
                if (
                    str(row["condition_id"]) != selected_condition
                    or str(row["payload_hash"]) != selected_hash
                ):
                    raise ValueError(
                        "clarification source_event_id conflicts with prior command"
                    )
            conn.commit()
        return {**dict(row), "duplicate": duplicate}

    def claim_market_clarifications(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        abandoned_after_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH due AS (
                    SELECT command_id
                    FROM quant.paper_market_clarification_commands
                    WHERE (
                        status='PENDING' AND next_attempt_at<=clock_timestamp()
                    ) OR (
                        status='PROCESSING'
                        AND claimed_at < clock_timestamp()
                            - make_interval(secs => %s)
                    )
                    ORDER BY next_attempt_at,command_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE quant.paper_market_clarification_commands command
                SET status='PROCESSING',worker_id=%s,claimed_at=clock_timestamp(),
                    attempts=attempts + 1,last_error=NULL,
                    updated_at=clock_timestamp()
                FROM due
                WHERE command.command_id=due.command_id
                RETURNING command.command_id,command.condition_id,
                          command.source_event_id,command.clarified_at,
                          command.payload_hash,command.payload,command.attempts
                """,
                (
                    max(1, int(abandoned_after_seconds)),
                    max(1, int(limit)),
                    str(worker_id),
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return rows

    def complete_market_clarification_command(
        self,
        command_id: int,
        *,
        worker_id: str,
    ) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_market_clarification_commands
                SET status='APPLIED',applied_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE command_id=%s AND status='PROCESSING' AND worker_id=%s
                RETURNING command_id
                """,
                (int(command_id), str(worker_id)),
            )
            changed = cur.fetchone() is not None
            conn.commit()
        return changed

    def fail_market_clarification_command(
        self,
        command_id: int,
        *,
        worker_id: str,
        error: str,
        max_attempts: int = 5,
    ) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_market_clarification_commands
                SET status=CASE WHEN attempts >= %s THEN 'FAILED' ELSE 'PENDING' END,
                    next_attempt_at=clock_timestamp()
                        + make_interval(
                            secs => LEAST(60, POWER(2, attempts))::double precision
                        ),
                    worker_id=NULL,claimed_at=NULL,last_error=%s,
                    updated_at=clock_timestamp()
                WHERE command_id=%s AND status='PROCESSING' AND worker_id=%s
                RETURNING command_id
                """,
                (
                    max(1, int(max_attempts)),
                    str(error)[:1000],
                    int(command_id),
                    str(worker_id),
                ),
            )
            changed = cur.fetchone() is not None
            conn.commit()
        return changed

    def apply_market_clarification(
        self,
        *,
        condition_id: str,
        source_event_id: str,
        clarified_at: datetime,
        payload_hash: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected_condition = str(condition_id).strip()
        selected_event = str(source_event_id).strip()
        selected_hash = str(payload_hash).strip()
        if not selected_condition or not selected_event or not selected_hash:
            raise ValueError(
                "condition_id, source_event_id and payload_hash are required"
            )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_market_clarifications (
                    condition_id,source_event_id,clarified_at,payload_hash,payload
                ) VALUES (%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (source_event_id) DO NOTHING
                RETURNING clarification_id
                """,
                (
                    selected_condition,
                    selected_event,
                    clarified_at,
                    selected_hash,
                    json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
                ),
            )
            inserted = cur.fetchone()
            if inserted is None:
                cur.execute(
                    """
                    SELECT clarification_id,condition_id,source_event_id,
                           clarified_at,payload_hash,asset_ids,canceled_intent_ids
                    FROM quant.paper_market_clarifications
                    WHERE source_event_id=%s
                    """,
                    (selected_event,),
                )
                existing = dict(cur.fetchone())
                if (
                    str(existing["condition_id"]) != selected_condition
                    or str(existing["payload_hash"]) != selected_hash
                ):
                    raise ValueError(
                        "clarification source_event_id conflicts with prior payload"
                    )
                conn.commit()
                return {**existing, "duplicate": True}

            clarification_id = int(inserted["clarification_id"])
            cur.execute(
                """
                SELECT asset_id
                FROM quant.paper_execution_market_catalog
                WHERE condition_id=%s
                ORDER BY asset_id
                """,
                (selected_condition,),
            )
            asset_ids = [str(row["asset_id"]) for row in cur.fetchall()]
            cur.execute(
                """
                WITH candidates AS (
                    SELECT intent_id,order_state
                    FROM quant.paper_live_order_intents
                    WHERE condition_id=%s
                      AND status IN ('QUEUED','PROCESSING','WORKING')
                    FOR UPDATE
                )
                UPDATE quant.paper_live_order_intents intent
                SET status='CANCELED',order_state='CANCELED',
                    cancel_request_ts=%s,cancel_arrival_ts=%s,cancel_ack_ts=%s,
                    completed_at=clock_timestamp(),
                    last_error='market_clarification',
                    updated_at=clock_timestamp()
                FROM candidates
                WHERE intent.intent_id=candidates.intent_id
                RETURNING intent.intent_id,candidates.order_state AS prior_state
                """,
                (
                    selected_condition,
                    clarified_at,
                    clarified_at,
                    clarified_at,
                ),
            )
            canceled = [dict(row) for row in cur.fetchall()]
            canceled_ids = sorted(int(row["intent_id"]) for row in canceled)
            for row in canceled:
                intent_id = int(row["intent_id"])
                _insert_order_event(
                    cur,
                    idempotency_key=(
                        f"paper-order:{intent_id}:clarification:{selected_event}"
                    ),
                    intent_id=intent_id,
                    event_type="CANCELED",
                    from_state=str(row["prior_state"]),
                    to_state="CANCELED",
                    reason="market_clarification",
                    payload={
                        "condition_id": selected_condition,
                        "source_event_id": selected_event,
                        "payload_hash": selected_hash,
                    },
                    event_ts=clarified_at,
                )
            cur.execute(
                """
                UPDATE quant.paper_market_clarifications
                SET asset_ids=%s::text[],canceled_intent_ids=%s::bigint[]
                WHERE clarification_id=%s
                """,
                (asset_ids, canceled_ids, clarification_id),
            )
            conn.commit()
            return {
                "clarification_id": clarification_id,
                "condition_id": selected_condition,
                "source_event_id": selected_event,
                "clarified_at": clarified_at,
                "payload_hash": selected_hash,
                "asset_ids": asset_ids,
                "canceled_intent_ids": canceled_ids,
                "duplicate": False,
            }

    def cancel(
        self,
        intent_id: int,
        *,
        reason: str = "user_cancel_requested",
        latency_ms: int = 100,
    ) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET status=CASE WHEN status='QUEUED' THEN 'CANCELED' ELSE status END,
                    order_state=CASE
                        WHEN status='QUEUED' THEN 'CANCELED'
                        ELSE 'CANCEL_INFLIGHT'
                    END,
                    cancel_request_ts=clock_timestamp(),
                    cancel_arrival_ts=clock_timestamp()
                        + make_interval(secs => %s::double precision / 1000),
                    cancel_ack_ts=CASE
                        WHEN status='QUEUED' THEN clock_timestamp()
                        ELSE NULL
                    END,
                    completed_at=CASE
                        WHEN status='QUEUED' THEN clock_timestamp()
                        ELSE NULL
                    END,
                    last_error=%s,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s AND status IN ('QUEUED','PROCESSING','WORKING')
                  AND cancel_request_ts IS NULL
                  AND order_state NOT IN (
                    'VENUE_DELAYED','CANCELED','CONFIRMED','REJECTED','FAILED_REVERSED'
                  )
                RETURNING intent_id, status, order_state,
                          cancel_request_ts, cancel_arrival_ts
                """,
                (max(0, int(latency_ms)), str(reason)[:1000], int(intent_id)),
            )
            row = cur.fetchone()
            changed = row is not None
            if row is not None:
                immediate = str(row["status"]) == "CANCELED"
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{intent_id}:cancel-requested",
                    intent_id=int(intent_id),
                    event_type="CANCELED" if immediate else "CANCEL_INFLIGHT",
                    from_state=None,
                    to_state="CANCELED" if immediate else "CANCEL_INFLIGHT",
                    reason=reason,
                    payload={
                        "cancel_arrival_ts": row["cancel_arrival_ts"],
                        "latency_ms": max(0, int(latency_ms)),
                    },
                    event_ts=row["cancel_request_ts"],
                )
            conn.commit()
        return changed

    def cancel_many(
        self,
        intent_ids: Iterable[int],
        *,
        reason: str = "user_bulk_cancel_requested",
        latency_ms: int = 100,
    ) -> list[int]:
        selected = sorted({int(intent_id) for intent_id in intent_ids})
        if not selected:
            return []
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET status=CASE WHEN status='QUEUED' THEN 'CANCELED' ELSE status END,
                    order_state=CASE
                        WHEN status='QUEUED' THEN 'CANCELED'
                        ELSE 'CANCEL_INFLIGHT'
                    END,
                    cancel_request_ts=clock_timestamp(),
                    cancel_arrival_ts=clock_timestamp()
                        + make_interval(secs => %s::double precision / 1000),
                    cancel_ack_ts=CASE
                        WHEN status='QUEUED' THEN clock_timestamp()
                        ELSE NULL
                    END,
                    completed_at=CASE
                        WHEN status='QUEUED' THEN clock_timestamp()
                        ELSE NULL
                    END,
                    last_error=%s,
                    updated_at=clock_timestamp()
                WHERE intent_id=ANY(%s::bigint[])
                  AND status IN ('QUEUED','PROCESSING','WORKING')
                  AND cancel_request_ts IS NULL
                  AND order_state NOT IN (
                    'VENUE_DELAYED','CANCELED','CONFIRMED','REJECTED','FAILED_REVERSED'
                  )
                RETURNING intent_id,status,order_state,
                          cancel_request_ts,cancel_arrival_ts
                """,
                (max(0, int(latency_ms)), str(reason)[:1000], selected),
            )
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                intent_id = int(row["intent_id"])
                immediate = str(row["status"]) == "CANCELED"
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{intent_id}:cancel-requested",
                    intent_id=intent_id,
                    event_type="CANCELED" if immediate else "CANCEL_INFLIGHT",
                    from_state=None,
                    to_state="CANCELED" if immediate else "CANCEL_INFLIGHT",
                    reason=reason,
                    payload={
                        "cancel_arrival_ts": row["cancel_arrival_ts"],
                        "latency_ms": max(0, int(latency_ms)),
                        "bulk": True,
                    },
                    event_ts=row["cancel_request_ts"],
                )
            conn.commit()
        return sorted(int(row["intent_id"]) for row in rows)

    def apply_due_cancels(self, *, limit: int = 100) -> list[int]:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH due AS (
                    SELECT intent_id
                    FROM quant.paper_live_order_intents
                    WHERE status IN ('PROCESSING','WORKING')
                      AND cancel_request_ts IS NOT NULL
                      AND cancel_arrival_ts <= clock_timestamp()
                      AND (
                        replace_arrival_ts IS NULL
                        OR cancel_arrival_ts <= replace_arrival_ts
                      )
                      AND order_state NOT IN (
                        'VENUE_DELAYED','CANCELED','CONFIRMED','REJECTED','FAILED_REVERSED'
                      )
                    ORDER BY cancel_arrival_ts, intent_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE quant.paper_live_order_intents i
                SET status='CANCELED', order_state='CANCELED',
                    cancel_ack_ts=clock_timestamp(),
                    completed_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                FROM due
                WHERE i.intent_id=due.intent_id
                RETURNING i.intent_id, i.cancel_ack_ts
                """,
                (max(1, int(limit)),),
            )
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{row['intent_id']}:cancel-ack",
                    intent_id=int(row["intent_id"]),
                    event_type="CANCELED",
                    from_state="CANCEL_INFLIGHT",
                    to_state="CANCELED",
                    reason="cancel_arrived_before_remaining_fill",
                    event_ts=row["cancel_ack_ts"],
                )
            conn.commit()
        return [int(row["intent_id"]) for row in rows]

    def replace(
        self,
        intent_id: int,
        *,
        limit_price: Decimal,
        size: Decimal,
        latency_ms: int = 100,
        reason: str = "user_replace_requested",
    ) -> bool:
        if size <= 0 or not Decimal("0") < limit_price < Decimal("1"):
            raise ValueError(
                "replacement size must be positive and price must be between 0 and 1"
            )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET order_state='REPLACE_REQUESTED',
                    replace_request_ts=clock_timestamp(),
                    replace_arrival_ts=clock_timestamp()
                        + make_interval(secs => %s::double precision / 1000),
                    replace_limit_price=%s,
                    replace_size=%s,
                    last_error=%s,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s
                  AND status IN ('PROCESSING','WORKING')
                  AND time_in_force IN ('GTC','GTD')
                  AND replace_request_ts IS NULL
                  AND order_state NOT IN (
                    'VENUE_DELAYED','CANCELED','CONFIRMED','REJECTED','FAILED_REVERSED'
                  )
                RETURNING replace_request_ts, replace_arrival_ts
                """,
                (
                    max(0, int(latency_ms)),
                    limit_price,
                    size,
                    str(reason)[:1000],
                    int(intent_id),
                ),
            )
            row = cur.fetchone()
            if row is not None:
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{intent_id}:replace-requested",
                    intent_id=int(intent_id),
                    event_type="REPLACE_REQUESTED",
                    from_state=None,
                    to_state="REPLACE_REQUESTED",
                    reason=reason,
                    payload={
                        "replace_arrival_ts": row["replace_arrival_ts"],
                        "limit_price": limit_price,
                        "size": size,
                        "latency_ms": max(0, int(latency_ms)),
                    },
                    event_ts=row["replace_request_ts"],
                )
            conn.commit()
        return row is not None

    def apply_due_replacements(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, int]]:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM quant.paper_live_order_intents
                WHERE status IN ('PROCESSING','WORKING')
                  AND replace_request_ts IS NOT NULL
                  AND replace_arrival_ts <= clock_timestamp()
                  AND (
                    cancel_arrival_ts IS NULL
                    OR replace_arrival_ts < cancel_arrival_ts
                  )
                  AND replace_intent_id IS NULL
                  AND order_state NOT IN (
                    'VENUE_DELAYED','CANCELED','CONFIRMED','REJECTED','FAILED_REVERSED'
                  )
                ORDER BY replace_arrival_ts, intent_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
            due = [dict(row) for row in cur.fetchall()]
            replacements: list[dict[str, int]] = []
            for old in due:
                epoch = int(old.get("queue_priority_epoch") or 0) + 1
                new_client_order_id = f"{old['client_order_id']}:r{epoch}"
                cur.execute(
                    """
                    INSERT INTO quant.paper_live_order_intents (
                        strategy_id, client_order_id, market_id, condition_id,
                        asset_id, side, time_in_force, limit_price, size,
                        amount_unit, tick_size, min_order_size, fee_rate,
                        fee_exponent, fee_taker_only, post_only, decision_ts,
                        expires_at, remaining_size, replaced_from_intent_id,
                        queue_priority_epoch
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s
                    )
                    ON CONFLICT (strategy_id, client_order_id) DO UPDATE SET
                        updated_at=quant.paper_live_order_intents.updated_at
                    RETURNING intent_id
                    """,
                    (
                        old["strategy_id"],
                        new_client_order_id,
                        old["market_id"],
                        old["condition_id"],
                        old["asset_id"],
                        old["side"],
                        old["time_in_force"],
                        old["replace_limit_price"],
                        old["replace_size"],
                        old["amount_unit"],
                        old["tick_size"],
                        old["min_order_size"],
                        old["fee_rate"],
                        old["fee_exponent"],
                        old["fee_taker_only"],
                        old["post_only"],
                        old["replace_arrival_ts"],
                        old["expires_at"],
                        old["replace_size"],
                        old["intent_id"],
                        epoch,
                    ),
                )
                replacement = cur.fetchone()
                assert replacement is not None
                new_intent_id = int(replacement["intent_id"])
                cur.execute(
                    """
                    UPDATE quant.paper_live_order_intents
                    SET status='CANCELED', order_state='CANCELED',
                        replace_intent_id=%s, completed_at=clock_timestamp(),
                        last_error='replaced_by_new_intent',
                        updated_at=clock_timestamp()
                    WHERE intent_id=%s
                    """,
                    (new_intent_id, int(old["intent_id"])),
                )
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{old['intent_id']}:replace-arrived",
                    intent_id=int(old["intent_id"]),
                    event_type="REPLACED",
                    from_state="REPLACE_REQUESTED",
                    to_state="CANCELED",
                    reason="replace_arrived_old_order_canceled",
                    payload={"replacement_intent_id": new_intent_id},
                    event_ts=old["replace_arrival_ts"],
                )
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{new_intent_id}:created-by-replace",
                    intent_id=new_intent_id,
                    event_type="CREATED",
                    from_state=None,
                    to_state="CREATED",
                    reason="replace_arrived_new_order_created",
                    payload={
                        "replaced_from_intent_id": int(old["intent_id"]),
                        "queue_priority_epoch": epoch,
                    },
                    event_ts=old["replace_arrival_ts"],
                )
                replacements.append(
                    {
                        "old_intent_id": int(old["intent_id"]),
                        "new_intent_id": new_intent_id,
                    }
                )
            conn.commit()
        return replacements

    def upsert_paired_probe(self, row: dict[str, Any]) -> dict[str, Any]:
        """Persist one A/B/C probe as a single idempotent audit record."""

        required = (
            "probe_id",
            "strategy_id",
            "client_order_id",
            "mode",
            "status",
            "market_id",
            "condition_id",
            "asset_id",
            "decision_ts",
        )
        missing = [field for field in required if row.get(field) in (None, "")]
        if missing:
            raise ValueError(
                f"paired probe missing required fields: {', '.join(missing)}"
            )
        values = {
            **row,
            "paper_intent_id": row.get("paper_intent_id"),
            "arrival_ts": row.get("arrival_ts"),
            "paper_prediction": json.dumps(
                _json_value(row.get("paper_prediction") or {})
            ),
            "live_lifecycle": json.dumps(_json_value(row.get("live_lifecycle") or {})),
            "orderfilled_ex_self": json.dumps(
                _json_value(row.get("orderfilled_ex_self") or {})
            ),
            "bucket_context": json.dumps(_json_value(row.get("bucket_context") or {})),
            "audit": json.dumps(_json_value(row.get("audit") or {})),
        }
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_paired_probes (
                    probe_id, strategy_id, client_order_id, paper_intent_id,
                    mode, status, market_id, condition_id, asset_id,
                    decision_ts, arrival_ts, paper_prediction, live_lifecycle,
                    orderfilled_ex_self, bucket_context, audit
                ) VALUES (
                    %(probe_id)s, %(strategy_id)s, %(client_order_id)s, %(paper_intent_id)s,
                    %(mode)s, %(status)s, %(market_id)s, %(condition_id)s, %(asset_id)s,
                    %(decision_ts)s, %(arrival_ts)s, %(paper_prediction)s::jsonb,
                    %(live_lifecycle)s::jsonb, %(orderfilled_ex_self)s::jsonb,
                    %(bucket_context)s::jsonb, %(audit)s::jsonb
                )
                ON CONFLICT (probe_id) DO UPDATE SET
                    paper_intent_id=COALESCE(EXCLUDED.paper_intent_id, quant.paper_paired_probes.paper_intent_id),
                    mode=EXCLUDED.mode, status=EXCLUDED.status,
                    arrival_ts=COALESCE(EXCLUDED.arrival_ts, quant.paper_paired_probes.arrival_ts),
                    paper_prediction=EXCLUDED.paper_prediction,
                    live_lifecycle=EXCLUDED.live_lifecycle,
                    orderfilled_ex_self=EXCLUDED.orderfilled_ex_self,
                    bucket_context=EXCLUDED.bucket_context,
                    audit=EXCLUDED.audit,
                    updated_at=clock_timestamp()
                RETURNING *
                """,
                values,
            )
            persisted = dict(cur.fetchone())
            conn.commit()
        return persisted

    def load_paired_probe(self, probe_id: str) -> dict[str, Any] | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.paper_paired_probes WHERE probe_id=%s",
                (str(probe_id),),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def load_paired_probes(
        self,
        *,
        limit: int = 500,
        mode: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM quant.paper_paired_probes
                WHERE (%s::text IS NULL OR mode=%s::text)
                  AND (%s::text IS NULL OR status=%s::text)
                ORDER BY decision_ts DESC, probe_id
                LIMIT %s
                """,
                (mode, mode, status, status, max(1, int(limit))),
            )
            return [dict(row) for row in cur.fetchall()]

    def fail(self, intent_id: int, error: str) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_live_order_intents
                SET status='FAILED', order_state='REJECTED',
                    completed_at=clock_timestamp(), last_error=%s,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s
                RETURNING intent_id
                """,
                (str(error)[:1000], int(intent_id)),
            )
            if cur.fetchone() is not None:
                _insert_order_event(
                    cur,
                    idempotency_key=f"paper-order:{intent_id}:failed",
                    intent_id=int(intent_id),
                    event_type="REJECTED",
                    from_state="ACCEPTED",
                    to_state="REJECTED",
                    reason=str(error)[:1000],
                )
            conn.commit()

    def counts(self) -> dict[str, int]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE status='QUEUED') AS queued,
                       count(*) FILTER (WHERE status='PROCESSING') AS processing,
                       count(*) FILTER (WHERE status='COMPLETED') AS completed,
                       count(*) FILTER (WHERE status='WORKING') AS working,
                       count(*) FILTER (WHERE status='FAILED') AS failed,
                       count(*) FILTER (WHERE status='CANCELED') AS canceled,
                       count(*) FILTER (WHERE status='EXPIRED') AS expired
                FROM quant.paper_live_order_intents
                """
            )
            return {key: int(value or 0) for key, value in dict(cur.fetchone()).items()}

    def write_health(self, worker_id: str, payload: dict[str, Any]) -> None:
        sample = dict(payload)
        sample.setdefault(
            "sampled_at",
            sample.get("updated_at") or datetime.now().astimezone(),
        )
        self.write_health_batch(worker_id, [sample])

    def write_health_batch(
        self,
        worker_id: str,
        payloads: Iterable[dict[str, Any]],
    ) -> None:
        samples = [dict(payload) for payload in payloads]
        if not samples:
            return
        for sample in samples:
            sample.setdefault(
                "sampled_at",
                sample.get("updated_at") or datetime.now().astimezone(),
            )
        latest = samples[-1]
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            values = _health_values(worker_id, latest)
            cur.execute(
                """
                INSERT INTO quant.paper_live_shadow_health (
                    worker_id, transport_state, watched_assets, ready_books,
                    execution_watched_assets, execution_ready_books,
                    execution_fresh_books, fresh_books, stale_books,
                    queued_intents, processing_intents, completed_intents, rejected_intents,
                    websocket_messages, reconnects, settled_positions,
                    feed_mismatch_assets, route_states, route_proxy_urls,
                    route_messages, route_reconnects, backpressure_status,
                    backpressure_reasons, last_message_at, last_error, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                          %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (worker_id) DO UPDATE SET
                    transport_state=EXCLUDED.transport_state,
                    watched_assets=EXCLUDED.watched_assets, ready_books=EXCLUDED.ready_books,
                    execution_watched_assets=EXCLUDED.execution_watched_assets,
                    execution_ready_books=EXCLUDED.execution_ready_books,
                    execution_fresh_books=EXCLUDED.execution_fresh_books,
                    fresh_books=EXCLUDED.fresh_books,
                    stale_books=EXCLUDED.stale_books, queued_intents=EXCLUDED.queued_intents,
                    processing_intents=EXCLUDED.processing_intents,
                    completed_intents=EXCLUDED.completed_intents,
                    rejected_intents=EXCLUDED.rejected_intents,
                    websocket_messages=EXCLUDED.websocket_messages,
                    reconnects=EXCLUDED.reconnects,
                    settled_positions=EXCLUDED.settled_positions,
                    feed_mismatch_assets=EXCLUDED.feed_mismatch_assets,
                    route_states=EXCLUDED.route_states,
                    route_proxy_urls=EXCLUDED.route_proxy_urls,
                    route_messages=EXCLUDED.route_messages,
                    route_reconnects=EXCLUDED.route_reconnects,
                    backpressure_status=EXCLUDED.backpressure_status,
                    backpressure_reasons=EXCLUDED.backpressure_reasons,
                    last_message_at=EXCLUDED.last_message_at,
                    last_error=EXCLUDED.last_error, updated_at=EXCLUDED.updated_at
                WHERE EXCLUDED.updated_at >= quant.paper_live_shadow_health.updated_at
                """,
                values + (latest["sampled_at"],),
            )
            cur.executemany(
                """
                INSERT INTO quant.paper_live_shadow_health_samples (
                    worker_id, transport_state, watched_assets, ready_books,
                    execution_watched_assets, execution_ready_books,
                    execution_fresh_books, fresh_books, stale_books,
                    queued_intents, processing_intents, completed_intents, rejected_intents,
                    websocket_messages, reconnects, settled_positions,
                    feed_mismatch_assets, route_states, route_proxy_urls,
                    route_messages, route_reconnects, backpressure_status,
                    backpressure_reasons, last_message_at, last_error, sampled_at
                )
                SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s::jsonb,
                       %s,%s,%s
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM quant.paper_live_shadow_health_samples existing
                    WHERE existing.worker_id=%s AND existing.sampled_at=%s
                )
                """,
                [
                    _health_values(worker_id, sample)
                    + (
                        sample["sampled_at"],
                        worker_id,
                        sample["sampled_at"],
                    )
                    for sample in samples
                ],
            )
            conn.commit()

    def status(self) -> dict[str, Any]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.paper_live_shadow_health ORDER BY updated_at DESC LIMIT 1"
            )
            health = dict(cur.fetchone() or {})
        return {"health": health, "intents": self.counts()}


def _health_values(
    worker_id: str,
    payload: dict[str, Any],
) -> tuple[Any, ...]:
    return (
        worker_id,
        payload["transport_state"],
        payload["watched_assets"],
        payload["ready_books"],
        payload.get("execution_watched_assets"),
        payload.get("execution_ready_books"),
        payload.get("execution_fresh_books"),
        payload["fresh_books"],
        payload["stale_books"],
        payload["queued_intents"],
        payload["processing_intents"],
        payload["completed_intents"],
        payload["rejected_intents"],
        payload["websocket_messages"],
        payload["reconnects"],
        payload.get("settled_positions", 0),
        payload.get("feed_mismatch_assets", 0),
        json.dumps(payload.get("route_states") or {}),
        json.dumps(payload.get("route_proxy_urls") or {}),
        json.dumps(payload.get("route_messages") or {}),
        json.dumps(payload.get("route_reconnects") or {}),
        payload.get("backpressure_status") or "DEFER_OR_REJECT",
        json.dumps(payload.get("backpressure_reasons") or []),
        payload.get("last_message_at"),
        payload.get("last_error"),
    )


def _insert_order_event(
    cur: Any,
    *,
    idempotency_key: str,
    intent_id: int,
    event_type: str,
    from_state: str | None,
    to_state: str,
    reason: str | None,
    result_audit_key: str | None = None,
    checkpoint_id: str | None = None,
    payload: dict[str, Any] | None = None,
    event_ts: datetime | None = None,
) -> bool:
    cur.execute(
        """
        INSERT INTO quant.paper_order_events (
            idempotency_key, intent_id, strategy_id, client_order_id,
            event_type, from_state, to_state, reason, result_audit_key,
            checkpoint_id, payload, event_ts
        )
        SELECT %s, i.intent_id, i.strategy_id, i.client_order_id,
               %s, %s, %s, %s, %s, %s, %s::jsonb,
               COALESCE(%s, clock_timestamp())
        FROM quant.paper_live_order_intents i
        WHERE i.intent_id=%s
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            str(idempotency_key),
            str(event_type),
            from_state,
            str(to_state),
            reason,
            result_audit_key,
            checkpoint_id,
            json.dumps(_json_value(payload or {})),
            event_ts,
            int(intent_id),
        ),
    )
    return int(cur.rowcount or 0) > 0


def _datetime_to_ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def _maker_research_state(row: dict[str, Any]) -> MakerQueueState:
    last_event_ts = row.get("last_event_ts")
    return MakerQueueState(
        paper_order_id=str(row["paper_order_id"]),
        asset_id=str(row["asset_id"]),
        side=str(row["side"]).upper(),
        price_tick=Decimal(str(row["price_tick"])),
        queue_model_version=str(row["queue_model_version"]),
        displayed_size_at_accept=Decimal(str(row["displayed_size_at_accept"])),
        own_orders_ahead=Decimal(str(row["own_orders_ahead"])),
        estimated_external_queue_ahead=Decimal(
            str(row["estimated_external_queue_ahead"])
        ),
        order_size=Decimal(str(row["accepted_order_size"])),
        cumulative_trade_volume_at_price=Decimal(
            str(row.get("cumulative_trade_volume_at_price") or 0)
        ),
        cumulative_cancel_ahead_estimate=Decimal(
            str(row.get("cumulative_cancel_ahead_estimate") or 0)
        ),
        last_event_id=str(row.get("last_event_id") or ""),
        book_generation=(
            int(row["book_generation"])
            if row.get("book_generation") is not None
            else None
        ),
        queue_epoch=int(row.get("queue_epoch") or 0),
        last_event_ts_ns=(
            _datetime_to_ns(last_event_ts) if last_event_ts is not None else None
        ),
    )


def _update_maker_research_state(
    cur: Any,
    *,
    row: dict[str, Any],
    state: MakerQueueState,
    cumulative_filled_size: Decimal,
) -> None:
    persisted_state = str(row.get("state") or "WORKING").upper()
    if persisted_state == "NEEDS_REBASE" and state.book_generation is not None:
        persisted_state = (
            "PARTIAL" if cumulative_filled_size > 0 else "WORKING"
        )
    cur.execute(
        """
        UPDATE quant.maker_research_queue_states
        SET displayed_size_at_accept=%s,
            estimated_external_queue_ahead=%s,
            cumulative_trade_volume_at_price=%s,
            cumulative_cancel_ahead_estimate=%s,
            cumulative_filled_size=%s,
            state=%s,
            book_generation=%s,
            queue_epoch=%s,
            last_event_id=%s,
            last_event_ts=%s,
            updated_at=clock_timestamp()
        WHERE paper_order_id=%s AND queue_model=%s
        """,
        (
            state.displayed_size_at_accept,
            state.estimated_external_queue_ahead,
            state.cumulative_trade_volume_at_price,
            state.cumulative_cancel_ahead_estimate,
            cumulative_filled_size,
            persisted_state,
            state.book_generation,
            state.queue_epoch,
            state.last_event_id,
            _ns_to_datetime(state.last_event_ts_ns),
            row["paper_order_id"],
            row["queue_model"],
        ),
    )


def _ns_to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1_000_000_000, tz=timezone.utc)


def _json_level_size(levels: Any, price: Decimal) -> Decimal:
    total = Decimal("0")
    for level in levels or []:
        if isinstance(level, dict):
            level_price, level_size = level.get("price"), level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            level_price, level_size = level[0], level[1]
        else:
            continue
        if Decimal(str(level_price)) == price:
            total += max(Decimal("0"), Decimal(str(level_size)))
    return total


def _audit_has_working_remainder(audit: dict[str, Any]) -> bool:
    intent = dict(audit.get("intent") or {})
    return (
        str(intent.get("order_type") or "").upper() in {"GTC", "GTD"}
        and str(audit.get("status") or "").upper() in {"WORKING", "PARTIAL"}
        and Decimal(str(audit.get("remaining_size") or 0)) > 0
    )


def _recovered_order_state(audit: dict[str, Any]) -> str:
    if Decimal(str(audit.get("filled_size") or 0)) > 0:
        return "CONFIRMED"
    status = str(audit.get("status") or "").upper()
    if status in {"CANCELLED", "CANCELED"}:
        return "CANCELED"
    if status == "EXPIRED":
        return "EXPIRED"
    return "REJECTED"


def _result_payload_from_audit(audit: dict[str, Any]) -> dict[str, Any]:
    intent = dict(audit.get("intent") or {})
    fills = list(audit.get("fills") or [])
    filled_notional = sum(
        (
            Decimal(str(fill.get("price") or 0)) * Decimal(str(fill.get("size") or 0))
            for fill in fills
            if isinstance(fill, dict)
        ),
        Decimal("0"),
    )
    remaining = Decimal(str(audit.get("remaining_size") or 0))
    amount_unit = str(intent.get("amount_unit") or "SHARES").upper()
    requested_amount = Decimal(
        str(intent.get("size", audit.get("requested_size")) or 0)
    )
    remaining_amount = (
        max(Decimal("0"), requested_amount - filled_notional)
        if amount_unit == "QUOTE"
        else remaining
    )
    return _json_value(
        {
            "audit_key": audit.get("audit_key"),
            "status": audit.get("status"),
            "reason": audit.get("reason"),
            "intent": intent,
            "arrival_ts": audit.get("arrival_ts"),
            "decision_checkpoint_id": audit.get("decision_checkpoint_id"),
            "arrival_checkpoint_id": audit.get("arrival_checkpoint_id"),
            "book_generation": audit.get("book_generation"),
            "coverage_grade": audit.get("coverage_grade"),
            "book_age_ms": audit.get("book_age_ms"),
            "fills": fills,
            "requested_amount": requested_amount,
            "amount_unit": amount_unit,
            "filled_size": audit.get("filled_size"),
            "remaining_size": remaining,
            "filled_notional": filled_notional,
            "remaining_amount": remaining_amount,
            "avg_fill_price": audit.get("avg_fill_price"),
            "total_fee": audit.get("total_fee"),
            "slippage": audit.get("slippage"),
            "source_manifest_ids": audit.get("source_manifest_ids") or [],
            "source_files": audit.get("source_files") or [],
            "source_event_start": audit.get("source_event_start"),
            "source_event_end": audit.get("source_event_end"),
            "rest_audit_at": audit.get("rest_audit_at"),
            "model_version": audit.get("model_version"),
            "config_hash": audit.get("config_hash"),
            "fidelity": audit.get("fidelity") or {},
            "worker_recovered_from_durable_audit": True,
        }
    )


def _intent_from_row(row: dict[str, Any]) -> OrderIntent:
    return OrderIntent(
        strategy_id=str(row["strategy_id"]),
        market_id=str(row["market_id"]),
        condition_id=str(row["condition_id"]),
        asset_id=str(row["asset_id"]),
        side=str(row["side"]).upper(),
        order_type=str(row["time_in_force"]).upper(),
        limit_price=Decimal(str(row["limit_price"])),
        size=Decimal(str(row["size"])),
        post_only=bool(row["post_only"]),
        decision_ts=row["decision_ts"],
        client_order_id=str(row["client_order_id"]),
        amount_unit=str(row.get("amount_unit") or "SHARES").upper(),
        expires_at=row.get("expires_at"),
        tick_size=Decimal(str(row["tick_size"]))
        if row.get("tick_size") is not None
        else None,
        min_order_size=Decimal(str(row["min_order_size"]))
        if row.get("min_order_size") is not None
        else None,
        fee_rate=Decimal(str(row["fee_rate"]))
        if row.get("fee_rate") is not None
        else None,
        fee_exponent=Decimal(str(row["fee_exponent"]))
        if row.get("fee_exponent") is not None
        else None,
        fee_taker_only=bool(row.get("fee_taker_only", True)),
        venue_regime_id=(
            str(row["venue_regime_id"])
            if row.get("venue_regime_id") is not None
            else None
        ),
        venue_regime_source_hash=(
            str(row["venue_regime_source_hash"])
            if row.get("venue_regime_source_hash") is not None
            else None
        ),
        venue_taker_delay_ms=int(row.get("venue_taker_delay_ms") or 0),
        venue_delay_source=(
            str(row["venue_delay_source"])
            if row.get("venue_delay_source") is not None
            else None
        ),
        venue_itode=bool(row.get("venue_itode", False)),
        venue_seconds_delay=int(row.get("venue_seconds_delay") or 0),
        fee_schedule_id=(
            str(row["fee_schedule_id"])
            if row.get("fee_schedule_id") is not None
            else None
        ),
        fee_schedule_source=(
            str(row["fee_schedule_source"])
            if row.get("fee_schedule_source") is not None
            else None
        ),
        economics_regime_id=(
            str(row["economics_regime_id"])
            if row.get("economics_regime_id") is not None
            else None
        ),
        builder_code=(
            str(row["builder_code"]) if row.get("builder_code") is not None else None
        ),
        builder_taker_fee_bps=int(row.get("builder_taker_fee_bps") or 0),
        builder_maker_fee_bps=int(row.get("builder_maker_fee_bps") or 0),
    )


def _checkpoint_values(item: ArrivalBookCheckpoint) -> tuple[Any, ...]:
    connection_id, message_seq = _source_parts(item.source_event_end)
    return (
        item.checkpoint_id,
        item.asset_id,
        item.market_id,
        item.condition_id,
        item.observed_at,
        item.generation,
        item.coverage_grade,
        item.market_state,
        item.book_status,
        item.has_gap,
        json.dumps([[str(level.price), str(level.size)] for level in item.bids]),
        json.dumps([[str(level.price), str(level.size)] for level in item.asks]),
        connection_id,
        message_seq,
        item.checkpoint_id,
    )


def _checkpoint_from_row(row: dict[str, Any]) -> ArrivalBookCheckpoint:
    source_event = None
    if row.get("source_connection_id") is not None:
        source_event = str(row["source_connection_id"])
        if row.get("source_message_seq") is not None:
            source_event += f":{int(row['source_message_seq'])}"
    return ArrivalBookCheckpoint(
        checkpoint_id=str(row["checkpoint_id"]),
        asset_id=str(row["asset_id"]),
        market_id=str(row["market_id"]),
        condition_id=str(row["condition_id"]),
        observed_at=row["observed_at"],
        generation=int(row["generation"]),
        coverage_grade=str(row["coverage_grade"]),
        market_state=str(row["market_state"]),
        book_status=str(row["book_status"]),
        has_gap=bool(row["has_gap"]),
        bids=tuple(
            PaperBookLevel(Decimal(str(price)), Decimal(str(size)))
            for price, size in row["bids"]
        ),
        asks=tuple(
            PaperBookLevel(Decimal(str(price)), Decimal(str(size)))
            for price, size in row["asks"]
        ),
        source_event_start=source_event,
        source_event_end=source_event,
    )


def _source_parts(value: str | None) -> tuple[str | None, int | None]:
    if not value or ":" not in value:
        return None, None
    connection_id, raw_seq = value.rsplit(":", 1)
    try:
        return connection_id, int(raw_seq)
    except ValueError:
        return connection_id, None


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
