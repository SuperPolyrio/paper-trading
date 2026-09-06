# Polymarket CTF PR-1 Implementation Evidence

Date: 2026-08-18

Source guide:

- `docs/模拟盘/polymarket_ctf_operations_and_account_economics_codex_guide.md`

## Scope completed

### Position Operation reservation

- `SPLIT` reserves collateral through `quant.paper_accounts.cash_reserved`.
- `MERGE`, `REDEEM`, and `NEG_RISK_CONVERT` reserve every token debit through
  `quant.paper_positions.reserved_quantity`.
- CLOB order reservations and position-operation reservations share the same
  aggregate availability, so one resource cannot be promised twice.
- Repeated create and process restart retain one durable reservation.
- `CONFIRMED` consumes the reservation in the same transaction as balance,
  position, cost-basis, realized-PnL, and ledger updates.
- `FAILED` releases the reservation without changing economic balances.
- Daily accounting reconciles both order and operation reservations.

Durable tables:

- `quant.simulator_position_operation_reservations`
- `quant.simulator_position_operation_token_reservations`

### CTF match settlement audit

- Every paper fill stores `settlement_match_type`.
- A fill produced from a single L2 book remains `UNKNOWN/NOT_PROVABLE` because
  L2 does not expose sufficient counterparty settlement evidence.
- Paired order or chain evidence classifies:
  - opposite sides on the same asset as `COMPLEMENTARY`;
  - two BUY orders on verified complementary assets as `MINT`;
  - two SELL orders on verified complementary assets as `MERGE`.
- Each classified path receives a deterministic collateral/token conservation
  audit and hash.
- Reconciliation is idempotent; conflicting definitive classifications are
  rejected rather than overwriting history.
- Reconciliation never reapplies the paper fill or changes cash/PnL.

Durable evidence table:

- `quant.paper_fill_ctf_settlement_audits`

## Acceptance evidence

- `reports/simulator_acceptance/paper-position-operations-20260818.json`
  - status: `PASS`
  - 15 checks passed
  - temporary rows cleaned
- `reports/simulator_acceptance/paper-ctf-settlement-20260818.json`
  - status: `PASS`
  - 7 checks passed
  - temporary rows cleaned
- `reports/simulator_acceptance/simulator-capability-audit-20260818.json`
  - overall status: `PASS`
  - `neg_risk_and_complete_set_operations`: `IMPLEMENTED_AND_ACCEPTED`
  - `ctf_match_settlement_audit`: `IMPLEMENTED_AND_ACCEPTED`

## Safety boundary

All acceptance scenarios are paper-only. They submit no live order and perform
no on-chain position operation.

## Next guide item

PR-2 is Complete-set Cost Basis: durable complete-set lots, joint basis,
paired-token provenance, split attribution, merge realization, and redeem
basis removal.
