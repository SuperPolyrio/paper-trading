# Polymarket Simulator PR-3: Authoritative Fee Engine

Date: 2026-08-18

Status: IMPLEMENTED_AND_ACCEPTED

## Scope

PR-3 replaces the remaining linear execution-fee fallback with one
point-in-time, per-fill economics contract shared by:

- taker L2 walk-book fills;
- maker trade-evidence fills;
- BUY all-in cash reservation;
- SELL cash and realized PnL accounting;
- paper CLI/API builder-attributed orders;
- PostgreSQL audit and migration parity;
- official fill fee reconciliation.

No live order was submitted for this acceptance.

## Authoritative Semantics

Platform fee:

```text
shares * platform_fee_rate * (price * (1 - price)) ^ exponent
```

Builder fee:

```text
shares * price * builder_fee_bps / 10000
```

Each component is rounded independently to five decimal places for every
fill. Order total fee is the sum of the rounded fill components. A maker pays
zero platform fee when the bound schedule is taker-only, while a maker builder
fee can still apply. Fee-free schedules produce zero platform fee.

Rebate and reward estimates are not netted against fill fees. They remain
separate delayed economics for later PRs.

Official references:

- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/builders/fees
- https://docs.polymarket.com/market-makers/maker-rebates

## Implementation

The authoritative domain is under `quant/simulator/economics/`:

- `fee_engine.py`: immutable per-fill platform and builder fee charge;
- `fee_schedule_registry.py`: deterministic effective-dated schedules;
- `fee_rounding.py`: five-decimal minimum-unit policy;
- `builder_fee_engine.py`: maker/taker builder fee limits and calculation;
- `fee_reconciler.py`: modeled-versus-official fee comparison;
- `paper_fee_engine_acceptance.py`: bounded PostgreSQL acceptance.

`quant/paper/taker_execution.py` is the only formal paper calculation entry.
The compatibility `quant/execution/models/fee_rebate.py` adapter delegates to
the same engine and no longer calculates a linear fee.

## Durable Evidence

Migration:

```text
0014-authoritative-fee-engine-v1
```

Tables:

```text
quant.paper_fee_schedules
quant.paper_fill_fee_charges
```

Every fee charge records its fill, asset, condition, liquidity role, price,
shares, applied platform parameters, platform fee, builder code/rate/fee,
rounding policy, schedule ID, economics regime ID and source. The fee charge
ID is deterministic, so replay cannot debit a fill twice.

Both tables are included in execution DB snapshots; fee charges and schedules
are included in core migration parity checks.

## Acceptance

Report:

```text
reports/simulator_acceptance/paper-fee-engine-20260818.json
```

Accepted scenarios:

- two-price taker order uses sum of independently rounded fill fees;
- builder fee stacks on platform fee;
- maker platform fee is zero while maker builder fee remains chargeable;
- fee-free market produces zero fee;
- all-in reservation includes platform and role-specific builder maximums;
- repeated result application does not duplicate a fee charge;
- failed FOK leaves no fee charge;
- BUY cash/cost basis and SELL realized PnL include total fill fee;
- point-in-time schedule is persisted and bound to the command;
- exact official fill amounts reconcile within `0.00001` USDC;
- all temporary PostgreSQL acceptance rows are removed.

Capability audit result:

```text
authoritative_fee_engine = IMPLEMENTED_AND_ACCEPTED
```

## Explicit Boundary

This PR does not claim maker rebate, taker rebate, liquidity reward, holding
reward or total-account-return parity. Those are delayed account cashflows and
must not reduce a fill fee before official receipt.
