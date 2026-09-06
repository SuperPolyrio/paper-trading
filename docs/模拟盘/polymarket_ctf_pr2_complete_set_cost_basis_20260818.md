# Polymarket CTF PR-2 Complete-set Cost Basis Evidence

Date: 2026-08-18

Source guide:

- `docs/模拟盘/polymarket_ctf_operations_and_account_economics_codex_guide.md`

## Scope completed

### Durable complete-set lots

- Every paper BUY creates an idempotent source lot.
- Opposite outcome BUY lots are paired into one joint-cost lot while retaining
  links to both source fills.
- Confirmed `SPLIT` creates a paired multi-leg lot.
- Confirmed `NEG_RISK_CONVERT` creates an attributed conversion lot.
- Existing positions without provenance are covered by explicit `TRANSFER_IN`
  bootstrap lots instead of silently inventing history.
- CTF settlement reconciliation can promote a source lot to
  `CTF_MINT_MATCH` without reapplying the fill.

Supported provenance values:

- `SPLIT`
- `SEPARATE_MARKET_BUYS`
- `CTF_MINT_MATCH`
- `NEG_RISK_CONVERSION`
- `TRANSFER_IN`

### Cost-basis consumption

- Paper SELL removes average cost basis and records exact source-lot
  allocations.
- Complete-set Merge consumes equal token quantities, credits collateral, and
  realizes `payout - joint basis`.
- Position-operation Merge, Redeem and negative-risk conversion consume basis
  once at confirmed finality.
- Resolution lifecycle Redeem removes all remaining basis only when cash is
  actually applied.
- Reserved token balances block Merge or Redeem from consuming inventory
  already promised to another order or operation.
- Replays use stable lot and consumption IDs and cannot double-create or
  double-consume basis.

Durable tables:

- `quant.simulator_complete_set_lots`
- `quant.simulator_complete_set_lot_legs`
- `quant.simulator_complete_set_consumptions`
- `quant.simulator_complete_set_consumption_legs`

## Database integration

- Migration: `0013-complete-set-lots-v1`
- All four tables participate in execution snapshot/parity checks.
- All four tables are protected by the execution authority fence.
- Local `poly_data_core` schema apply: `PASS`.
- Authority trigger verification: one active fence trigger on each table.

## Acceptance evidence

- `reports/simulator_acceptance/paper-complete-set-cost-basis-20260818.json`
  - status: `PASS`
  - independent BUY/BUY pairing, SELL, Merge, conservation and replay passed
  - acquired basis `9.90 = 5.87 consumed + 4.03 remaining`
- `reports/simulator_acceptance/paper-position-operations-20260818.json`
  - status: `PASS`
  - Split and negative-risk provenance passed
  - Merge, conversion and Redeem basis consumption passed
- `reports/simulator_acceptance/paper-ctf-settlement-20260818.json`
  - status: `PASS`
  - `SEPARATE_MARKET_BUYS -> CTF_MINT_MATCH` reconciliation passed
- `reports/simulator_acceptance/paper-resolution-lifecycle-20260818.json`
  - status: `PASS`
  - full basis removal at redeemed cash application passed
- `reports/simulator_acceptance/simulator-capability-audit-20260818.json`
  - overall status: `PASS`
  - `complete_set_cost_basis`: `IMPLEMENTED_AND_ACCEPTED`

Relevant regression suite: `94 passed`.

## Safety boundary

All acceptance scenarios are local paper-only database transactions. They do
not submit live orders or on-chain position operations.

## Next guide item

PR-3 is Fee Engine final unification: one effective fee schedule and rounding
contract shared by taker, maker, SELL, accounting, NAV and calibration paths.
