#!/usr/bin/env python3
"""Rollback-only cross-tenant RLS probe for a migrated test database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.core.db import postgres_connection  # noqa: E402


def run_probe() -> dict[str, object]:
    tenant_a = uuid4()
    tenant_b = uuid4()
    checks: dict[str, bool] = {}
    with postgres_connection(readonly=False) as conn, conn.cursor() as cur:
        try:
            for tenant_id, key in ((tenant_a, "a"), (tenant_b, "b")):
                cur.execute(
                    "SELECT set_config('app.current_tenant_id', %s, true)",
                    (str(tenant_id),),
                )
                cur.execute(
                    """
                    INSERT INTO quant.paper_tenants (
                        tenant_id,name,idempotency_key
                    ) VALUES (%s,%s,%s)
                    """,
                    (tenant_id, f"rls-probe-{key}", f"rls-probe:{tenant_id}"),
                )
            cur.execute("SET LOCAL ROLE poly_quant_paper_tenant_runtime")
            cur.execute(
                "SELECT set_config('app.current_tenant_id', %s, true)",
                (str(tenant_a),),
            )
            cur.execute(
                """
                SELECT tenant_id FROM quant.paper_tenants
                WHERE tenant_id=ANY(%s::uuid[]) ORDER BY tenant_id
                """,
                ([str(tenant_a), str(tenant_b)],),
            )
            visible = [str(row["tenant_id"]) for row in cur.fetchall()]
            checks["tenant_a_visible"] = visible == [str(tenant_a)]
            checks["tenant_b_hidden"] = str(tenant_b) not in visible
            checks["missing_scope_default_deny"] = False
            cur.execute("SELECT set_config('app.current_tenant_id', '', true)")
            cur.execute("SELECT count(*) AS count FROM quant.paper_tenants")
            checks["missing_scope_default_deny"] = int(cur.fetchone()["count"]) == 0
        finally:
            conn.rollback()
    return {
        "schema_version": "paper_tenant_rls_probe_v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "rollback_only": True,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-test-database",
        action="store_true",
        help="required guard; all writes are rolled back",
    )
    args = parser.parse_args()
    if not args.confirm_test_database:
        parser.error("--confirm-test-database is required")
    report = run_probe()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
