"""Idempotent Postgres storage for calibration models, runs, and probes."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from quant.core.db import postgres_connection

from .calibration_domain import canonical_json, redact_mapping

DEFAULT_VENUE_REGIME_ID = "polymarket-clob-v2-async-commit-20260724"


SCHEMA_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS quant",
    """
    CREATE TABLE IF NOT EXISTS quant.venue_regimes (
        venue_regime_id TEXT PRIMARY KEY,
        venue TEXT NOT NULL,
        clob_version TEXT NOT NULL,
        sdk_name TEXT NOT NULL,
        sdk_version TEXT NOT NULL,
        effective_from TIMESTAMPTZ NOT NULL,
        effective_to TIMESTAMPTZ,
        order_response_mode TEXT NOT NULL,
        fee_schedule_hash TEXT NOT NULL,
        rounding_rules_hash TEXT NOT NULL,
        matching_engine_release TEXT,
        source_changelog_url TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    INSERT INTO quant.venue_regimes (
        venue_regime_id, venue, clob_version, sdk_name, sdk_version,
        effective_from, order_response_mode, fee_schedule_hash,
        rounding_rules_hash, matching_engine_release, source_changelog_url
    ) VALUES (
        'polymarket-clob-v2-async-commit-20260724',
        'POLYMARKET', 'V2', 'py-clob-client-v2', '1.1.0',
        '2026-07-24 04:00:00+00',
        'ORDER_ID_TRADE_IDS_ASYNC_HASH',
        'market_fee_schedule_v2_dynamic',
        'clob_v2_tick_min_size_exact_decimal',
        'async_commit_20260724',
        'https://docs.polymarket.com/changelog/predictions'
    ) ON CONFLICT (venue_regime_id) DO NOTHING
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.execution_model_versions (
        model_version TEXT PRIMARY KEY,
        model_family TEXT NOT NULL,
        status TEXT NOT NULL,
        trained_run_id TEXT,
        venue_regime_id TEXT NOT NULL REFERENCES quant.venue_regimes(venue_regime_id),
        validated_domain_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        training_manifest_hash TEXT,
        holdout_manifest_hash TEXT,
        metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        code_commit TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        promoted_at TIMESTAMPTZ,
        demoted_at TIMESTAMPTZ,
        supersedes_model_version TEXT REFERENCES quant.execution_model_versions(model_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_calibration_models (
        model_version TEXT PRIMARY KEY,
        model_state TEXT NOT NULL,
        manifest_id TEXT NOT NULL UNIQUE,
        manifest JSONB NOT NULL,
        execution_config_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        frozen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        promoted_at TIMESTAMPTZ,
        validation_report JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_calibration_runs (
        run_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        run_status TEXT NOT NULL,
        model_version TEXT NOT NULL REFERENCES quant.paper_calibration_models(model_version),
        manifest_id TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        plan JSONB NOT NULL,
        expected_probe_count INTEGER NOT NULL,
        completed_probe_count INTEGER NOT NULL DEFAULT 0,
        submitted_order_count INTEGER NOT NULL DEFAULT 0,
        report JSONB NOT NULL DEFAULT '{}'::jsonb,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_calibration_probes (
        probe_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES quant.paper_calibration_runs(run_id),
        paired_probe_id TEXT,
        model_version TEXT NOT NULL,
        manifest_id TEXT NOT NULL,
        probe_state TEXT NOT NULL,
        asset_id TEXT,
        market_id TEXT,
        condition_id TEXT,
        side TEXT,
        order_type TEXT,
        amount NUMERIC,
        amount_unit TEXT,
        decision_ts TIMESTAMPTZ,
        artifact_bitmap JSONB NOT NULL DEFAULT '{}'::jsonb,
        prediction JSONB NOT NULL DEFAULT '{}'::jsonb,
        market_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        risk_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        signed_order_audit JSONB NOT NULL DEFAULT '{}'::jsonb,
        lifecycle JSONB NOT NULL DEFAULT '{}'::jsonb,
        reconciliation JSONB NOT NULL DEFAULT '{}'::jsonb,
        timestamps JSONB NOT NULL DEFAULT '{}'::jsonb,
        errors JSONB NOT NULL DEFAULT '[]'::jsonb,
        exchange_submit_called BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_calibration_events (
        event_key TEXT PRIMARY KEY,
        probe_id TEXT NOT NULL REFERENCES quant.paper_calibration_probes(probe_id),
        event_type TEXT NOT NULL,
        source TEXT NOT NULL,
        event_ts TIMESTAMPTZ,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_calibration_pnl_positions (
        account_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        market_id TEXT,
        condition_id TEXT,
        real_quantity NUMERIC NOT NULL DEFAULT 0,
        real_cost_basis NUMERIC NOT NULL DEFAULT 0,
        real_realized_pnl NUMERIC NOT NULL DEFAULT 0,
        paper_quantity NUMERIC NOT NULL DEFAULT 0,
        paper_cost_basis NUMERIC NOT NULL DEFAULT 0,
        paper_realized_pnl NUMERIC NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (account_id, asset_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_calibration_pnl_entries (
        probe_id TEXT PRIMARY KEY REFERENCES quant.paper_calibration_probes(probe_id),
        account_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        side TEXT NOT NULL,
        real_realized_pnl_delta NUMERIC NOT NULL DEFAULT 0,
        paper_realized_pnl_delta NUMERIC NOT NULL DEFAULT 0,
        pnl JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_calibration_pnl_settlements (
        settlement_key TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        winning_asset_id TEXT NOT NULL,
        resolved_at TIMESTAMPTZ,
        expected_real_payout NUMERIC NOT NULL,
        observed_real_payout NUMERIC,
        real_realized_pnl_delta NUMERIC NOT NULL,
        paper_realized_pnl_delta NUMERIC NOT NULL,
        cash_reconciliation_status TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_calibration_runs_status ON quant.paper_calibration_runs(run_status, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_paper_calibration_probes_run ON quant.paper_calibration_probes(run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_paper_calibration_probes_state ON quant.paper_calibration_probes(probe_state, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_paper_calibration_events_probe ON quant.paper_calibration_events(probe_id, event_ts, event_key)",
    "CREATE INDEX IF NOT EXISTS idx_paper_calibration_pnl_entries_account_time ON quant.paper_calibration_pnl_entries(account_id, event_ts)",
    "ALTER TABLE quant.paper_calibration_models ADD COLUMN IF NOT EXISTS validation_report JSONB NOT NULL DEFAULT '{}'::jsonb",
    f"ALTER TABLE quant.paper_calibration_models ADD COLUMN IF NOT EXISTS venue_regime_id TEXT NOT NULL DEFAULT '{DEFAULT_VENUE_REGIME_ID}' REFERENCES quant.venue_regimes(venue_regime_id)",
    f"ALTER TABLE quant.paper_calibration_runs ADD COLUMN IF NOT EXISTS venue_regime_id TEXT NOT NULL DEFAULT '{DEFAULT_VENUE_REGIME_ID}' REFERENCES quant.venue_regimes(venue_regime_id)",
    f"ALTER TABLE quant.paper_calibration_probes ADD COLUMN IF NOT EXISTS venue_regime_id TEXT NOT NULL DEFAULT '{DEFAULT_VENUE_REGIME_ID}' REFERENCES quant.venue_regimes(venue_regime_id)",
    "CREATE INDEX IF NOT EXISTS idx_paper_calibration_probe_regime ON quant.paper_calibration_probes(venue_regime_id, decision_ts)",
    "CREATE INDEX IF NOT EXISTS idx_execution_model_status ON quant.execution_model_versions(model_family, status, created_at DESC)",
)


def _pnl_state(
    payload: Mapping[str, Any],
    leg_name: str,
    *,
    before: bool,
) -> Any | None:
    from .pnl import PositionPnlState

    leg = payload.get(leg_name) if isinstance(payload.get(leg_name), Mapping) else None
    if leg is None:
        return None
    position_key = "position_before" if before else "position_after"
    cost_key = "cost_basis_before" if before else "cost_basis_after"
    required = {position_key, cost_key, "realized_pnl_total", "realized_pnl_delta"}
    if not required.issubset(leg):
        return None
    try:
        realized_total = Decimal(str(leg["realized_pnl_total"]))
        realized_delta = Decimal(str(leg["realized_pnl_delta"]))
        return PositionPnlState(
            quantity=Decimal(str(leg[position_key])),
            cost_basis=Decimal(str(leg[cost_key])),
            realized_pnl=realized_total - realized_delta if before else realized_total,
        )
    except Exception:
        return None


class CalibrationStore:
    def __init__(self, connection_factory: Any = postgres_connection) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def freeze_model(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        model_version = str(manifest["paper_execution_model_version"])
        manifest_id = str(manifest["manifest_id"])
        config_hash = str(manifest["execution_config_hash"])
        venue_regime_id = str(
            manifest.get("venue_regime_id") or DEFAULT_VENUE_REGIME_ID
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_calibration_models (
                    model_version, model_state, manifest_id, manifest,
                    execution_config_hash, venue_regime_id
                ) VALUES (%s,%s,%s,%s::jsonb,%s,%s)
                ON CONFLICT (model_version) DO NOTHING
                RETURNING *
                """,
                (
                    model_version,
                    str(manifest["model_state"]),
                    manifest_id,
                    canonical_json(manifest),
                    config_hash,
                    venue_regime_id,
                ),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT * FROM quant.paper_calibration_models WHERE model_version=%s",
                    (model_version,),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("failed to freeze calibration model")
                if str(row["manifest_id"]) != manifest_id:
                    raise RuntimeError(
                        f"model_version {model_version} is already frozen at manifest {row['manifest_id']}"
                    )
            cur.execute(
                """
                INSERT INTO quant.execution_model_versions (
                    model_version, model_family, status, venue_regime_id,
                    training_manifest_hash, code_commit, config_hash
                ) VALUES (%s,'TAKER_L2',%s,%s,%s,%s,%s)
                ON CONFLICT (model_version) DO NOTHING
                """,
                (
                    model_version,
                    str(manifest["model_state"]),
                    venue_regime_id,
                    manifest_id,
                    str(manifest.get("git_commit") or "UNKNOWN"),
                    config_hash,
                ),
            )
            conn.commit()
            return dict(row)

    def create_run(self, row: Mapping[str, Any]) -> dict[str, Any]:
        venue_regime_id = str(row.get("venue_regime_id") or DEFAULT_VENUE_REGIME_ID)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_calibration_runs (
                    run_id, mode, run_status, model_version, manifest_id, plan_hash,
                    plan, expected_probe_count, started_at, venue_regime_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (run_id) DO UPDATE SET
                    run_status=EXCLUDED.run_status,
                    updated_at=clock_timestamp()
                WHERE quant.paper_calibration_runs.manifest_id=EXCLUDED.manifest_id
                  AND quant.paper_calibration_runs.plan_hash=EXCLUDED.plan_hash
                RETURNING *
                """,
                (
                    row["run_id"],
                    row["mode"],
                    row["run_status"],
                    row["model_version"],
                    row["manifest_id"],
                    row["plan_hash"],
                    canonical_json(row["plan"]),
                    int(row["expected_probe_count"]),
                    row["started_at"],
                    venue_regime_id,
                ),
            )
            persisted = cur.fetchone()
            if persisted is None:
                raise RuntimeError(
                    "run_id already exists with a different frozen plan or manifest"
                )
            conn.commit()
            return dict(persisted)

    def transition_run_status(
        self,
        run_id: str,
        *,
        expected: str,
        target: str,
    ) -> dict[str, Any]:
        """Compare-and-set a run status so an approved run cannot execute twice."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_calibration_runs
                SET run_status=%s, updated_at=clock_timestamp()
                WHERE run_id=%s AND run_status=%s
                RETURNING *
                """,
                (str(target), str(run_id), str(expected)),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT run_status FROM quant.paper_calibration_runs WHERE run_id=%s",
                    (str(run_id),),
                )
                current = cur.fetchone()
                state = str(current["run_status"]) if current else "MISSING"
                raise RuntimeError(
                    f"run status transition rejected: {run_id} is {state}, expected {expected}"
                )
            conn.commit()
            return dict(row)

    def upsert_probe(self, row: Mapping[str, Any]) -> dict[str, Any]:
        json_fields = (
            "artifact_bitmap",
            "prediction",
            "market_snapshot",
            "risk_snapshot",
            "signed_order_audit",
            "lifecycle",
            "reconciliation",
            "timestamps",
            "errors",
        )
        values = dict(row)
        values["venue_regime_id"] = str(
            values.get("venue_regime_id") or DEFAULT_VENUE_REGIME_ID
        )
        for field in json_fields:
            values[field] = canonical_json(
                values.get(field) or ([] if field == "errors" else {})
            )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_calibration_probes (
                    probe_id, run_id, paired_probe_id, model_version, manifest_id,
                    probe_state, asset_id, market_id, condition_id, side, order_type,
                    amount, amount_unit, decision_ts, artifact_bitmap, prediction,
                    market_snapshot, risk_snapshot, signed_order_audit, lifecycle,
                    reconciliation, timestamps, errors, exchange_submit_called,
                    venue_regime_id
                ) VALUES (
                    %(probe_id)s,%(run_id)s,%(paired_probe_id)s,%(model_version)s,%(manifest_id)s,
                    %(probe_state)s,%(asset_id)s,%(market_id)s,%(condition_id)s,%(side)s,%(order_type)s,
                    %(amount)s,%(amount_unit)s,%(decision_ts)s,%(artifact_bitmap)s::jsonb,%(prediction)s::jsonb,
                    %(market_snapshot)s::jsonb,%(risk_snapshot)s::jsonb,%(signed_order_audit)s::jsonb,
                    %(lifecycle)s::jsonb,%(reconciliation)s::jsonb,%(timestamps)s::jsonb,
                    %(errors)s::jsonb,%(exchange_submit_called)s,%(venue_regime_id)s
                )
                ON CONFLICT (probe_id) DO UPDATE SET
                    paired_probe_id=COALESCE(EXCLUDED.paired_probe_id, quant.paper_calibration_probes.paired_probe_id),
                    probe_state=EXCLUDED.probe_state,
                    artifact_bitmap=EXCLUDED.artifact_bitmap,
                    prediction=EXCLUDED.prediction,
                    market_snapshot=EXCLUDED.market_snapshot,
                    risk_snapshot=EXCLUDED.risk_snapshot,
                    signed_order_audit=EXCLUDED.signed_order_audit,
                    lifecycle=EXCLUDED.lifecycle,
                    reconciliation=EXCLUDED.reconciliation,
                    timestamps=EXCLUDED.timestamps,
                    errors=EXCLUDED.errors,
                    venue_regime_id=EXCLUDED.venue_regime_id,
                    exchange_submit_called=(quant.paper_calibration_probes.exchange_submit_called OR EXCLUDED.exchange_submit_called),
                    updated_at=clock_timestamp()
                WHERE quant.paper_calibration_probes.run_id=EXCLUDED.run_id
                  AND quant.paper_calibration_probes.manifest_id=EXCLUDED.manifest_id
                RETURNING *
                """,
                values,
            )
            persisted = cur.fetchone()
            if persisted is None:
                raise RuntimeError(
                    "probe_id already exists under a different run or manifest"
                )
            conn.commit()
            return dict(persisted)

    def append_events(self, rows: Iterable[Mapping[str, Any]]) -> int:
        values = [
            (
                row["event_key"],
                row["probe_id"],
                row["event_type"],
                row["source"],
                row.get("event_ts"),
                canonical_json(row.get("payload") or {}),
            )
            for row in rows
        ]
        if not values:
            return 0
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO quant.paper_calibration_events (
                    event_key, probe_id, event_type, source, event_ts, payload
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (event_key) DO NOTHING
                """,
                values,
            )
            written = int(cur.rowcount or 0)
            conn.commit()
            return written

    def finish_run(
        self, run_id: str, *, status: str, report: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_calibration_runs run
                SET run_status=%s,
                    completed_probe_count=(
                        SELECT count(*) FROM quant.paper_calibration_probes p
                        WHERE p.run_id=run.run_id
                    ),
                    submitted_order_count=(
                        SELECT count(*) FROM quant.paper_calibration_probes p
                        WHERE p.run_id=run.run_id AND p.exchange_submit_called=TRUE
                    ),
                    report=%s::jsonb, completed_at=clock_timestamp(), updated_at=clock_timestamp()
                WHERE run_id=%s
                RETURNING *
                """,
                (str(status), canonical_json(report), str(run_id)),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"unknown calibration run: {run_id}")
            conn.commit()
            return dict(row)

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.paper_calibration_runs WHERE run_id=%s",
                (str(run_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def load_model(self, model_version: str) -> dict[str, Any] | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.paper_calibration_models WHERE model_version=%s",
                (str(model_version),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def promote_model(
        self,
        *,
        run_id: str,
        model_version: str,
        report: Mapping[str, Any],
    ) -> dict[str, Any]:
        from .calibration_report import assert_promotion_report

        assert_promotion_report(report)
        dataset_manifest = report.get("dataset_manifest")
        campaign = (
            dataset_manifest
            if isinstance(dataset_manifest, Mapping)
            and str(dataset_manifest.get("schema_version"))
            == "taker_calibration_dataset_manifest_v1"
            else None
        )
        campaign_source = campaign.get("source") if isinstance(campaign, Mapping) else {}
        campaign_source = campaign_source if isinstance(campaign_source, Mapping) else {}
        expected_run_id = (
            str(campaign_source.get("anchor_run_id") or "")
            if campaign is not None
            else str(report.get("run_id") or "")
        )
        if expected_run_id != str(run_id):
            raise RuntimeError(
                "calibration report run_id does not match promotion request"
            )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT mode, run_status, model_version, submitted_order_count
                FROM quant.paper_calibration_runs WHERE run_id=%s
                """,
                (str(run_id),),
            )
            run = cur.fetchone()
            if run is None:
                raise RuntimeError("calibration run does not exist")
            if str(run["mode"]) != "live":
                raise RuntimeError(
                    "only a live calibration run can anchor model promotion"
                )
            if str(run["model_version"]) != str(model_version):
                raise RuntimeError(
                    "calibration run belongs to a different model version"
                )
            if campaign is None:
                if str(run["run_status"]) != "PASS":
                    raise RuntimeError(
                        "only a passing live calibration run can promote a model"
                    )
                if int(run["submitted_order_count"] or 0) < 100:
                    raise RuntimeError(
                        "promotion requires at least 100 submitted paired probes"
                    )
            else:
                self._assert_campaign_promotion_inputs(
                    cur,
                    campaign=campaign,
                    model_version=model_version,
                    venue_regime_id=str(run.get("venue_regime_id") or ""),
                )
            cur.execute(
                """
                UPDATE quant.paper_calibration_models
                SET model_state='CALIBRATED_ACTIVE', promoted_at=clock_timestamp(),
                    validation_report=%s::jsonb,
                    updated_at=clock_timestamp()
                WHERE model_version=%s
                  AND model_state IN (
                    'CALIBRATING','CANDIDATE','HOLDOUT_PASSED','SHADOW_PROMOTED'
                  )
                RETURNING *
                """,
                (canonical_json(report), str(model_version)),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("model is missing or not in CALIBRATING state")
            cur.execute(
                """
                UPDATE quant.execution_model_versions
                SET status='CALIBRATED_ACTIVE', trained_run_id=%s,
                    metrics_json=%s::jsonb, promoted_at=clock_timestamp()
                WHERE model_version=%s
                  AND status IN ('CALIBRATING','CANDIDATE','HOLDOUT_PASSED','SHADOW_PROMOTED')
                """,
                (str(run_id), canonical_json(report), str(model_version)),
            )
            conn.commit()
            return dict(row)

    @staticmethod
    def _assert_campaign_promotion_inputs(
        cur: Any,
        *,
        campaign: Mapping[str, Any],
        model_version: str,
        venue_regime_id: str,
    ) -> None:
        entries = campaign.get("entries")
        if not isinstance(entries, list) or not entries:
            raise RuntimeError("campaign promotion requires a non-empty frozen manifest")
        probe_ids = [
            str(entry.get("probe_id") or "")
            for entry in entries
            if isinstance(entry, Mapping)
        ]
        if len(probe_ids) != len(entries) or len(set(probe_ids)) != len(probe_ids) or not all(probe_ids):
            raise RuntimeError("campaign manifest has invalid probe ids")
        cur.execute(
            """
            SELECT p.probe_id, p.probe_state, p.exchange_submit_called,
                   p.model_version, p.manifest_id, p.venue_regime_id, r.mode
            FROM quant.paper_calibration_probes p
            JOIN quant.paper_calibration_runs r ON r.run_id=p.run_id
            WHERE p.probe_id=ANY(%s)
            """,
            (probe_ids,),
        )
        rows = [dict(row) for row in cur.fetchall()]
        if {str(row["probe_id"]) for row in rows} != set(probe_ids):
            raise RuntimeError("campaign source probes are no longer available")
        source = campaign.get("source")
        source = source if isinstance(source, Mapping) else {}
        manifest_id = str(source.get("model_manifest_id") or "")
        invalid = [
            row
            for row in rows
            if str(row.get("mode")) != "live"
            or str(row.get("model_version")) != str(model_version)
            or str(row.get("manifest_id")) != manifest_id
            or str(row.get("venue_regime_id")) != str(venue_regime_id)
        ]
        if invalid:
            raise RuntimeError("campaign probes no longer match the anchored model manifest")
        submitted = sum(bool(row.get("exchange_submit_called")) for row in rows)
        if submitted < 100:
            raise RuntimeError("campaign promotion requires at least 100 submitted probes")

    def register_execution_model_version(
        self,
        *,
        model_version: str,
        model_family: str,
        trained_run_id: str,
        venue_regime_id: str,
        validated_domain: Mapping[str, Any],
        training_manifest_hash: str,
        holdout_manifest_hash: str,
        metrics: Mapping[str, Any],
        code_commit: str,
        config_hash: str,
        supersedes_model_version: str | None = None,
    ) -> dict[str, Any]:
        """Register an immutable non-taker execution model candidate."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.execution_model_versions (
                    model_version, model_family, status, trained_run_id,
                    venue_regime_id, validated_domain_json,
                    training_manifest_hash, holdout_manifest_hash, metrics_json,
                    code_commit, config_hash, supersedes_model_version
                ) VALUES (
                    %s,%s,'HOLDOUT_PASSED',%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s
                )
                ON CONFLICT (model_version) DO NOTHING
                RETURNING *
                """,
                (
                    str(model_version),
                    str(model_family),
                    str(trained_run_id),
                    str(venue_regime_id),
                    canonical_json(validated_domain),
                    str(training_manifest_hash),
                    str(holdout_manifest_hash),
                    canonical_json(metrics),
                    str(code_commit),
                    str(config_hash),
                    supersedes_model_version,
                ),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT * FROM quant.execution_model_versions WHERE model_version=%s",
                    (str(model_version),),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("failed to register execution model")
                immutable = {
                    "model_family": str(model_family),
                    "trained_run_id": str(trained_run_id),
                    "venue_regime_id": str(venue_regime_id),
                    "training_manifest_hash": str(training_manifest_hash),
                    "holdout_manifest_hash": str(holdout_manifest_hash),
                    "code_commit": str(code_commit),
                    "config_hash": str(config_hash),
                }
                if any(
                    str(row[key] or "") != value for key, value in immutable.items()
                ):
                    raise RuntimeError(
                        f"model_version {model_version} already exists with different immutable evidence"
                    )
            conn.commit()
            return dict(row)

    def promote_execution_model_version(
        self,
        *,
        model_version: str,
        model_family: str,
        venue_regime_id: str,
        evaluation: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Promote an immutable candidate while demoting the previous active version."""

        if str(evaluation.get("status")) != "PASS":
            raise RuntimeError("only a passing holdout evaluation can promote a model")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.execution_model_versions
                WHERE model_version=%s AND model_family=%s AND venue_regime_id=%s
                """,
                (str(model_version), str(model_family), str(venue_regime_id)),
            )
            current = cur.fetchone()
            if current is not None and str(current["status"]) == "CALIBRATED_ACTIVE":
                if canonical_json(current["metrics_json"]) != canonical_json(evaluation):
                    raise RuntimeError(
                        "active model version cannot be reused with different evaluation evidence"
                    )
                return dict(current)
            cur.execute(
                """
                UPDATE quant.execution_model_versions
                SET status='CALIBRATED_STALE', demoted_at=clock_timestamp()
                WHERE model_family=%s AND venue_regime_id=%s
                  AND status='CALIBRATED_ACTIVE' AND model_version<>%s
                """,
                (str(model_family), str(venue_regime_id), str(model_version)),
            )
            cur.execute(
                """
                UPDATE quant.execution_model_versions
                SET status='CALIBRATED_ACTIVE', promoted_at=clock_timestamp(),
                    metrics_json=%s::jsonb
                WHERE model_version=%s AND model_family=%s
                  AND venue_regime_id=%s AND status='HOLDOUT_PASSED'
                RETURNING *
                """,
                (
                    canonical_json(evaluation),
                    str(model_version),
                    str(model_family),
                    str(venue_regime_id),
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(
                    "maker model is missing or not in HOLDOUT_PASSED state"
                )
            conn.commit()
            return dict(row)

    def mark_models_stale(
        self,
        *,
        venue_regime_id: str,
        model_family: str = "TAKER_L2",
        reason: str,
    ) -> int:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.execution_model_versions
                SET status='CALIBRATED_STALE', demoted_at=clock_timestamp(),
                    metrics_json=metrics_json || jsonb_build_object(
                        'demotion_reason', %s,
                        'demoted_at', clock_timestamp()
                    )
                WHERE venue_regime_id=%s AND model_family=%s
                  AND status='CALIBRATED_ACTIVE'
                """,
                (str(reason), str(venue_regime_id), str(model_family)),
            )
            changed = int(cur.rowcount or 0)
            conn.commit()
            return changed

    def load_events(self, probe_id: str) -> list[dict[str, Any]]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_calibration_events
                WHERE probe_id=%s ORDER BY event_ts, event_key
                """,
                (str(probe_id),),
            )
            return [dict(row) for row in cur.fetchall()]

    def load_probes(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_calibration_probes
                WHERE run_id=%s ORDER BY created_at, probe_id
                """,
                (str(run_id),),
            )
            return [dict(row) for row in cur.fetchall()]

    def load_linked_live_probes(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return externally submitted live probes linked to paper A legs."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.*
                FROM quant.paper_calibration_probes p
                JOIN quant.paper_calibration_runs r ON r.run_id=p.run_id
                LEFT JOIN quant.paper_paired_probes paired
                  ON paired.probe_id=p.paired_probe_id
                WHERE r.mode='live'
                  AND p.exchange_submit_called
                  AND p.paired_probe_id IS NOT NULL
                  AND (
                    paired.probe_id IS NULL
                    OR paired.mode <> 'record-only'
                    OR COALESCE(paired.live_lifecycle->>'state', '') <> 'TERMINAL'
                    OR COALESCE(
                        (paired.orderfilled_ex_self->>'source_coverage_complete')::boolean,
                        FALSE
                    ) IS NOT TRUE
                    OR (
                        p.probe_state='CALIBRATABLE'
                        AND jsonb_array_length(
                            COALESCE(p.lifecycle->'transaction_hashes', '[]'::jsonb)
                        ) > 0
                        AND COALESCE(
                            (paired.orderfilled_ex_self->>'transaction_confirmation_complete')::boolean,
                            FALSE
                        ) IS NOT TRUE
                    )
                  )
                ORDER BY p.decision_ts DESC, p.probe_id
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
            return [dict(row) for row in cur.fetchall()]

    def load_probes_by_ids(self, probe_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Load a frozen probe set without trusting a report's derived metrics."""

        values = sorted({str(value) for value in probe_ids if str(value)})
        if not values:
            return []
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_calibration_probes
                WHERE probe_id=ANY(%s)
                ORDER BY created_at, probe_id
                """,
                (values,),
            )
            return [dict(row) for row in cur.fetchall()]

    def load_live_probes_for_model(
        self,
        *,
        model_version: str,
        manifest_id: str,
        venue_regime_id: str,
    ) -> list[dict[str, Any]]:
        """Return the full live history for one frozen taker model manifest."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.*
                FROM quant.paper_calibration_probes p
                JOIN quant.paper_calibration_runs r ON r.run_id=p.run_id
                WHERE r.mode='live'
                  AND p.model_version=%s
                  AND p.manifest_id=%s
                  AND p.venue_regime_id=%s
                ORDER BY p.decision_ts NULLS LAST, p.created_at, p.probe_id
                """,
                (str(model_version), str(manifest_id), str(venue_regime_id)),
            )
            return [dict(row) for row in cur.fetchall()]

    def recent_usage(self, *, since: datetime) -> dict[str, Any]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE exchange_submit_called) AS submitted_orders,
                       COALESCE(sum(
                           CASE
                               WHEN NOT exchange_submit_called THEN 0
                               WHEN amount_unit='QUOTE' THEN amount
                               ELSE COALESCE(
                                   NULLIF(risk_snapshot->'checks'->>'order_notional', '')::numeric,
                                   0
                               )
                           END
                       ), 0) AS gross_quote_amount
                FROM quant.paper_calibration_probes
                WHERE created_at >= %s
                """,
                (since,),
            )
            return dict(cur.fetchone())

    def apply_probe_pnl(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Apply a confirmed paired fill once and return its portfolio PnL."""

        from .pnl import PositionPnlState, build_paired_pnl

        lifecycle = (
            row.get("lifecycle") if isinstance(row.get("lifecycle"), Mapping) else {}
        )
        account_before = (
            lifecycle.get("account_before")
            if isinstance(lifecycle.get("account_before"), Mapping)
            else {}
        )
        account_id = str(account_before.get("funder_address") or "").lower()
        asset_id = str(row.get("asset_id") or "")
        reconciliation = (
            row.get("reconciliation")
            if isinstance(row.get("reconciliation"), Mapping)
            else {}
        )
        truth = (
            reconciliation.get("order")
            if isinstance(reconciliation.get("order"), Mapping)
            else {}
        )
        if not account_id or not asset_id:
            return {
                "schema_version": "calibration_pnl_reconciliation_v1",
                "status": "MISSING_ACCOUNT_ID",
                "pnl_reconciled": False,
            }
        try:
            actual_size = Decimal(str(truth.get("actual_matched_size") or 0))
        except Exception:
            actual_size = Decimal("0")
        if actual_size <= 0:
            payload, _, _ = build_paired_pnl(row, truth)
            return payload

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.paper_calibration_pnl_entries WHERE probe_id=%s",
                (str(row["probe_id"]),),
            )
            existing = cur.fetchone()
            if existing is not None:
                old_payload = dict(existing["pnl"])
                old_real_before = _pnl_state(old_payload, "real", before=True)
                old_paper_before = _pnl_state(old_payload, "paper", before=True)
                if old_real_before is None or old_paper_before is None:
                    return old_payload
                payload, real_after, paper_after = build_paired_pnl(
                    row,
                    truth,
                    real_state=old_real_before,
                    paper_state=old_paper_before,
                )
                if canonical_json(payload) == canonical_json(old_payload):
                    return old_payload
                cur.execute(
                    """
                    SELECT count(*) AS later_count
                    FROM quant.paper_calibration_pnl_entries
                    WHERE account_id=%s AND asset_id=%s
                      AND (event_ts, probe_id) > (%s, %s)
                    """,
                    (
                        str(existing["account_id"]),
                        str(existing["asset_id"]),
                        existing["event_ts"],
                        str(existing["probe_id"]),
                    ),
                )
                if int(cur.fetchone()["later_count"] or 0) > 0:
                    return {
                        "schema_version": "calibration_pnl_reconciliation_v1",
                        "status": "CORRECTION_REQUIRES_LEDGER_REBUILD",
                        "pnl_reconciled": False,
                        "account_id": str(existing["account_id"]),
                        "asset_id": str(existing["asset_id"]),
                    }
                cur.execute(
                    """
                    SELECT * FROM quant.paper_calibration_pnl_positions
                    WHERE account_id=%s AND asset_id=%s FOR UPDATE
                    """,
                    (str(existing["account_id"]), str(existing["asset_id"])),
                )
                position = cur.fetchone()
                old_real_after = _pnl_state(old_payload, "real", before=False)
                old_paper_after = _pnl_state(old_payload, "paper", before=False)
                if (
                    position is None
                    or old_real_after is None
                    or old_paper_after is None
                    or Decimal(str(position["real_quantity"]))
                    != old_real_after.quantity
                    or Decimal(str(position["real_cost_basis"]))
                    != old_real_after.cost_basis
                    or Decimal(str(position["real_realized_pnl"]))
                    != old_real_after.realized_pnl
                    or Decimal(str(position["paper_quantity"]))
                    != old_paper_after.quantity
                    or Decimal(str(position["paper_cost_basis"]))
                    != old_paper_after.cost_basis
                    or Decimal(str(position["paper_realized_pnl"]))
                    != old_paper_after.realized_pnl
                ):
                    return {
                        "schema_version": "calibration_pnl_reconciliation_v1",
                        "status": "CORRECTION_REQUIRES_LEDGER_REBUILD",
                        "pnl_reconciled": False,
                        "account_id": str(existing["account_id"]),
                        "asset_id": str(existing["asset_id"]),
                    }
                cur.execute(
                    """
                    UPDATE quant.paper_calibration_pnl_entries
                    SET real_realized_pnl_delta=%s,
                        paper_realized_pnl_delta=%s,
                        pnl=%s::jsonb
                    WHERE probe_id=%s
                    """,
                    (
                        payload.get("real", {}).get("realized_pnl_delta", 0),
                        payload.get("paper", {}).get("realized_pnl_delta", 0),
                        canonical_json(payload),
                        str(row["probe_id"]),
                    ),
                )
                cur.execute(
                    """
                    UPDATE quant.paper_calibration_pnl_positions
                    SET real_quantity=%s, real_cost_basis=%s, real_realized_pnl=%s,
                        paper_quantity=%s, paper_cost_basis=%s, paper_realized_pnl=%s,
                        updated_at=clock_timestamp()
                    WHERE account_id=%s AND asset_id=%s
                    """,
                    (
                        real_after.quantity,
                        real_after.cost_basis,
                        real_after.realized_pnl,
                        paper_after.quantity,
                        paper_after.cost_basis,
                        paper_after.realized_pnl,
                        str(existing["account_id"]),
                        str(existing["asset_id"]),
                    ),
                )
                conn.commit()
                return payload
            cur.execute(
                """
                INSERT INTO quant.paper_calibration_pnl_positions (
                    account_id, asset_id, market_id, condition_id
                ) VALUES (%s,%s,%s,%s)
                ON CONFLICT (account_id, asset_id) DO NOTHING
                """,
                (
                    account_id,
                    asset_id,
                    str(row.get("market_id") or ""),
                    str(row.get("condition_id") or ""),
                ),
            )
            cur.execute(
                """
                SELECT * FROM quant.paper_calibration_pnl_positions
                WHERE account_id=%s AND asset_id=%s FOR UPDATE
                """,
                (account_id, asset_id),
            )
            state = cur.fetchone()
            assert state is not None
            real_before = PositionPnlState(
                quantity=Decimal(str(state["real_quantity"])),
                cost_basis=Decimal(str(state["real_cost_basis"])),
                realized_pnl=Decimal(str(state["real_realized_pnl"])),
            )
            paper_before = PositionPnlState(
                quantity=Decimal(str(state["paper_quantity"])),
                cost_basis=Decimal(str(state["paper_cost_basis"])),
                realized_pnl=Decimal(str(state["paper_realized_pnl"])),
            )
            conditional = (
                account_before.get("conditional")
                if isinstance(account_before.get("conditional"), Mapping)
                else {}
            )
            try:
                observed_real_quantity = Decimal(str(conditional.get("balance") or 0))
            except Exception:
                observed_real_quantity = Decimal("0")
            if observed_real_quantity != real_before.quantity:
                return {
                    "schema_version": "calibration_pnl_reconciliation_v1",
                    "status": "POSITION_BASELINE_MISMATCH",
                    "pnl_reconciled": False,
                    "scope": "PORTFOLIO_LEDGER",
                    "account_id": account_id,
                    "asset_id": asset_id,
                    "tracked_position_before": format(real_before.quantity, "f"),
                    "observed_position_before": format(observed_real_quantity, "f"),
                    "reason": (
                        "account contains position changes outside the calibration "
                        "ledger; cost basis must be imported before PnL can continue"
                    ),
                }
            payload, real_after, paper_after = build_paired_pnl(
                row,
                truth,
                real_state=real_before,
                paper_state=paper_before,
            )
            cur.execute(
                """
                INSERT INTO quant.paper_calibration_pnl_entries (
                    probe_id, account_id, asset_id, event_ts, side,
                    real_realized_pnl_delta, paper_realized_pnl_delta, pnl
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (probe_id) DO NOTHING
                RETURNING probe_id
                """,
                (
                    str(row["probe_id"]),
                    account_id,
                    asset_id,
                    row.get("decision_ts") or datetime.now().astimezone(),
                    str(row.get("side") or ""),
                    payload.get("real", {}).get("realized_pnl_delta", 0),
                    payload.get("paper", {}).get("realized_pnl_delta", 0),
                    canonical_json(payload),
                ),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT pnl FROM quant.paper_calibration_pnl_entries WHERE probe_id=%s",
                    (str(row["probe_id"]),),
                )
                concurrent = cur.fetchone()
                conn.commit()
                return dict(concurrent["pnl"])
            cur.execute(
                """
                UPDATE quant.paper_calibration_pnl_positions
                SET market_id=%s, condition_id=%s,
                    real_quantity=%s, real_cost_basis=%s, real_realized_pnl=%s,
                    paper_quantity=%s, paper_cost_basis=%s, paper_realized_pnl=%s,
                    updated_at=clock_timestamp()
                WHERE account_id=%s AND asset_id=%s
                """,
                (
                    str(row.get("market_id") or ""),
                    str(row.get("condition_id") or ""),
                    real_after.quantity,
                    real_after.cost_basis,
                    real_after.realized_pnl,
                    paper_after.quantity,
                    paper_after.cost_basis,
                    paper_after.realized_pnl,
                    account_id,
                    asset_id,
                ),
            )
            conn.commit()
            return payload

    def realized_loss(
        self, *, since: datetime, account_id: str | None = None
    ) -> Decimal:
        predicate = "AND account_id=%s" if account_id else ""
        params: tuple[Any, ...] = (
            (since, str(account_id).lower(), since, str(account_id).lower())
            if account_id
            else (since, since)
        )
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(sum(loss), 0) AS loss
                FROM (
                    SELECT GREATEST(0, -real_realized_pnl_delta) AS loss
                    FROM quant.paper_calibration_pnl_entries
                    WHERE event_ts >= %s {predicate}
                    UNION ALL
                    SELECT GREATEST(0, -real_realized_pnl_delta) AS loss
                    FROM quant.paper_calibration_pnl_settlements
                    WHERE COALESCE(resolved_at, created_at) >= %s {predicate}
                ) losses
                """,
                params,
            )
            row = cur.fetchone()
            return Decimal(str((row or {}).get("loss") or 0))

    def load_pnl_position(
        self,
        *,
        account_id: str,
        asset_id: str,
    ) -> dict[str, Any] | None:
        """Load the tracked live-vs-paper position without mark-data joins."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM quant.paper_calibration_pnl_positions
                WHERE account_id=%s AND asset_id=%s
                """,
                (str(account_id).lower(), str(asset_id)),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def pnl_summary(self, *, account_id: str | None = None) -> dict[str, Any]:
        return summarize_pnl_positions(self.pnl_positions(account_id=account_id))

    def pnl_positions(self, *, account_id: str | None = None) -> list[dict[str, Any]]:
        """Return position-level liquidation marks from the live book first.

        A quiet market does not become stale merely because it has no recent
        price change. The live BookState remains usable while it is READY,
        gap-free, and backed by the redundant transport. Archive coverage is a
        fallback for reporting only and is explicitly identified as such.
        """

        predicate = "WHERE p.account_id=%s" if account_id else ""
        params = (str(account_id).lower(),) if account_id else ()
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.*,
                       COALESCE(t.market_title, m.title) AS market_title,
                       t.outcome_name,
                       t.market_state,
                       CASE
                         WHEN b.best_bid IS NOT NULL
                          AND b.book_status='READY'
                          AND b.has_gap=FALSE
                          AND b.coverage_grade IN ('A', 'A_PLUS')
                          AND b.transport_state='REDUNDANT'
                         THEN b.best_bid
                         WHEN c.current_best_bid IS NOT NULL
                          AND c.has_gap=FALSE
                          AND c.coverage_grade IN ('A', 'A_PLUS')
                          AND c.last_receive_ts >= now() - interval '5 minutes'
                         THEN c.current_best_bid
                       END AS mark_bid,
                       CASE
                         WHEN b.best_bid IS NOT NULL
                          AND b.book_status='READY'
                          AND b.has_gap=FALSE
                          AND b.coverage_grade IN ('A', 'A_PLUS')
                          AND b.transport_state='REDUNDANT'
                         THEN 'LIVE_BOOK'
                         WHEN c.current_best_bid IS NOT NULL
                          AND c.has_gap=FALSE
                          AND c.coverage_grade IN ('A', 'A_PLUS')
                          AND c.last_receive_ts >= now() - interval '5 minutes'
                         THEN 'ARCHIVE_COVERAGE'
                         ELSE 'UNAVAILABLE'
                       END AS mark_source,
                       CASE
                         WHEN b.best_bid IS NOT NULL
                          AND b.book_status='READY'
                          AND b.has_gap=FALSE
                          AND b.coverage_grade IN ('A', 'A_PLUS')
                          AND b.transport_state='REDUNDANT'
                         THEN b.observed_at
                         ELSE c.last_receive_ts
                       END AS mark_observed_at,
                       b.best_ask AS live_best_ask,
                       b.coverage_grade AS live_coverage_grade,
                       b.has_gap AS live_has_gap,
                       b.transport_state AS live_transport_state
                FROM quant.paper_calibration_pnl_positions p
                LEFT JOIN quant.paper_market_registry_tokens t
                  ON t.asset_id=p.asset_id
                LEFT JOIN core.markets m
                  ON m.id::text=p.market_id
                LEFT JOIN quant.paper_live_current_books b
                  ON b.asset_id=p.asset_id
                LEFT JOIN quant.clob_l2_current_coverage c ON c.asset_id=p.asset_id
                {predicate}
                ORDER BY p.updated_at, p.account_id, p.asset_id
                """,
                params,
            )
            rows = [dict(row) for row in cur.fetchall()]
        now = datetime.now().astimezone()
        output: list[dict[str, Any]] = []
        for row in rows:
            for key in (
                "real_quantity",
                "real_cost_basis",
                "real_realized_pnl",
                "paper_quantity",
                "paper_cost_basis",
                "paper_realized_pnl",
            ):
                row[key] = Decimal(str(row.get(key) or 0))
            mark = (
                Decimal(str(row["mark_bid"]))
                if row.get("mark_bid") is not None
                else None
            )
            observed_at = row.get("mark_observed_at")
            age = (
                max(0.0, (now - observed_at.astimezone()).total_seconds())
                if observed_at is not None
                else None
            )
            row.update(
                mark_bid=mark,
                mark_status="READY" if mark is not None else "UNAVAILABLE",
                mark_age_seconds=age,
                real_liquidation_value=(
                    row["real_quantity"] * mark if mark is not None else Decimal("0")
                ),
                paper_liquidation_value=(
                    row["paper_quantity"] * mark if mark is not None else Decimal("0")
                ),
                real_unrealized_pnl=(
                    row["real_quantity"] * mark - row["real_cost_basis"]
                    if mark is not None
                    else None
                ),
                paper_unrealized_pnl=(
                    row["paper_quantity"] * mark - row["paper_cost_basis"]
                    if mark is not None
                    else None
                ),
            )
            output.append(row)
        return output

    def settlement_watch_positions(
        self,
        *,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        predicate = "AND p.account_id=%s" if account_id else ""
        params = (str(account_id).lower(),) if account_id else ()
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.account_id, p.asset_id, p.market_id, p.condition_id,
                       p.real_quantity, p.real_cost_basis,
                       COALESCE(t.market_title, m.title) AS market_title,
                       t.outcome_name, t.market_state,
                       mkt.resolved, mkt.resolution_status,
                       mkt.winning_asset_id, mkt.resolved_time,
                       mkt.resolution_source
                FROM quant.paper_calibration_pnl_positions p
                LEFT JOIN quant.paper_market_registry_tokens t
                  ON t.asset_id=p.asset_id
                LEFT JOIN core.markets m ON m.id::text=p.market_id
                LEFT JOIN quant.paper_market_registry_markets mkt
                  ON mkt.condition_id=p.condition_id
                WHERE p.real_quantity > 0
                {predicate}
                ORDER BY p.updated_at, p.asset_id
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    def pending_settlements(
        self,
        *,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        predicate = "WHERE account_id=%s" if account_id else ""
        params = (str(account_id).lower(),) if account_id else ()
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM quant.paper_calibration_pnl_settlements
                {predicate}
                {"AND" if predicate else "WHERE"} cash_reconciliation_status='PENDING_REDEMPTION'
                ORDER BY resolved_at, settlement_key
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    def load_settlement(self, settlement_key: str) -> dict[str, Any] | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_calibration_pnl_settlements
                WHERE settlement_key=%s
                """,
                (str(settlement_key),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def latest_verified_redeem_settlement(self) -> dict[str, Any] | None:
        """Return one cash-reconciled redemption with transaction evidence."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM quant.paper_calibration_pnl_settlements
                WHERE cash_reconciliation_status='PASS'
                  AND observed_real_payout=expected_real_payout
                  AND payload->'redemption_evidence'->>'transaction_hash' LIKE '0x%%'
                  AND (payload->'redemption_evidence'->>'receipt_status')::integer=1
                ORDER BY COALESCE(resolved_at, created_at) DESC, settlement_key DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def load_pnl_candidate_probes(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM quant.paper_calibration_probes
                WHERE exchange_submit_called=TRUE
                  AND reconciliation->'order'->>'actual_matched_size' IS NOT NULL
                ORDER BY decision_ts, created_at, probe_id
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
            return [dict(row) for row in cur.fetchall()]

    def settle_resolved_pnl_positions(self, *, limit: int = 100) -> int:
        """Realize resolved positions while leaving real cash receipt pending."""

        from .pnl import PositionPnlState, apply_resolution

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.*, m.winning_asset_id, m.resolved_time, m.resolution_source
                FROM quant.paper_calibration_pnl_positions p
                JOIN quant.paper_market_registry_markets m
                  ON m.condition_id=p.condition_id
                WHERE (p.real_quantity > 0 OR p.paper_quantity > 0)
                  AND m.resolved=TRUE
                  AND m.resolution_status='RESOLVED'
                  AND m.winning_asset_id IS NOT NULL
                ORDER BY m.resolved_time, p.account_id, p.asset_id
                LIMIT %s
                FOR UPDATE OF p
                """,
                (max(1, int(limit)),),
            )
            rows = [dict(row) for row in cur.fetchall()]
            changed = 0
            for row in rows:
                account_id = str(row["account_id"])
                asset_id = str(row["asset_id"])
                winning_asset_id = str(row["winning_asset_id"])
                resolved_at = row.get("resolved_time")
                settlement_key = (
                    "calsettle-"
                    + hashlib.sha256(
                        "|".join(
                            (
                                account_id,
                                asset_id,
                                winning_asset_id,
                                str(resolved_at or "unknown"),
                            )
                        ).encode("utf-8")
                    ).hexdigest()
                )
                real_before = PositionPnlState(
                    quantity=Decimal(str(row["real_quantity"])),
                    cost_basis=Decimal(str(row["real_cost_basis"])),
                    realized_pnl=Decimal(str(row["real_realized_pnl"])),
                )
                paper_before = PositionPnlState(
                    quantity=Decimal(str(row["paper_quantity"])),
                    cost_basis=Decimal(str(row["paper_cost_basis"])),
                    realized_pnl=Decimal(str(row["paper_realized_pnl"])),
                )
                winning = asset_id == winning_asset_id
                real_after, expected_payout, real_delta = apply_resolution(
                    real_before,
                    winning=winning,
                )
                paper_after, paper_payout, paper_delta = apply_resolution(
                    paper_before,
                    winning=winning,
                )
                payload = {
                    "schema_version": "calibration_pnl_settlement_v1",
                    "settlement_key": settlement_key,
                    "account_id": account_id,
                    "asset_id": asset_id,
                    "winning_asset_id": winning_asset_id,
                    "winning": winning,
                    "resolution_source": row.get("resolution_source"),
                    "resolved_at": resolved_at,
                    "expected_real_payout": format(expected_payout, "f"),
                    "observed_real_payout": None,
                    "paper_payout": format(paper_payout, "f"),
                    "real_realized_pnl_delta": format(real_delta, "f"),
                    "paper_realized_pnl_delta": format(paper_delta, "f"),
                    "cash_reconciliation_status": "PENDING_REDEMPTION",
                }
                cur.execute(
                    """
                    INSERT INTO quant.paper_calibration_pnl_settlements (
                        settlement_key, account_id, asset_id, winning_asset_id,
                        resolved_at, expected_real_payout, observed_real_payout,
                        real_realized_pnl_delta, paper_realized_pnl_delta,
                        cash_reconciliation_status, payload
                    ) VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (settlement_key) DO NOTHING
                    RETURNING settlement_key
                    """,
                    (
                        settlement_key,
                        account_id,
                        asset_id,
                        winning_asset_id,
                        resolved_at,
                        expected_payout,
                        real_delta,
                        paper_delta,
                        "PENDING_REDEMPTION",
                        canonical_json(payload),
                    ),
                )
                if cur.fetchone() is None:
                    continue
                cur.execute(
                    """
                    UPDATE quant.paper_calibration_pnl_positions
                    SET real_quantity=0, real_cost_basis=0, real_realized_pnl=%s,
                        paper_quantity=0, paper_cost_basis=0, paper_realized_pnl=%s,
                        updated_at=clock_timestamp()
                    WHERE account_id=%s AND asset_id=%s
                    """,
                    (
                        real_after.realized_pnl,
                        paper_after.realized_pnl,
                        account_id,
                        asset_id,
                    ),
                )
                changed += 1
            conn.commit()
            return changed

    def record_observed_settlement_payout(
        self,
        *,
        settlement_key: str,
        observed_payout: Any,
        tolerance: Any = "0.00001",
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        observed = Decimal(str(observed_payout))
        allowed = abs(Decimal(str(tolerance)))
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_calibration_pnl_settlements
                WHERE settlement_key=%s FOR UPDATE
                """,
                (str(settlement_key),),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"unknown calibration settlement: {settlement_key}")
            expected = Decimal(str(row["expected_real_payout"]))
            status = "PASS" if abs(observed - expected) <= allowed else "MISMATCH"
            payload = dict(row["payload"])
            payload.update(
                observed_real_payout=format(observed, "f"),
                payout_error=format(abs(observed - expected), "f"),
                cash_reconciliation_status=status,
                tolerance=format(allowed, "f"),
            )
            if evidence:
                payload["redemption_evidence"] = redact_mapping(dict(evidence))
            cur.execute(
                """
                UPDATE quant.paper_calibration_pnl_settlements
                SET observed_real_payout=%s, cash_reconciliation_status=%s,
                    payload=%s::jsonb
                WHERE settlement_key=%s
                RETURNING *
                """,
                (observed, status, canonical_json(payload), str(settlement_key)),
            )
            updated = dict(cur.fetchone())
            conn.commit()
            return updated


def summarize_pnl_positions(positions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize already-marked position rows without another database read."""

    rows = [dict(row) for row in positions]
    open_positions = [
        row for row in rows if Decimal(str(row.get("real_quantity") or 0)) > 0
    ]
    marked_positions = [
        row for row in open_positions if row.get("mark_status") == "READY"
    ]
    marks_complete = len(open_positions) == len(marked_positions)

    def total(key: str, source: Iterable[Mapping[str, Any]]) -> Decimal:
        return sum((Decimal(str(row.get(key) or 0)) for row in source), Decimal("0"))

    real_mark = total("real_liquidation_value", marked_positions)
    paper_mark = total(
        "paper_liquidation_value",
        (
            row
            for row in rows
            if Decimal(str(row.get("paper_quantity") or 0)) > 0
            and row.get("mark_status") == "READY"
        ),
    )
    real_cost = total("real_cost_basis", rows)
    paper_cost = total("paper_cost_basis", rows)
    real_realized = total("real_realized_pnl", rows)
    paper_realized = total("paper_realized_pnl", rows)
    mark_times = [
        row.get("mark_observed_at")
        for row in open_positions
        if row.get("mark_observed_at") is not None
    ]
    mark_ages = [
        row.get("mark_age_seconds")
        for row in open_positions
        if row.get("mark_age_seconds") is not None
    ]
    return {
        "position_count": len(rows),
        "open_positions": len(open_positions),
        "marked_open_positions": len(marked_positions),
        "real_quantity": total("real_quantity", rows),
        "real_cost_basis": real_cost,
        "real_realized_pnl": real_realized,
        "paper_quantity": total("paper_quantity", rows),
        "paper_cost_basis": paper_cost,
        "paper_realized_pnl": paper_realized,
        "marked_real_liquidation_value": real_mark,
        "marked_paper_liquidation_value": paper_mark,
        "oldest_mark_at": min(mark_times) if mark_times else None,
        "latest_mark_at": max(mark_times) if mark_times else None,
        "mark_max_age_seconds": max(mark_ages) if mark_ages else None,
        "mark_status": "READY" if marks_complete else "PARTIAL",
        "real_unrealized_pnl": (real_mark - real_cost) if marks_complete else None,
        "paper_unrealized_pnl": (paper_mark - paper_cost)
        if marks_complete
        else None,
        "real_total_pnl": (
            real_realized + real_mark - real_cost if marks_complete else None
        ),
        "paper_total_pnl": (
            paper_realized + paper_mark - paper_cost if marks_complete else None
        ),
    }
