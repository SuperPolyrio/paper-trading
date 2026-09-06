# Paper Authority Backup And Restore Runbook

## Scope

This runbook covers the paper execution authority database only. The bundle
contains the simulator's durable order, fill, ledger, journal, position,
finality, risk, tenant, OMS, liquidity, settlement and position-operation
tables.

It deliberately excludes:

- market registry and lifecycle history;
- registry outbox and subscription state;
- L2 archive and current coverage tables;
- the derived execution market catalog;
- active authority leases.

Those datasets have separate owners or are rebuilt. Restoring an active lease
would be unsafe, so a recovered database must contain zero lease rows before a
worker is allowed to acquire authority.

## Local Restore Drill

Run from the repository root:

```bash
/home/jiahuaiyu/.conda/envs/prediction-market-quant/bin/python \
  scripts/run_paper_db_restore_drill.py
```

The command performs these steps:

1. Opens one read-only, repeatable-read source transaction.
2. Exports every authority backup table as deterministic CSV.
3. Compresses each table independently with gzip.
4. Records row count, content SHA256, compressed-file SHA256, source WAL LSN
   and transaction snapshot in `manifest.json`.
5. Starts an owned, disposable PostgreSQL container on loopback.
6. Applies the current versioned paper schema.
7. Restores all files in dependency order and resets sequences.
8. Recomputes every table fingerprint from the restored database.
9. Verifies double-entry balance, fill domains, finality domains and zero
   restored authority leases.
10. Removes the disposable target unless `--keep-target` was specified.

Safety checks reject a non-loopback target, the source port, a database not
prefixed with `paper_target_`, and an unowned Docker container.

## Acceptance

The drill is PASS only when all of the following hold:

- manifest and every compressed file pass SHA256 verification;
- every restored table has the original row count and content SHA256;
- all journals are balanced;
- fills satisfy price, size and fee domains;
- ledger finality states match the execution contract;
- no active authority lease is restored;
- no source write and no live exchange submission occurred.

The latest report is:

```text
runtime_outputs/production/db-restore/restore-drill-latest.json
```

The first accepted local run on 2026-08-18 restored 91 tables and 2,059,135
rows from a 107,767,451-byte compressed bundle in 111.803 seconds.

## Recovery Boundary

This is a logical snapshot restore. The report intentionally emits:

```text
pitr_validated=false
```

It does not prove managed PostgreSQL regional failover, WAL point-in-time
recovery, cross-region object retention, deletion protection or production RPO.
Those require the final managed database topology and a separate destructive
recovery exercise against a non-production target.

## Retention

Keep the signed manifest and at least the latest accepted bundle outside the
database host before treating the backup as disaster-recovery evidence. A
local bundle on the same host protects against logical corruption, not host or
region loss. Run the isolated restore drill after schema changes and at least
quarterly once the production storage destination is configured.
