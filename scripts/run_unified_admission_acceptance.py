#!/usr/bin/env python3
"""Persist and verify the unified eligibility admission matrix."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.core.db import postgres_connection
from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    AdmissionStatus,
    ExposureEffect,
    GeoblockSnapshot,
    JurisdictionMode,
    PostgresAdmissionStore,
    UnifiedAdmissionService,
)


def _snapshot(
    *, blocked: bool, country: str, observed_at: datetime
) -> GeoblockSnapshot:
    source = f"{blocked}:{country}:{observed_at.isoformat()}"
    return GeoblockSnapshot(
        blocked=blocked,
        country=country,
        region="",
        detected_ip="192.0.2.1",
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=60),
        raw_payload_hash=hashlib.sha256(source.encode()).hexdigest(),
        source="acceptance_fixture",
    )


def _request(
    run_id: str,
    name: str,
    *,
    operation: AdmissionOperation,
    effect: ExposureEffect,
    observed_at: datetime,
    before: Decimal | None = None,
    after: Decimal | None = None,
) -> AdmissionRequest:
    return AdmissionRequest(
        request_id=f"{run_id}:{name}",
        operation=operation,
        account_id=f"{run_id}:account",
        strategy_id=f"{run_id}:strategy",
        asset_id=f"{run_id}:asset",
        condition_id=f"{run_id}:condition",
        market_id=f"{run_id}:market",
        exposure_effect=effect,
        exposure_before=before,
        exposure_after=after,
        observed_at=observed_at,
        metadata={"acceptance_case": name},
    )


def run() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    run_id = f"admission-acceptance-{uuid4().hex[:16]}"
    store = PostgresAdmissionStore()
    store.ensure_schema()
    service = UnifiedAdmissionService(store=store)
    unrestricted = _snapshot(blocked=False, country="HK", observed_at=now)
    close_only = _snapshot(blocked=True, country="BR", observed_at=now)
    full_block = _snapshot(blocked=True, country="IR", observed_at=now)
    cases = (
        (
            "unrestricted_open",
            AdmissionOperation.ORDER,
            ExposureEffect.INCREASE,
            unrestricted,
            None,
            None,
            AdmissionStatus.ALLOWED,
            JurisdictionMode.UNRESTRICTED,
        ),
        (
            "close_only_open",
            AdmissionOperation.ORDER,
            ExposureEffect.INCREASE,
            close_only,
            Decimal(1),
            Decimal(2),
            AdmissionStatus.DENIED,
            JurisdictionMode.CLOSE_ONLY,
        ),
        (
            "close_only_reduce",
            AdmissionOperation.ORDER,
            ExposureEffect.REDUCE,
            close_only,
            Decimal(2),
            Decimal(1),
            AdmissionStatus.ALLOWED,
            JurisdictionMode.CLOSE_ONLY,
        ),
        (
            "close_only_reverse",
            AdmissionOperation.ORDER,
            ExposureEffect.REDUCE,
            close_only,
            Decimal(1),
            Decimal(-1),
            AdmissionStatus.DENIED,
            JurisdictionMode.CLOSE_ONLY,
        ),
        (
            "full_block_reduce",
            AdmissionOperation.ORDER,
            ExposureEffect.REDUCE,
            full_block,
            Decimal(2),
            Decimal(1),
            AdmissionStatus.DENIED,
            JurisdictionMode.BLOCK_COMPLETELY,
        ),
        (
            "cancel",
            AdmissionOperation.ORDER_CANCEL,
            ExposureEffect.NEUTRAL,
            None,
            None,
            None,
            AdmissionStatus.ALLOWED,
            JurisdictionMode.NOT_APPLICABLE,
        ),
        (
            "bridge_withdrawal",
            AdmissionOperation.BRIDGE_WITHDRAWAL,
            ExposureEffect.NEUTRAL,
            None,
            None,
            None,
            AdmissionStatus.ALLOWED,
            JurisdictionMode.NOT_APPLICABLE,
        ),
    )
    results: dict[str, dict[str, object]] = {}
    for name, operation, effect, snapshot, before, after, status, mode in cases:
        request = _request(
            run_id,
            name,
            operation=operation,
            effect=effect,
            observed_at=now,
            before=before,
            after=after,
        )
        decision = service.decide(request, geoblock_snapshot=snapshot)
        results[name] = {
            "decision_id": decision.decision_id,
            "status": decision.status.value,
            "mode": decision.jurisdiction_mode.value,
            "pass": decision.status is status and decision.jurisdiction_mode is mode,
        }

    restarted = UnifiedAdmissionService(store=PostgresAdmissionStore())
    replay_request = _request(
        run_id,
        "close_only_reduce",
        operation=AdmissionOperation.ORDER,
        effect=ExposureEffect.REDUCE,
        observed_at=now,
        before=Decimal(2),
        after=Decimal(1),
    )
    replay = restarted.decide(replay_request)
    replay_pass = replay.decision_id == results["close_only_reduce"]["decision_id"]

    with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS rows,
                   count(DISTINCT request_id) AS request_ids,
                   count(*) FILTER (WHERE length(geoblock_raw_payload_hash)=64)
                       AS hashed_geo_rows,
                   count(*) FILTER (WHERE policy_version IS NOT NULL)
                       AS versioned_rows
            FROM quant.simulator_admission_decisions
            WHERE request_id LIKE %s
            """,
            (f"{run_id}:%",),
        )
        database = dict(cur.fetchone())
    expected_geo = sum(case[3] is not None for case in cases)
    database_pass = (
        int(database["rows"]) == len(cases)
        and int(database["request_ids"]) == len(cases)
        and int(database["hashed_geo_rows"]) == expected_geo
        and int(database["versioned_rows"]) == len(cases)
    )
    checks = {
        "decision_matrix": all(bool(row["pass"]) for row in results.values()),
        "restart_idempotency": replay_pass,
        "database_evidence": database_pass,
    }
    return {
        "schema_version": "unified_admission_acceptance_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "cases": results,
        "database": database,
        "live_submission_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
