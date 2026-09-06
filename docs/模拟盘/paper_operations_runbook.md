# Paper Simulator Operations Runbook

## Scope

This runbook covers the paper execution worker and its PostgreSQL authority. It does not operate the full-market L2 archive and it must never submit a real Polymarket order.

Operational artifacts are written under `runtime_outputs/paper_operations/`:

- `status.json`: current SLO verdict and admission mode.
- `metrics.prom`: Prometheus text exposition.
- `alerts.json`: current alerts.
- `alert-events.jsonl`: append-only FIRING/RESOLVED transitions.
- `report.md`: current human-readable report.

Health endpoints:

- `/health/live`: process heartbeat.
- `/health/ready`: BookState, DB, lease, and fencing readiness.
- `/health/authority`: current writer lease and epoch.
- `/health/slo`: HTTP 200 only for a fresh SLO PASS.
- `/status`: returns a fresh operational snapshot even when degraded.
- `/metrics`: Prometheus metrics.

## Admission Levels

| Level | Admission | Meaning |
|---|---|---|
| GREEN | `ACCEPT` | All required evidence is present and SLO checks pass. |
| YELLOW | `CALIBRATED_TAKER_SMALL_ONLY` | Evidence is incomplete or a non-critical warning exists. |
| ORANGE | `READ_ONLY` | Latency, freshness, capacity, or version checks fail. New orders are blocked. |
| RED | `FAIL_CLOSED` | Authority, accounting, unsafe fill, unknown terminal, or DB safety failed. |

Never manually override ORANGE or RED. Diagnose, reconcile, and wait for a new snapshot to resolve the alert.

The local worker defaults to `--operations-admission-shadow`. Promote it with `PAPER_OPERATIONS_ADMISSION_FLAG=--operations-admission-enforce` only after the snapshot timer is running and the shadow disagreement review passes. A missing or stale operations snapshot is fail-closed in enforce mode.

## First Response

1. Read `status.json`, then note `operational_level`, `admission_mode`, active alerts, worker ID, build ID, and generation time.
2. Check `/health/live`, `/health/ready`, `/health/authority`, and `/health/slo` independently.
3. Preserve `status.json`, `alerts.json`, the worker status file, and relevant journal logs before restarting anything.
4. Confirm whether the failure is isolated to one token, one worker, the execution DB, or all paper commands.
5. For RED, keep paper execution fail-closed until reconciliation returns zero mismatches and a fresh lease is held.

## Source Collection Failure

Alert: `SOURCE_*_FAILED`.

Check the configured status path, build manifest, execution DB credentials, and PostgreSQL reachability. A missing manifest is a warning; acceptance or DB platform collection failure is RED because safety cannot be proven. Fix the source and run `scripts/run_paper_operations_snapshot.sh` again.

## LOB Feed Divergence

Alerts: `READY_BOOK_RATIO`, `BOOK_FRESHNESS`, `FEED_DIVERGENCE`, `EVENT_LAG`.

This runbook only handles the paper BookState consumer. Identify affected asset IDs, move them to resyncing, and block those tokens. Do not restart the full-market archive from this runbook. Resume an affected token only after a new baseline and subsequent event establish a fresh book.

## DB High Latency

Alerts: `DATABASE_AVAILABLE`, `DATABASE_CONNECTION_HEADROOM`, `DB_LATENCY`.

Inspect DB connectivity, active connections, lock waits, long transactions, disk saturation, and replica/primary role. ORANGE permits observation only; unavailable authority DB is RED. Do not switch writers until the old lease is expired or explicitly released and the new epoch is visible.

## Worker Lost Lease

Alerts: `AUTHORITY_HELD`, `AUTHORITY_FENCING`, `AUTHORITY_LEASE_REMAINING`.

Stop command admission immediately. Inspect `paper_execution_partition_leases` and `paper_authority_config`. A replacement worker must acquire a strictly newer epoch. Verify that stale-writer order and ledger writes are rejected before restoring readiness.

## Journal Mismatch

Alerts: `LEDGER_MISMATCH_ZERO`, `RESERVATION_MISMATCH_ZERO`, `JOURNAL_MISMATCH_ZERO`, `NEGATIVE_CASH_ZERO`, `INVALID_POSITION_ZERO`.

Keep the worker RED. Preserve the affected journals, fills, orders, account rows, positions, and finality events. Run reconciliation read-only first. Repair through an idempotent compensating journal or finality transition; never edit account balances or positions directly.

## Unknown Order Terminal

Alert: `UNKNOWN_TERMINAL_ZERO`.

Keep the command blocked and query durable inflight state, order events, fills, and venue evidence using the same idempotency key. Resolve to a proven terminal outcome or `VOIDED`; never infer a fill from a timeout or retry a command with a new key.

## Unsafe Fill

Alert: `UNSAFE_FILL_ZERO`.

This is RED. Stop admission, preserve the causally preceding BookState checkpoint and intent, and verify freshness, sequence, finality, liquidity reservation, and risk decision. Do not delete the fill. Correct it through the finality and accounting reversal path.

## Execution Latency Or Backlog

Alerts: `ORDER_ACCEPTANCE_*`, `TERMINAL_RESULT_*`, `EXECUTION_QUEUE_AGE`.

ORANGE blocks new commands while the worker drains. Inspect oldest queued intent, DB latency, deterministic scheduler lag, and stuck inflight commands. Do not increase concurrency until the liquidity overlay, account reservations, and idempotency behavior have been revalidated at the new setting.

## Deployment Version Skew

Alert: `DEPLOYMENT_VERSION_MATCH`.

Compare the worker `build_id` with `paper-worker-build.json` and verify every manifest hash. Remain read-only until code, migration version, configuration, dashboard, and alert rules identify the same build.

## Service Availability

Alert: `SERVICE_AVAILABILITY`.

Inspect heartbeat gaps and the first failed sample. A finite soak must not erase an earlier failure when the worker later recovers. Resume an unfinished soak only with its original `soak-state.json`; start a new output directory after a completed or failed campaign.

## Missing SLO Evidence

Alert: `METRIC_MISSING_*`.

The state is YELLOW, never an implicit PASS. Generate representative paper intents for missing acceptance or terminal latency metrics, and allow enough samples for availability and freshness percentiles. Do not synthesize production evidence from fixtures or old campaigns.

## Health Observer Gap

`health_sample_continuity=WARN` does not by itself mean that market data or
paper execution stopped. The acceptance runner classifies every observer gap
larger than the health freshness threshold using the worker id, transport
state, cumulative WebSocket counters, per-route counters, endpoint message
freshness, and ready-book ratio.

An observer gap is non-blocking only when it is classified as
`OBSERVER_GAP_WITH_DATA_PROGRESS`. `execution_data_continuity` remains a hard
gate and fails when any interval is classified as
`DATA_PLANE_CONTINUITY_UNPROVEN`, including worker replacement, unavailable
transport, no message progress, stale endpoint messages, or an insufficient
ready-book ratio.

Analyze a closed historical window without changing its original soak report:

```bash
python -m quant.paper.acceptance health-gaps \
  --start-at 2026-08-01T08:02:28.496869+00:00 \
  --end-at 2026-08-02T08:02:28.726702+00:00 \
  --json-out runtime_outputs/paper_live_shadow/health-gap-analysis.json
```

Post-hoc classification is diagnostic evidence. It does not retroactively
promote an original failed soak; run one final soak with the corrected gate.

### Execution Readiness Denominator

`ready_book_ratio` is calculated from `execution_watched_assets` and
`execution_ready_books`, not every retained watch target. Open positions and
in-flight intents may remain watched after a market becomes stale or resolved
for NAV, finality, and settlement processing; those non-tradable tokens must
not lower the new-order readiness SLO. A worker that does not publish the
execution-specific counters is an old build and must not start the final soak.

The `0.98` target remains a final-soak SLO. A ratio from `0.90` through `0.98`
is a partial-universe degradation: operations stay `YELLOW` and admit only
small calibrated taker orders, while the per-token checkpoint, gap, freshness,
and execution-profile gates remain authoritative. A ratio below `0.90` is a
systemic degradation and enters `ORANGE` read-only mode. This prevents a small
set of unhealthy tokens from disabling unrelated healthy markets without
allowing a degraded worker to pass the final soak.

The watch-target query admits new-order candidates only when the execution
catalog says `market_state=LIVE` and `execution_eligible=true`. Active intents
and open paper/calibration positions remain observable through their explicit
reasons, while historical strategy/watchlist rows alone do not keep a closed
market in the real-time execution universe.

## Mass Resolution And Venue Errors

For mass resolution, rate-limit resolution/finality processing, preserve idempotency keys, and reconcile cash and positions after every batch. For venue-style `425`, maintenance, cancel-only, rate-limit, or schema errors, keep the venue model fail-closed and record the exact response class. These conditions must not be converted into simulated fills.

## Soak Promotion

The 6h, 24h, and 7d campaigns use separate output directories. A 24h unit starts only after the 6h report is complete and PASS. A 7d unit starts only after the 24h report is complete, PASS, and its longitudinal SLO status is PASS. A failed campaign is retained for diagnosis and is never automatically restarted.
