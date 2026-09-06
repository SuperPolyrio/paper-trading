# Unified Eligibility / Geoblock Admission

## Scope

This implementation centralizes eligibility decisions for simulator commands and
the bounded real-probe boundary. It follows the locally archived official
Geographic Restrictions document at:

`docs/reference/polymarket_official/snapshots/2026-08-18T033128Z/raw/api-reference/geoblock.md`

The official document governs order placement. CTF, Bridge, Sponsor and Dispute
operations still pass through the same service and persist a decision, but are
recorded as `NOT_APPLICABLE`; the simulator does not invent undocumented order
geoblock rules for those operations.

## Policy

- `UNRESTRICTED`: OPEN, INCREASE and REDUCE are allowed.
- `CLOSE_ONLY`: only a proven reduction where
  `exposure_before > exposure_after >= 0` is allowed.
- `BLOCK_COMPLETELY`: opening and closing orders are denied.
- Missing, timed-out, stale or unknown order geoblock evidence fails closed.
- Cancel, read and reconciliation operations remain available.

Policy version: `polymarket-geoblock-2026-08-18`.

## Architecture

```text
PolymarketGeoblockClient (explicit process-local proxy, trust_env=false)
    -> JurisdictionPolicy
    -> UnifiedAdmissionService
    -> PostgresAdmissionStore
    -> public API / paper worker / real probe / Combo-RFQ / CTF / account programs
```

The adapter never changes the global Clash profile. The route is selected only
through the client or process configuration.

## Durable Evidence

The migration version is `0024-unified-admission-v1` and owns:

- `quant.simulator_admission_policies`
- `quant.simulator_geoblock_snapshots`
- `quant.simulator_admission_decisions`

Each decision records operation and account identity, policy version, country,
region, detected IP, raw payload SHA256, exposure before/after, status, reasons,
observation time and expiry. Reusing a request ID with a different payload is an
idempotency collision.

## Integrated Boundaries

- Public API submit, replace, single cancel, bulk cancel, market cancel and
  cancel-all.
- Live paper worker before central risk and execution.
- Real taker probe at initial preflight and immediately before submit.
- Combo and RFQ coordinator; construction without admission is rejected.
- Split, Merge, Redeem and Neg-risk Convert create and reconciliation paths.
- Bridge deposit/withdrawal, Sponsor lifecycle and Dispute lifecycle.

Paper API and worker support `OFF`, `SHADOW` and `ENFORCE`. The checked-in GCP
launcher enables shadow mode only; production enforcement requires reviewing
shadow disagreements before changing the launch flag.

## Acceptance Evidence

- Unit and integration matrix covers unrestricted, close-only, full block,
  reverse SELL, API timeout, stale evidence, cache expiry, restart idempotency,
  public API, worker and Combo/RFQ.
- `runtime_outputs/unified_admission/acceptance-latest.json`: PostgreSQL decision
  matrix and restart recovery.
- `runtime_outputs/unified_admission/account-program-fault-latest.json`: Bridge,
  Sponsor, Dispute and account cashflow fault injection.
- `runtime_outputs/unified_admission/multileg-acceptance-latest.json`: durable
  Combo/RFQ and multi-leg acceptance.
- `runtime_outputs/unified_admission/live-route-latest.json`: official live API
  traffic. HK was unrestricted; SG was close-only; reduce was allowed and open
  or reverse exposure was denied.

No real order was submitted during this acceptance. A bounded `SELL 1 share FOK`
prepare was attempted against an existing position, but no held asset was fresh
and dual-feed A/A+ at the order boundary. The runner returned
`phase5c_approved_asset_not_currently_fresh` with
`exchange_order_submitted=false`; no safety or market-data gate was bypassed.
