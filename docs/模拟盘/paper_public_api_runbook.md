# Paper Public API Runbook

## Status

The `/v1/paper` contract is implemented and accepted locally. It is paper-only:
the server loads no wallet credential and exposes no live-order adapter. Public
internet deployment remains a separate production gate.

The local Account Manager is available at `/paper.html`. It consumes only this
tenant-scoped API and stores its bearer token in browser session storage, never
in a URL or persistent local storage.

## Contract

- Base path: `/v1/paper`
- OpenAPI: `/v1/paper/openapi.json`
- Checked-in schema: `docs/api/paper-v1-openapi.json`
- Authentication: tenant-scoped bearer API key
- Mutation safety: `Idempotency-Key` is required
- Pagination: opaque `cursor` plus `limit` from 1 to 200
- Rate limit: `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`
- Trace: `X-Request-Id`
- Execution marker: `X-Paper-Trading-Mode: PAPER_ONLY`

Every success uses `{data, meta}`. Every failure uses:

```json
{
  "error": {
    "code": "PAPER_VALIDATION_ERROR",
    "message": "...",
    "details": {},
    "request_id": "..."
  }
}
```

Stable error families are `PAPER_AUTH_*`, `PAPER_FORBIDDEN`,
`PAPER_VALIDATION_ERROR`, `PAPER_*_NOT_FOUND`, `PAPER_IDEMPOTENCY_*`,
`PAPER_CONFLICT`, `PAPER_RATE_LIMITED`, `PAPER_REPLAY_*`, and `PAPER_INTERNAL`.

Replay resources are `/replays`, `/replays/{id}`, `/pause`, `/resume`, `/fork`,
`/events`, and `/report`. A replay freezes persisted paper lifecycle events and
NAV/TCA/fill evidence at creation. It does not rematch historical L2 or submit a
live order.

## Credentials

Install a random pepper without putting it in an environment file:

```bash
openssl rand -base64 48 | scripts/install_paper_secret_from_stdin.sh \
  paper-api-key-pepper \
  ~/.config/prediction-market-quant/secrets/paper-api-key-pepper
```

Create a key with the trusted operator CLI. The token is printed once; the DB
stores only an HMAC-SHA256 hash.

```bash
PAPER_API_KEY_PEPPER_FILE=~/.config/prediction-market-quant/secrets/paper-api-key-pepper \
scripts/manage_paper_tenants.py --apply create-api-key \
  --tenant-id TENANT_UUID \
  --actor-user-id OWNER_UUID \
  --name sdk \
  --scope paper:read \
  --scope paper:trade \
  --scope paper:accounts:write
```

Revoke it with `--apply revoke-api-key --api-key-id KEY_UUID`. Public clients
cannot create or rotate credentials.

## Local Start

The secure wrapper disables dotenv loading and requires both credential files:

```bash
PAPER_API_KEY_PEPPER_FILE=/run/credentials/paper-api-key-pepper \
PAPER_DB_PASSWORD_CREDENTIAL_PATH=/run/credentials/paper-db-password \
PAPER_SECURITY_REQUIRE_DB_CREDENTIAL_FILE=1 \
scripts/run_paper_api_secure.sh --host 127.0.0.1 --port 18510
```

`PAPER_API_ALLOWED_ORIGINS` is an explicit comma-separated allowlist. An empty
value disables browser cross-origin access; it never falls back to `*`.

## Idempotency

The identity is `(tenant, user, operation, Idempotency-Key)`. Reusing the key
with the same canonical body replays the completed response. Reusing it with a
different body returns `409 PAPER_IDEMPOTENCY_CONFLICT`. An in-flight lease
returns `409 PAPER_IDEMPOTENCY_IN_PROGRESS`; an expired or failed lease may be
retried. Paper `client_order_id` is derived from the idempotency key when absent,
so a crash between intent creation and HTTP completion cannot create a second
intent.

## Versioning And Deprecation

Breaking resource or field changes require `/v2`. Additive fields may ship in
`/v1`. A deprecated operation remains supported for at least 90 days and must
return `Deprecation` and `Sunset` headers before removal. Contract changes must
update the OpenAPI artifact, both SDKs, this changelog, and contract tests in the
same change.

## Changelog

### 2026-08-16

- Added tenant-scoped replay sessions with frozen data/config hashes,
  pause/resume cursor, fork, deterministic artifacts, strategy performance,
  risk, data-quality, and benchmark reports.
- Added replay resources to OpenAPI, Python/TypeScript SDKs, and the local
  Account Manager. The UI labels this evidence as recorded lifecycle replay,
  not L2 rematching.
- Added disposable PostgreSQL replay acceptance covering idempotency,
  pause/resume/fork, repeated artifact-hash equality, and cross-tenant denial.
- Added account performance, NAV history, journal, TCA, order audit, and bounded
  account export resources.
- Added CSV and JSONL serialization with row-count and SHA256 response headers;
  export bytes are charged to the tenant archive-export quota.
- Added mark-quality fields to positions and mandatory fidelity, calibration,
  data-quality, capacity, model, and checkpoint evidence to order audit output.
- Added Python and TypeScript SDK methods plus the local Account Manager UI for
  accounts, orders, positions, fills, ledger, journal, NAV/TCA, audit, and export.
- Added disposable PostgreSQL and desktop/mobile browser acceptance. Neither
  acceptance starts a worker, makes a network API call, or submits a live order.

### 2026-08-07

- Added `/v1/paper` accounts, fork, orders, cancel, replace, positions, fills,
  and ledger resources.
- Added hash-only API keys, idempotency leases, request auditing, quotas, opaque
  pagination, stable errors, OpenAPI, and Python/TypeScript SDKs.
- Added disposable PostgreSQL acceptance for tenant isolation and paper-only
  intent enqueue/cancel.

## Acceptance

```bash
python scripts/export_paper_openapi.py --check
pytest -q tests/execution/test_paper_public_api.py
python scripts/run_paper_public_api_postgres_acceptance.py \
  --confirm-disposable-database
python scripts/run_paper_account_manager_postgres_acceptance.py \
  --confirm-disposable-database
python scripts/run_paper_replay_postgres_acceptance.py \
  --confirm-disposable-database
pytest -q tests/execution
```

The PostgreSQL acceptance refuses non-loopback hosts and database names without
`test` or `acceptance`. It never starts the paper worker or submits a live order.

The Account Manager acceptance additionally verifies NAV/history, position mark
quality, order timeline and checkpoints, TCA, balanced journal lines, exports,
and cross-tenant denials. Browser acceptance lives in
`tests/e2e/paper_account_visual.spec.cjs` and covers desktop, order detail, and a
390 px mobile viewport. Replay acceptance additionally verifies frozen event
order, deterministic parent/fork hashes, performance/risk/benchmark reports,
and that neither network nor live-order submission is used.

## Local Account Manager

Serve the static workspace locally:

```bash
python -m http.server 18520 --bind 127.0.0.1 --directory webpage
```

Open `http://127.0.0.1:18520/paper.html`. When the API runs separately on port
18510, use `http://127.0.0.1:18510/v1/paper` as the API base and include
`http://127.0.0.1:18520` in `PAPER_API_ALLOWED_ORIGINS`. The checked-in default
`/v1/paper` remains suitable when the page and API share an origin.
