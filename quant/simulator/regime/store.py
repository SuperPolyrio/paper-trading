"""Additive PostgreSQL persistence for frozen venue regimes."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from quant.core.db import postgres_connection

from .model import VenueRegimeBinding, VenueRegimeSnapshot, _json_value


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.venue_regime_snapshots (
        regime_id TEXT PRIMARY KEY, venue TEXT NOT NULL,
        valid_from TIMESTAMPTZ NOT NULL, valid_to TIMESTAMPTZ,
        api_version TEXT NOT NULL, sdk_name TEXT NOT NULL, sdk_version TEXT NOT NULL,
        signature_type TEXT NOT NULL, collateral_token TEXT NOT NULL, exchange_contract TEXT NOT NULL,
        market_type TEXT NOT NULL, tick_size_rule JSONB NOT NULL, min_order_rule JSONB NOT NULL,
        fee_schedule_json JSONB NOT NULL, rebate_schedule_json JSONB NOT NULL,
        delay_class TEXT NOT NULL, rate_limit_json JSONB NOT NULL, batch_max_size INTEGER NOT NULL,
        heartbeat_rule JSONB NOT NULL, matching_mode TEXT NOT NULL,
        source_url TEXT NOT NULL, source_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "CREATE INDEX IF NOT EXISTS venue_regime_snapshots_lookup_idx ON quant.venue_regime_snapshots (venue, valid_from, valid_to)",
)


class PostgresVenueRegimeStore:
    def __init__(self, connection_factory: Any = postgres_connection) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)

    def sync_calibration_regimes(self) -> int:
        """Import the existing canonical venue-contract rows idempotently."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('quant.venue_regimes') AS table_name")
            if cur.fetchone()["table_name"] is None:
                return 0
            cur.execute(
                """
                SELECT venue_regime_id,venue,clob_version,sdk_name,sdk_version,
                       effective_from,effective_to,order_response_mode,
                       fee_schedule_hash,rounding_rules_hash,
                       matching_engine_release,source_changelog_url
                FROM quant.venue_regimes
                ORDER BY venue,effective_from,venue_regime_id
                """
            )
            snapshots = tuple(_snapshot_from_calibration_row(dict(row)) for row in cur)
        imported = 0
        for snapshot in snapshots:
            self.freeze(snapshot)
            imported += 1
        return imported

    def freeze(self, snapshot: VenueRegimeSnapshot) -> VenueRegimeSnapshot:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT regime_id FROM quant.venue_regime_snapshots
                WHERE venue=%s AND regime_id<>%s
                  AND (valid_to IS NULL OR valid_to>%s)
                  AND (%s::timestamptz IS NULL OR valid_from<%s::timestamptz)
                """,
                (
                    snapshot.venue,
                    snapshot.regime_id,
                    snapshot.valid_from,
                    snapshot.valid_to,
                    snapshot.valid_to,
                ),
            )
            if cur.fetchone() is not None:
                raise ValueError("venue regime validity ranges must not overlap")
            cur.execute(
                """
                INSERT INTO quant.venue_regime_snapshots (
                    regime_id, venue, valid_from, valid_to, api_version, sdk_name, sdk_version,
                    signature_type, collateral_token, exchange_contract, market_type, tick_size_rule,
                    min_order_rule, fee_schedule_json, rebate_schedule_json, delay_class, rate_limit_json,
                    batch_max_size, heartbeat_rule, matching_mode, source_url, source_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (regime_id) DO NOTHING
                RETURNING source_hash
                """,
                _values(snapshot),
            )
            row = cur.fetchone()
            if row is not None:
                return snapshot
            cur.execute(
                "SELECT source_hash FROM quant.venue_regime_snapshots WHERE regime_id=%s",
                (snapshot.regime_id,),
            )
            existing = cur.fetchone()
            if existing is None or str(existing["source_hash"]) != snapshot.source_hash:
                raise ValueError("regime id conflict with different content")
            return snapshot

    def effective_at(self, *, venue: str, at: datetime) -> dict[str, Any]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.venue_regime_snapshots
                WHERE venue=%s AND valid_from<=%s AND (valid_to IS NULL OR valid_to>%s)
                ORDER BY valid_from DESC
                """,
                (venue, at, at),
            )
            rows = cur.fetchall()
            if len(rows) != 1:
                raise LookupError(
                    f"expected exactly one {venue} regime at {at.isoformat()}, "
                    f"found {len(rows)}"
                )
            return dict(rows[0])

    def binding_at(self, *, venue: str, at: datetime) -> VenueRegimeBinding:
        row = self.effective_at(venue=venue, at=at)
        return VenueRegimeBinding(
            regime_id=str(row["regime_id"]),
            source_hash=str(row["source_hash"]),
            venue=str(row["venue"]),
            valid_from=row["valid_from"],
            valid_to=row.get("valid_to"),
        )


def _values(snapshot: VenueRegimeSnapshot) -> tuple[Any, ...]:
    def encoded(value: Any) -> str:
        return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))

    return (
        snapshot.regime_id,
        snapshot.venue,
        snapshot.valid_from,
        snapshot.valid_to,
        snapshot.api_version,
        snapshot.sdk_name,
        snapshot.sdk_version,
        snapshot.signature_type,
        snapshot.collateral_token,
        snapshot.exchange_contract,
        snapshot.market_type,
        encoded(snapshot.tick_size_rule),
        encoded(snapshot.min_order_rule),
        encoded(snapshot.fee_schedule),
        encoded(snapshot.rebate_schedule),
        snapshot.delay_class,
        encoded(snapshot.rate_limits),
        snapshot.batch_max_size,
        encoded(snapshot.heartbeat_rule),
        snapshot.matching_mode,
        snapshot.source_url,
        snapshot.source_hash,
    )


def _snapshot_from_calibration_row(row: dict[str, Any]) -> VenueRegimeSnapshot:
    rounding_hash = str(row["rounding_rules_hash"])
    return VenueRegimeSnapshot(
        regime_id=str(row["venue_regime_id"]),
        venue=str(row["venue"]),
        valid_from=row["effective_from"],
        valid_to=row.get("effective_to"),
        api_version=str(row["clob_version"]),
        sdk_name=str(row["sdk_name"]),
        sdk_version=str(row["sdk_version"]),
        signature_type="POLYMARKET_CONFIGURED_SIGNATURE_TYPE",
        collateral_token="POLYGON_USDC_COLLATERAL",
        exchange_contract="POLYMARKET_CTF_EXCHANGE_FAMILY",
        market_type="BINARY_CLOB_AND_NEG_RISK",
        tick_size_rule={
            "source_hash": rounding_hash,
            "source": "point_in_time_market_terms",
        },
        min_order_rule={
            "source_hash": rounding_hash,
            "source": "point_in_time_market_terms",
        },
        fee_schedule={
            "source_hash": str(row["fee_schedule_hash"]),
            "source": "point_in_time_market_terms",
        },
        rebate_schedule={"source": "polymarket_rewards_schedule"},
        delay_class="ASYNC_ORDER_TRADE_COMMIT",
        rate_limits={"source": "paper_venue_gateway_config"},
        batch_max_size=15,
        heartbeat_rule={"source": "paper_venue_gateway_config"},
        matching_mode=str(row["order_response_mode"]),
        source_url=str(row["source_changelog_url"]),
    )
