#!/usr/bin/env python3
"""Validate official geoblock truth over explicit routes without submitting orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    AdmissionStatus,
    ExposureEffect,
    JurisdictionMode,
    PolymarketGeoblockClient,
    PostgresAdmissionStore,
    UnifiedAdmissionService,
)


def _request(
    run_id: str,
    name: str,
    *,
    effect: ExposureEffect,
    before: Decimal,
    after: Decimal,
    observed_at: datetime,
) -> AdmissionRequest:
    return AdmissionRequest(
        request_id=f"{run_id}:{name}",
        operation=AdmissionOperation.ORDER,
        account_id=f"{run_id}:account",
        strategy_id=f"{run_id}:strategy",
        asset_id=f"{run_id}:asset",
        exposure_effect=effect,
        exposure_before=before,
        exposure_after=after,
        observed_at=observed_at,
        metadata={"source": "official_geoblock_live_acceptance", "case": name},
    )


def run(*, unrestricted_proxy: str, close_only_proxy: str) -> dict[str, object]:
    run_id = f"live-geoblock-{uuid4().hex[:16]}"
    store = PostgresAdmissionStore()
    store.ensure_schema()
    service = UnifiedAdmissionService(store=store)
    unrestricted = PolymarketGeoblockClient(
        proxy_url=unrestricted_proxy,
        ttl_seconds=60,
    ).snapshot()
    close_only = PolymarketGeoblockClient(
        proxy_url=close_only_proxy,
        ttl_seconds=60,
    ).snapshot()
    cases = (
        (
            "unrestricted_open",
            unrestricted,
            ExposureEffect.INCREASE,
            Decimal(1),
            Decimal(2),
            AdmissionStatus.ALLOWED,
            JurisdictionMode.UNRESTRICTED,
        ),
        (
            "close_only_reduce",
            close_only,
            ExposureEffect.REDUCE,
            Decimal(2),
            Decimal(1),
            AdmissionStatus.ALLOWED,
            JurisdictionMode.CLOSE_ONLY,
        ),
        (
            "close_only_reverse",
            close_only,
            ExposureEffect.REDUCE,
            Decimal(1),
            Decimal(-1),
            AdmissionStatus.DENIED,
            JurisdictionMode.CLOSE_ONLY,
        ),
        (
            "close_only_open",
            close_only,
            ExposureEffect.INCREASE,
            Decimal(1),
            Decimal(2),
            AdmissionStatus.DENIED,
            JurisdictionMode.CLOSE_ONLY,
        ),
    )
    results: dict[str, dict[str, object]] = {}
    for name, snapshot, effect, before, after, expected_status, expected_mode in cases:
        decision = service.decide(
            _request(
                run_id,
                name,
                effect=effect,
                before=before,
                after=after,
                observed_at=snapshot.observed_at,
            ),
            geoblock_snapshot=snapshot,
        )
        results[name] = {
            "status": decision.status.value,
            "mode": decision.jurisdiction_mode.value,
            "country": snapshot.country,
            "region": snapshot.region,
            "detected_ip": snapshot.detected_ip,
            "raw_payload_hash": snapshot.raw_payload_hash,
            "decision_id": decision.decision_id,
            "pass": (
                decision.status is expected_status
                and decision.jurisdiction_mode is expected_mode
            ),
        }
    passed = all(bool(row["pass"]) for row in results.values())
    return {
        "schema_version": "live_geoblock_route_acceptance_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "status": "PASS" if passed else "FAIL",
        "checks": results,
        "routes": {
            "unrestricted_proxy": unrestricted_proxy,
            "close_only_proxy": close_only_proxy,
        },
        "official_api_requests_performed": True,
        "live_order_submitted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--unrestricted-proxy",
        default="http://127.0.0.1:17981",
    )
    parser.add_argument(
        "--close-only-proxy",
        default="http://127.0.0.1:18080",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runtime_outputs/unified_admission/live-route-latest.json"),
    )
    args = parser.parse_args()
    try:
        report = run(
            unrestricted_proxy=args.unrestricted_proxy,
            close_only_proxy=args.close_only_proxy,
        )
    except Exception as exc:
        report = {
            "schema_version": "live_geoblock_route_acceptance_v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "FAIL",
            "error": f"{exc.__class__.__name__}:{str(exc)}",
            "official_api_requests_performed": True,
            "live_order_submitted": False,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
