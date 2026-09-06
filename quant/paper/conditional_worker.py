"""Trusted paper-only worker for causal conditional-order triggers."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from quant.core.db import postgres_connection

from .conditional_orders import PostgresConditionalOrderService, TriggerObservation
from .live_shadow_store import LiveShadowStore
from .public_api import ApiIdentity, PostgresPaperApiBackend
from .tenant_platform import (
    PaperPermission,
    PostgresTenantPlatformStore,
    TenantPrincipal,
)

LOGGER = logging.getLogger(__name__)


class ConditionalOrderWorker:
    """Poll the current BookState read model; never connect to a market feed."""

    def __init__(
        self,
        principal: TenantPrincipal,
        *,
        connection_factory: Any = postgres_connection,
        max_book_age_seconds: Decimal = Decimal(5),
    ) -> None:
        self.principal = principal
        self.max_book_age_seconds = Decimal(max_book_age_seconds)
        self.tenant_store = PostgresTenantPlatformStore(connection_factory)
        self.live_store = LiveShadowStore(connection_factory)
        self.service = PostgresConditionalOrderService(self.tenant_store)
        self.backend = PostgresPaperApiBackend(
            connection_factory=connection_factory,
            pepper=b"internal-conditional-worker-pepper",
            tenant_store=self.tenant_store,
            live_store=self.live_store,
            conditional_service=self.service,
            artifact_signing_key=b"internal-conditional-worker-signing",
        )
        self.identity = ApiIdentity(
            principal=principal,
            api_key_id=UUID(int=0),
            key_prefix="internal-conditional-worker",
            scopes=frozenset({"paper:read", "paper:trade"}),
        )

    def _submit_child(self, order: dict[str, Any], evidence: dict[str, Any]) -> int:
        conditional_id = str(order["conditional_order_id"])
        child = dict(order["child_order"])
        payload = {
            **child,
            "account_id": str(order["account_id"]),
            "strategy_id": str(order["strategy_id"]),
            "client_order_id": f"conditional-{conditional_id}",
            "conditional_trigger_evidence": evidence,
        }
        result = self.backend.submit_order(
            self.identity,
            payload=payload,
            idempotency_key=f"conditional-{conditional_id}",
        )
        return int(result["intent_id"])

    def _observations(self) -> list[TriggerObservation]:
        now = datetime.now(timezone.utc)
        with self.tenant_store._transaction(
            self.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT DISTINCT conditional.asset_id,conditional.reference_price,
                       book.observed_at,book.coverage_grade,book.has_gap,
                       book.best_bid,book.best_ask,book.transport_state
                FROM quant.paper_conditional_orders conditional
                LEFT JOIN quant.paper_live_current_books book
                  ON book.asset_id=conditional.asset_id
                WHERE conditional.tenant_id=%s
                  AND conditional.status IN ('ARMED','PENDING_DATA')
                  AND conditional.trigger_kind IN ('PRICE','TIME')
                ORDER BY conditional.asset_id,conditional.reference_price
                """,
                (self.principal.tenant_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        observations = []
        for row in rows:
            observed_at = row.get("observed_at") or datetime.fromtimestamp(
                0, tz=timezone.utc
            )
            age = Decimal(str(max(0.0, (now - observed_at).total_seconds())))
            best_bid = row.get("best_bid")
            best_ask = row.get("best_ask")
            reference = str(row["reference_price"]).upper()
            if reference == "BEST_BID":
                price = best_bid
            elif reference == "BEST_ASK":
                price = best_ask
            elif reference == "MID" and best_bid is not None and best_ask is not None:
                price = (Decimal(str(best_bid)) + Decimal(str(best_ask))) / Decimal(2)
            else:
                price = None
            transport = str(row.get("transport_state") or "DISCONNECTED").upper()
            observations.append(
                TriggerObservation(
                    asset_id=str(row["asset_id"]),
                    event_ts=observed_at,
                    source="PAPER_CURRENT_BOOK",
                    data_quality=str(row.get("coverage_grade") or "D"),
                    reference_price=reference,
                    price=None if price is None else Decimal(str(price)),
                    has_gap=bool(row.get("has_gap", True)),
                    stale=(
                        row.get("observed_at") is None
                        or age > self.max_book_age_seconds
                        or transport not in {"CONNECTED", "REDUNDANT"}
                    ),
                )
            )
        return observations

    def _parent_terminal_events(self) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(
            self.principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT DISTINCT conditional.parent_intent_id,
                       intent.status,intent.order_state,intent.result,
                       COALESCE(intent.completed_at,intent.updated_at) AS event_ts
                FROM quant.paper_conditional_orders conditional
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=conditional.parent_intent_id
                WHERE conditional.tenant_id=%s
                  AND conditional.parent_intent_id IS NOT NULL
                  AND conditional.status IN ('ARMED','PENDING_DATA')
                  AND (
                    conditional.trigger_kind='PARENT_TERMINAL'
                    OR conditional.trigger_source='WAITING_PARENT'
                  )
                ORDER BY conditional.parent_intent_id
                """,
                (self.principal.tenant_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        events = []
        for row in rows:
            result = dict(row.get("result") or {})
            filled_size = Decimal(str(result.get("filled_size") or 0))
            status = str(row.get("status") or "").upper()
            order_state = str(row.get("order_state") or "").upper()
            if filled_size > 0 or order_state == "CONFIRMED":
                parent_status = "CONFIRMED"
            elif status in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED", "FAILED"}:
                parent_status = status
            else:
                continue
            events.append(
                {
                    "parent_intent_id": int(row["parent_intent_id"]),
                    "parent_status": parent_status,
                    "event_ts": row.get("event_ts") or datetime.now(timezone.utc),
                }
            )
        return events

    def run_once(self) -> dict[str, int]:
        recovered = self.service.recover_triggering(
            self.principal, submit_child=self._submit_child
        )
        parent_events = self._parent_terminal_events()
        parent_triggered = []
        for event in parent_events:
            parent_triggered.extend(
                self.service.process_parent_terminal(
                    self.principal,
                    parent_intent_id=event["parent_intent_id"],
                    parent_status=event["parent_status"],
                    event_ts=event["event_ts"],
                    submit_child=self._submit_child,
                )
            )
        triggered = []
        observations = self._observations()
        for observation in observations:
            triggered.extend(
                self.service.process_observation(
                    self.principal,
                    observation,
                    submit_child=self._submit_child,
                )
            )
        return {
            "observations": len(observations),
            "recovered": len(recovered),
            "parent_events": len(parent_events),
            "triggered": len(parent_triggered) + len(triggered),
        }

    def run(self, *, interval_seconds: float, max_seconds: float | None) -> None:
        started = time.monotonic()
        while max_seconds is None or time.monotonic() - started < max_seconds:
            self.run_once()
            time.sleep(max(0.05, float(interval_seconds)))


class ConditionalOrderSupervisor:
    """Discover active tenants and run their conditional workers independently."""

    def __init__(
        self,
        *,
        connection_factory: Any = postgres_connection,
        max_book_age_seconds: Decimal = Decimal(5),
    ) -> None:
        self.connection_factory = connection_factory
        self.max_book_age_seconds = Decimal(max_book_age_seconds)
        self._workers: dict[UUID, ConditionalOrderWorker] = {}

    def _active_principals(self) -> list[TenantPrincipal]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (conditional.tenant_id)
                       conditional.tenant_id,conditional.created_by
                FROM quant.paper_conditional_orders conditional
                JOIN quant.paper_memberships membership
                  ON membership.tenant_id=conditional.tenant_id
                 AND membership.user_id=conditional.created_by
                 AND membership.status='ACTIVE'
                WHERE conditional.status IN (
                    'ARMED','PENDING_DATA','TRIGGERING'
                )
                ORDER BY conditional.tenant_id,conditional.created_at,
                         conditional.conditional_order_id
                """
            )
            rows = [dict(row) for row in cur.fetchall()]
        return [
            TenantPrincipal(
                tenant_id=UUID(str(row["tenant_id"])),
                actor_user_id=UUID(str(row["created_by"])),
            )
            for row in rows
        ]

    def run_once(self) -> dict[str, Any]:
        principals = self._active_principals()
        active_tenants = {principal.tenant_id for principal in principals}
        for tenant_id in set(self._workers) - active_tenants:
            self._workers.pop(tenant_id, None)
        totals: dict[str, Any] = {
            "tenants": len(principals),
            "observations": 0,
            "recovered": 0,
            "parent_events": 0,
            "triggered": 0,
            "errors": [],
        }
        for principal in principals:
            worker = self._workers.get(principal.tenant_id)
            if worker is None or worker.principal.actor_user_id != principal.actor_user_id:
                worker = ConditionalOrderWorker(
                    principal,
                    connection_factory=self.connection_factory,
                    max_book_age_seconds=self.max_book_age_seconds,
                )
                self._workers[principal.tenant_id] = worker
            try:
                result = worker.run_once()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "conditional worker tenant failed tenant_id=%s",
                    principal.tenant_id,
                )
                totals["errors"].append(
                    {
                        "tenant_id": str(principal.tenant_id),
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            for key in ("observations", "recovered", "parent_events", "triggered"):
                totals[key] += int(result[key])
        return totals

    def run(self, *, interval_seconds: float, max_seconds: float | None) -> None:
        started = time.monotonic()
        while max_seconds is None or time.monotonic() - started < max_seconds:
            self.run_once()
            time.sleep(max(0.05, float(interval_seconds)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=UUID)
    parser.add_argument("--actor-user-id", type=UUID)
    parser.add_argument("--all-tenants", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=0.5)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--max-book-age-seconds", type=Decimal, default=Decimal(5))
    args = parser.parse_args()
    if args.all_tenants:
        if args.tenant_id is not None or args.actor_user_id is not None:
            parser.error("--all-tenants cannot be combined with tenant identity")
        worker: ConditionalOrderWorker | ConditionalOrderSupervisor = (
            ConditionalOrderSupervisor(
                max_book_age_seconds=args.max_book_age_seconds,
            )
        )
    else:
        if args.tenant_id is None or args.actor_user_id is None:
            parser.error(
                "provide --all-tenants or both --tenant-id and --actor-user-id"
            )
        worker = ConditionalOrderWorker(
            TenantPrincipal(
                tenant_id=args.tenant_id,
                actor_user_id=args.actor_user_id,
            ),
            max_book_age_seconds=args.max_book_age_seconds,
        )
    if args.once:
        print(worker.run_once())
    else:
        worker.run(interval_seconds=args.interval_seconds, max_seconds=args.max_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
