# Source Cutover Plan

## Current State

The paper-trading code is independently installable and tested from this
repository. Production cutover has not happened yet. The old checkout remains
the executable path for active paper calibration and account services.

Do not delete paper files from `prediction-market-quant` until this plan is
complete. Those files are untracked in that worktree, so deletion before a
verified cutover would be both destructive and difficult to recover.

## Ownership Boundary

Move to this repository:

- Paper API, worker, database schema, account ledger, and tenant services.
- Calibration portfolio and settlement watchers.
- Maker calibration collector and candidate monitors.
- Official account truth, reward sync, paired reconciler, and daily accounting.
- Paper health, execution catalog, soak, and evidence operations.

Do not move here:

- Full L2 collectors, archive writers, coverage workers, registry daemons, and
  XUE archive mounts. They belong to `market-data`.
- Strategy selection and broad backtest orchestration. They belong to
  `quant-agent` or `backtest-lab` and should depend on this package.
- Website deployment and shared infrastructure. They belong to their existing
  sibling repositories.

## Source Dependencies To Remove

Before source deletion, sibling code still importing `quant.paper` or
`quant.simulator` must install `superpolyrio-paper-trading` and pass its own
tests. Known callers include:

- `quant/backtest/pml2`
- `quant/benchmark`
- `quant/risk/event_risk`
- `quant/settlement`
- `quant/validation`
- legacy tests under `quant/backtest/tests`

Strategy-specific paper wrappers are not copied back into this repository.
Their owners must import the installed paper package instead.

## Safe Cutover

1. Pin the target Git commit and install it in the production conda environment.
2. Back up the existing user unit files and record active service status.
3. Install only the paper-owned units from this repository. Preserve the
   existing `~/.config/prediction-market-quant` environment files; never copy
   credentials into Git.
4. Run database schema status and backup checks. Schema migration must be
   backward compatible with the currently running worker.
5. Switch the DB tunnel and read-only account/reconciliation jobs first.
6. Switch the Paper API, then verify health, OpenAPI, authentication, tenant
   isolation, account reads, order dry-run, and exports.
7. Switch one Paper worker instance. Verify event-socket consumption, database
   checkpoints, queue age, idempotency, positions, PnL, and finality recovery.
8. Switch calibration, settlement, reward, maker, paired-reconciliation, and
   soak services one at a time. Check each journal before proceeding.
9. Restart the host and verify that every paper-owned unit starts from this
   checkout and no unit resolves the old source path.
10. Run the full regression suite and a no-submit end-to-end Paper order after
    restart. Compare account, order, fill, ledger, NAV, and audit outputs.
11. Keep the old checkout read-only for one observation window. Roll back a
    service by restoring its unit and pinned source revision if a gate fails.
12. Only after all callers and services are clean, remove the old paper files in
    a dedicated source-repository commit. Never mass-delete unrelated dirty or
    untracked files.

## Cutover Gates

Cutover is complete only when all of the following are true:

- No active paper-owned systemd unit references the old checkout.
- No sibling production module relies on an undeclared source-tree import.
- API and worker health are green after a reboot.
- Database backup and rollback have been exercised.
- No duplicate fills, ledger rows, reward entries, or finality transitions are
  produced during the handover.
- Runtime evidence and credentials remain outside Git.

Long-duration soak and live maker/taker promotion remain evidence gates. They
do not block repository extraction, and repository extraction does not make
them pass.
