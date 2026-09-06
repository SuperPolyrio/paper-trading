\set ON_ERROR_STOP on

SELECT CASE WHEN EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='poly_quant_paper_tenant_runtime'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolreplication
      AND NOT rolbypassrls
) THEN 1 ELSE 0 END AS tenant_role_is_least_privilege;

SELECT CASE WHEN NOT has_table_privilege(
    'poly_quant_paper_tenant_runtime',
    'quant.paper_accounts',
    'SELECT'
) AND NOT has_table_privilege(
    'poly_quant_paper_tenant_runtime',
    'quant.paper_live_order_intents',
    'SELECT'
) THEN 1 ELSE 0 END AS no_direct_legacy_execution_access;

SELECT CASE WHEN bool_and(c.relrowsecurity AND c.relforcerowsecurity)
    THEN 1 ELSE 0 END AS all_tenant_tables_force_rls
FROM pg_class c
JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='quant'
  AND c.relname = ANY(ARRAY[
      'paper_tenants',
      'paper_users',
      'paper_memberships',
      'paper_account_registry',
      'paper_account_generations',
      'paper_strategies',
      'paper_strategy_deployments',
      'paper_intent_ownership',
      'paper_quotas',
      'paper_usage_meter',
      'paper_quota_consumptions',
      'paper_api_keys',
      'paper_api_idempotency',
      'paper_api_request_log',
      'paper_tenant_audit_events',
      'paper_tenant_deletion_requests'
  ]);

SELECT CASE WHEN count(*)=16 THEN 1 ELSE 0 END AS tenant_policy_count_is_complete
FROM pg_policies
WHERE schemaname='quant'
  AND policyname='paper_tenant_isolation'
  AND tablename = ANY(ARRAY[
      'paper_tenants',
      'paper_users',
      'paper_memberships',
      'paper_account_registry',
      'paper_account_generations',
      'paper_strategies',
      'paper_strategy_deployments',
      'paper_intent_ownership',
      'paper_quotas',
      'paper_usage_meter',
      'paper_quota_consumptions',
      'paper_api_keys',
      'paper_api_idempotency',
      'paper_api_request_log',
      'paper_tenant_audit_events',
      'paper_tenant_deletion_requests'
  ]);
