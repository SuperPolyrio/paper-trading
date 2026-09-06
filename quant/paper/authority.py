"""Database-backed execution authority leases and epoch fencing."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

AUTHORITY_TABLES = (
    "paper_execution_partition_leases",
    "paper_authority_config",
)

# These relations contain execution lifecycle or economic authority. Read models,
# market data, watchlists, and health samples intentionally remain unfenced.
FENCED_TABLES = (
    "paper_live_order_intents",
    "paper_order_events",
    "paper_market_clarifications",
    "paper_market_clarification_commands",
    "paper_risk_decisions",
    "paper_execution_profile_decisions",
    "maker_queue_states",
    "paper_global_event_kernel_state",
    "paper_global_event_kernel_events",
    "paper_order_reservations",
    "paper_reservation_release_events",
    "paper_taker_order_audits",
    "paper_fills",
    "paper_fill_ctf_settlement_audits",
    "paper_ledger_entries",
    "paper_journal_lines",
    "paper_portfolio_applied_results",
    "paper_accounts",
    "paper_positions",
    "paper_settlements",
    "simulator_complete_set_lots",
    "simulator_complete_set_lot_legs",
    "simulator_complete_set_consumptions",
    "simulator_complete_set_consumption_legs",
    "paper_execution_finality",
    "paper_execution_finality_events",
    "simulator_position_operations",
    "simulator_position_operation_events",
    "simulator_position_operation_nonces",
    "simulator_position_operation_reservations",
    "simulator_position_operation_token_reservations",
    "paper_position_operation_applications",
)


class AuthorityLeaseUnavailable(RuntimeError):
    """Raised when another live owner already holds a partition."""


class AuthorityLeaseLost(RuntimeError):
    """Raised when a worker can no longer renew its exact lease epoch."""


@dataclass(frozen=True)
class AuthorityLeaseToken:
    partition_key: str
    owner_instance_id: str
    lease_epoch: int
    lease_until: datetime
    heartbeat_at: datetime
    acquired_at: datetime
    released_at: datetime | None
    fencing_enforced: bool


class AuthorityLeaseHandle:
    """Thread-safe token holder shared by DB worker threads."""

    def __init__(self, token: AuthorityLeaseToken | None = None) -> None:
        self._token = token
        self._last_token = token
        self._lock = threading.Lock()

    @property
    def token(self) -> AuthorityLeaseToken | None:
        with self._lock:
            return self._token

    @property
    def last_token(self) -> AuthorityLeaseToken | None:
        with self._lock:
            return self._last_token

    @property
    def held(self) -> bool:
        return self.token is not None

    def replace(self, token: AuthorityLeaseToken) -> None:
        with self._lock:
            self._token = token
            self._last_token = token

    def clear(self) -> None:
        with self._lock:
            self._token = None


class AuthorityLeaseStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quant.paper_execution_partition_leases (
                    partition_key TEXT PRIMARY KEY,
                    owner_instance_id TEXT NOT NULL,
                    lease_epoch BIGINT NOT NULL CHECK (lease_epoch > 0),
                    lease_until TIMESTAMPTZ NOT NULL,
                    heartbeat_at TIMESTAMPTZ NOT NULL,
                    acquired_at TIMESTAMPTZ NOT NULL,
                    released_at TIMESTAMPTZ,
                    CHECK (lease_until >= acquired_at)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quant.paper_authority_config (
                    config_id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (config_id),
                    fencing_enforced BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
                )
                """
            )
            cur.execute(
                """
                INSERT INTO quant.paper_authority_config (config_id, fencing_enforced)
                VALUES (TRUE, FALSE)
                ON CONFLICT (config_id) DO NOTHING
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION quant.paper_enforce_authority_fence()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $fence$
                DECLARE
                    enforcement_enabled BOOLEAN;
                    selected_partition TEXT;
                    selected_owner TEXT;
                    selected_epoch BIGINT;
                    valid_lease BOOLEAN;
                BEGIN
                    SELECT fencing_enforced
                    INTO enforcement_enabled
                    FROM quant.paper_authority_config
                    WHERE config_id=TRUE;

                    IF NOT COALESCE(enforcement_enabled, FALSE) THEN
                        RETURN NEW;
                    END IF;

                    IF current_setting('poly_quant.control_plane_write', TRUE) = 'on' THEN
                        RETURN NEW;
                    END IF;

                    selected_partition := NULLIF(
                        current_setting('poly_quant.authority_partition_key', TRUE),
                        ''
                    );
                    selected_owner := NULLIF(
                        current_setting('poly_quant.authority_owner_instance_id', TRUE),
                        ''
                    );
                    BEGIN
                        selected_epoch := NULLIF(
                            current_setting('poly_quant.authority_lease_epoch', TRUE),
                            ''
                        )::BIGINT;
                    EXCEPTION WHEN invalid_text_representation THEN
                        selected_epoch := NULL;
                    END;

                    IF selected_partition IS NULL
                       OR selected_owner IS NULL
                       OR selected_epoch IS NULL THEN
                        RAISE EXCEPTION
                            'paper authority fence rejected %.%: missing lease token',
                            TG_TABLE_SCHEMA, TG_TABLE_NAME
                            USING ERRCODE='55000';
                    END IF;

                    SELECT TRUE
                    INTO valid_lease
                    FROM quant.paper_execution_partition_leases
                    WHERE partition_key=selected_partition
                      AND owner_instance_id=selected_owner
                      AND lease_epoch=selected_epoch
                      AND released_at IS NULL
                      AND lease_until > clock_timestamp();

                    IF NOT COALESCE(valid_lease, FALSE) THEN
                        RAISE EXCEPTION
                            'paper authority fence rejected %.%: stale or expired lease epoch %',
                            TG_TABLE_SCHEMA, TG_TABLE_NAME, selected_epoch
                            USING ERRCODE='55000';
                    END IF;

                    NEW.authority_partition_key := selected_partition;
                    NEW.authority_lease_epoch := selected_epoch;
                    RETURN NEW;
                END
                $fence$
                """
            )
            for table in FENCED_TABLES:
                cur.execute(
                    "SELECT to_regclass(%s)::text AS relation", (f"quant.{table}",)
                )
                if cur.fetchone()["relation"] is None:
                    continue
                cur.execute(
                    f"ALTER TABLE quant.{table} "
                    "ADD COLUMN IF NOT EXISTS authority_partition_key TEXT"
                )
                cur.execute(
                    f"ALTER TABLE quant.{table} "
                    "ADD COLUMN IF NOT EXISTS authority_lease_epoch BIGINT"
                )
                cur.execute(
                    f"DROP TRIGGER IF EXISTS paper_authority_fence ON quant.{table}"
                )
                cur.execute(
                    f"CREATE TRIGGER paper_authority_fence "
                    f"BEFORE INSERT OR UPDATE ON quant.{table} "
                    "FOR EACH ROW EXECUTE FUNCTION quant.paper_enforce_authority_fence()"
                )
            conn.commit()

    def set_fencing_enforced(self, enabled: bool) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_authority_config
                SET fencing_enforced=%s,updated_at=clock_timestamp()
                WHERE config_id=TRUE
                RETURNING fencing_enforced
                """,
                (bool(enabled),),
            )
            row = cur.fetchone()
            conn.commit()
        return bool(row and row["fencing_enforced"])

    def acquire(
        self,
        *,
        partition_key: str,
        owner_instance_id: str,
        lease_seconds: float,
    ) -> AuthorityLeaseToken | None:
        partition = _required(partition_key, "partition_key")
        owner = _required(owner_instance_id, "owner_instance_id")
        duration = _positive_seconds(lease_seconds)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT partition_key,owner_instance_id,lease_epoch,lease_until,
                       heartbeat_at,acquired_at,released_at
                FROM quant.paper_execution_partition_leases
                WHERE partition_key=%s
                FOR UPDATE
                """,
                (partition,),
            )
            current = cur.fetchone()
            if current is None:
                cur.execute(
                    """
                    INSERT INTO quant.paper_execution_partition_leases (
                        partition_key,owner_instance_id,lease_epoch,lease_until,
                        heartbeat_at,acquired_at,released_at
                    ) VALUES (
                        %s,%s,1,
                        clock_timestamp() + make_interval(secs => %s),
                        clock_timestamp(),clock_timestamp(),NULL
                    )
                    """,
                    (partition, owner, duration),
                )
            else:
                cur.execute(
                    """
                    SELECT clock_timestamp() AS db_now,
                           %s::timestamptz AS lease_until,
                           %s::timestamptz AS released_at
                    """,
                    (current["lease_until"], current["released_at"]),
                )
                timing = cur.fetchone()
                active = (
                    timing["released_at"] is None
                    and timing["lease_until"] > timing["db_now"]
                )
                if active and str(current["owner_instance_id"]) != owner:
                    conn.rollback()
                    return None
                if active:
                    cur.execute(
                        """
                        UPDATE quant.paper_execution_partition_leases
                        SET heartbeat_at=clock_timestamp(),
                            lease_until=clock_timestamp() + make_interval(secs => %s),
                            released_at=NULL
                        WHERE partition_key=%s AND owner_instance_id=%s
                        """,
                        (duration, partition, owner),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE quant.paper_execution_partition_leases
                        SET owner_instance_id=%s,
                            lease_epoch=lease_epoch + 1,
                            lease_until=clock_timestamp() + make_interval(secs => %s),
                            heartbeat_at=clock_timestamp(),
                            acquired_at=clock_timestamp(),
                            released_at=NULL
                        WHERE partition_key=%s
                        """,
                        (owner, duration, partition),
                    )
            token = self._load_token(cur, partition)
            conn.commit()
        return token

    def heartbeat(
        self,
        token: AuthorityLeaseToken,
        *,
        lease_seconds: float,
    ) -> AuthorityLeaseToken | None:
        duration = _positive_seconds(lease_seconds)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_execution_partition_leases
                SET heartbeat_at=clock_timestamp(),
                    lease_until=clock_timestamp() + make_interval(secs => %s)
                WHERE partition_key=%s
                  AND owner_instance_id=%s
                  AND lease_epoch=%s
                  AND released_at IS NULL
                  AND lease_until > clock_timestamp()
                RETURNING partition_key
                """,
                (
                    duration,
                    token.partition_key,
                    token.owner_instance_id,
                    token.lease_epoch,
                ),
            )
            if cur.fetchone() is None:
                conn.rollback()
                return None
            renewed = self._load_token(cur, token.partition_key)
            conn.commit()
        return renewed

    def release(self, token: AuthorityLeaseToken) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_execution_partition_leases
                SET released_at=clock_timestamp(),
                    lease_until=clock_timestamp(),
                    heartbeat_at=clock_timestamp()
                WHERE partition_key=%s
                  AND owner_instance_id=%s
                  AND lease_epoch=%s
                  AND released_at IS NULL
                """,
                (
                    token.partition_key,
                    token.owner_instance_id,
                    token.lease_epoch,
                ),
            )
            released = int(cur.rowcount or 0) == 1
            conn.commit()
        return released

    def status(self, partition_key: str) -> AuthorityLeaseToken | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            return self._load_token(cur, _required(partition_key, "partition_key"))

    @staticmethod
    def _load_token(cur: Any, partition_key: str) -> AuthorityLeaseToken | None:
        cur.execute(
            """
            SELECT lease.partition_key,lease.owner_instance_id,lease.lease_epoch,
                   lease.lease_until,lease.heartbeat_at,lease.acquired_at,
                   lease.released_at,
                   COALESCE(config.fencing_enforced,FALSE) AS fencing_enforced
            FROM quant.paper_execution_partition_leases lease
            LEFT JOIN quant.paper_authority_config config ON config.config_id=TRUE
            WHERE lease.partition_key=%s
            """,
            (partition_key,),
        )
        row = cur.fetchone()
        return AuthorityLeaseToken(**dict(row)) if row is not None else None


class AuthorityLeaseController:
    def __init__(
        self,
        store: AuthorityLeaseStore,
        *,
        partition_key: str,
        owner_instance_id: str,
        lease_seconds: float = 15.0,
        heartbeat_seconds: float = 5.0,
        handle: AuthorityLeaseHandle | None = None,
    ) -> None:
        self.store = store
        self.partition_key = _required(partition_key, "partition_key")
        self.owner_instance_id = _required(owner_instance_id, "owner_instance_id")
        self.lease_seconds = _positive_seconds(lease_seconds)
        self.heartbeat_seconds = _positive_seconds(heartbeat_seconds)
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be less than lease_seconds")
        self.handle = handle or AuthorityLeaseHandle()

    @property
    def held(self) -> bool:
        return self.handle.held

    @property
    def token(self) -> AuthorityLeaseToken | None:
        return self.handle.token

    def acquire(self) -> AuthorityLeaseToken:
        token = self.store.acquire(
            partition_key=self.partition_key,
            owner_instance_id=self.owner_instance_id,
            lease_seconds=self.lease_seconds,
        )
        if token is None:
            self.handle.clear()
            raise AuthorityLeaseUnavailable(
                f"partition {self.partition_key!r} already has a live owner"
            )
        self.handle.replace(token)
        return token

    def heartbeat(self) -> AuthorityLeaseToken:
        token = self.handle.token
        if token is None:
            raise AuthorityLeaseLost("authority lease is not held")
        renewed = self.store.heartbeat(token, lease_seconds=self.lease_seconds)
        if renewed is None:
            self.handle.clear()
            raise AuthorityLeaseLost(
                f"authority lease lost for {token.partition_key} epoch {token.lease_epoch}"
            )
        self.handle.replace(renewed)
        return renewed

    def release(self) -> bool:
        token = self.handle.token
        if token is None:
            return False
        try:
            return self.store.release(token)
        finally:
            self.handle.clear()

    def mark_lost(self) -> None:
        self.handle.clear()


class FencedPostgresConnectionFactory:
    """Attach the current authority token to every worker transaction."""

    def __init__(self, connection_factory: Any, handle: AuthorityLeaseHandle) -> None:
        self.connection_factory = connection_factory
        self.handle = handle
        self._local = threading.local()

    @contextmanager
    def __call__(self, *, readonly: bool = False) -> Iterator[Any]:
        with self.connection_factory(readonly=readonly) as conn:
            if readonly:
                yield conn
                return
            token = self.handle.token
            backend_pid = getattr(getattr(conn, "info", None), "backend_pid", None)
            desired_marker = (
                (
                    token.partition_key,
                    token.owner_instance_id,
                    int(token.lease_epoch),
                )
                if token is not None
                else None
            )
            cached_marker = getattr(self._local, "authority_marker", object())
            marker_key = (backend_pid, desired_marker)
            if cached_marker != marker_key:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            set_config('poly_quant.authority_partition_key', %s, FALSE),
                            set_config('poly_quant.authority_owner_instance_id', %s, FALSE),
                            set_config('poly_quant.authority_lease_epoch', %s, FALSE)
                        """,
                        (
                            desired_marker[0] if desired_marker is not None else "",
                            desired_marker[1] if desired_marker is not None else "",
                            str(desired_marker[2]) if desired_marker is not None else "",
                        ),
                    )
                # Persist session-scoped markers before the caller starts its
                # transaction. The DB trigger still validates lease freshness
                # and exact epoch on every fenced write.
                conn.commit()
                self._local.authority_marker = marker_key
            yield conn


class ControlPlanePostgresConnectionFactory:
    """Mark explicit ingress/admin transactions as non-execution writes."""

    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory

    @contextmanager
    def __call__(self, *, readonly: bool = False) -> Iterator[Any]:
        with self.connection_factory(readonly=readonly) as conn:
            if readonly:
                yield conn
                return

            # Schema initialization commits between individual migrations. Keep
            # the ingress marker at session scope so those commits cannot turn a
            # control-plane operation into an unfenced execution write midway.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('poly_quant.control_plane_write', 'on', FALSE)"
                )
            conn.commit()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('poly_quant.control_plane_write', 'off', FALSE)"
                    )
                conn.commit()


def _required(value: str, name: str) -> str:
    selected = str(value).strip()
    if not selected:
        raise ValueError(f"{name} is required")
    return selected


def _positive_seconds(value: float) -> float:
    selected = float(value)
    if selected <= 0:
        raise ValueError("lease durations must be positive")
    return selected
