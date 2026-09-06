# Paper Tenant Platform Runbook

## Boundary

PR-6 adds the product ownership layer without changing the accepted execution
ledger key. `quant.paper_accounts.strategy_id` remains the internal financial
key; `quant.paper_account_registry.account_id` is the external account identity.

The supported ownership chain is:

```text
tenant -> user/membership -> account -> strategy -> deployment
       -> intent ownership -> order/fill/ledger read models
```

API keys, OAuth/session handling, OpenAPI and SDKs belong to PR-7. No paper
tenant component contains a Polymarket private key or a live-order client.

## Local Installation

All mutating commands require `--apply`. They use the configured paper database.

```bash
scripts/manage_paper_tenants.py --apply init-schema
psql "$PAPER_DATABASE_URL" -f deploy/postgres/paper_tenant_runtime_role.sql
psql "$PAPER_DATABASE_URL" -f deploy/postgres/verify_paper_tenant_runtime_role.sql
```

Bootstrap is idempotent. The tenant UUID is derived from the idempotency key.

```bash
scripts/manage_paper_tenants.py --apply bootstrap \
  --tenant-name research \
  --owner-email owner@example.invalid \
  --owner-display-name Owner \
  --idempotency-key research-v1
```

Create more accounts with unique idempotency keys. A fork is rejected while the
parent has `QUEUED`, `PROCESSING`, or `WORKING` intents or active reservations.
The fork copies cash and positions at one hash-addressed snapshot, resets all
reserved quantities to zero, and never copies open orders.

## RLS Contract

Every tenant-owned table has `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL
SECURITY`. Read and write policies use only:

```sql
current_setting('app.current_tenant_id', true)
```

An absent or empty setting sees zero rows. Repository transactions set this
value before membership or resource queries. The tenant runtime capability has
`NOBYPASSRLS`, no login, read-only access to tenant-scoped metadata/views, and no
direct access to legacy account, intent, fill, ledger or journal tables.

Run the rollback-only negative probe only against a migrated test database:

```bash
scripts/run_paper_tenant_rls_probe.py --confirm-test-database
scripts/run_paper_tenant_postgres_acceptance.py --confirm-disposable-database
```

The probe creates two temporary tenants, assumes the restricted capability,
proves tenant A cannot see tenant B and proves missing scope returns zero rows,
then rolls the transaction back.

Tenant tables are included in same-region snapshot/parity migration. Because
`FORCE RLS` would otherwise make an unscoped migration look like an empty data
set, source and target migration connections must use a dedicated schema
migration identity with `BYPASSRLS` (or a superuser operator). The application
and paper worker roles remain `NOBYPASSRLS`; migration fails before truncation
when the privileged identity is missing.

## Quotas

Default hard limits cover user intents/s, account open orders, strategy
watchlist tokens, replay concurrency, API requests/minute, DB query seconds/minute
and archive export bytes/day. Consumption is serialized by a locked meter row.
`(tenant_id, idempotency_key)` makes retries free of double charging. Missing
quota configuration fails closed.

Gauge quotas (`window_seconds=0`) use a permanent bucket and must be checked
against the authoritative current count before admitting a command. PR-7 owns
the HTTP rate-limit headers and public error mapping.

## Acceptance

```bash
pytest -q tests/execution/test_paper_tenant_platform.py
scripts/run_paper_tenant_acceptance.py
```

The deterministic report does not touch a database or network and cannot prove
that production RLS has been applied. Production acceptance additionally
requires applying the schema/role to the isolated paper DB and running the
rollback-only RLS probe with the restricted role.

The local PostgreSQL 16 disposable-database gate additionally covers tenant
bootstrap replay, user membership and impersonation, multiple accounts,
quiescent fork and position snapshot, strategy deployment replay, intent
ownership, quota replay/denial, audit chaining, and source/target parity.
