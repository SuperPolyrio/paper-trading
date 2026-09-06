\set ON_ERROR_STOP on

-- Metadata/read-model capability for the future public paper API.  Account
-- creation and order submission remain trusted control-plane commands; this
-- role never receives direct access to the legacy execution authority tables.
DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'poly_quant_paper_tenant_runtime'
    ) THEN
        CREATE ROLE poly_quant_paper_tenant_runtime
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
            NOREPLICATION NOBYPASSRLS;
    END IF;
END
$block$;

REVOKE CREATE ON SCHEMA public FROM poly_quant_paper_tenant_runtime;
GRANT USAGE ON SCHEMA quant TO poly_quant_paper_tenant_runtime;

GRANT SELECT ON
    quant.paper_tenants,
    quant.paper_users,
    quant.paper_memberships,
    quant.paper_account_registry,
    quant.paper_account_generations,
    quant.paper_strategies,
    quant.paper_strategy_deployments,
    quant.paper_intent_ownership,
    quant.paper_quotas,
    quant.paper_usage_meter,
    quant.paper_quota_consumptions,
    quant.paper_api_keys,
    quant.paper_api_idempotency,
    quant.paper_api_request_log,
    quant.paper_tenant_audit_events,
    quant.paper_tenant_deletion_requests,
    quant.paper_tenant_orders_v,
    quant.paper_tenant_fills_v,
    quant.paper_tenant_ledger_v
TO poly_quant_paper_tenant_runtime;

REVOKE ALL ON
    quant.paper_accounts,
    quant.paper_positions,
    quant.paper_live_order_intents,
    quant.paper_fills,
    quant.paper_ledger_entries,
    quant.paper_journal_lines
FROM poly_quant_paper_tenant_runtime;

COMMENT ON ROLE poly_quant_paper_tenant_runtime IS
    'Tenant-scoped paper metadata/read models; requires app.current_tenant_id and cannot access legacy execution tables directly.';
