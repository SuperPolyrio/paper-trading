# Migration Audit

## Source And Target

- Source: `prediction-market-quant`
- Target: `SuperPolyrio/paper-trading`
- Source behavior baseline: 893 relevant tests passed with importlib mode.
- Source `quant` size observed during extraction: 472,144 Python lines.
- Final target `quant` size: 143,494 Python lines across 342 files.
- Extracted target regression suite: 820 tests passed under the dedicated
  `prediction-market-quant` conda environment (Python 3.12).
- Compatibility check: the production-runtime tests also pass under the host
  Python 3.10 interpreter.
- Installed command smoke test: all 11 supported CLI entry points passed.
- Import closure: 367 `quant` and `scripts` modules loaded with zero failures.
- HTTP smoke test: health and OpenAPI endpoints returned 200 after a test-only
  Paper API pepper was supplied; startup without it remained fail-closed.
- Secret scan: no credential files, private-key material, or authenticated URLs
  were migrated; explicit dummy values remain only in security tests.
- Official references: 656 manifest entries were rehashed with zero missing or
  mismatched files, and the documentation tree has zero broken local links.

The extraction preserves the existing `quant.*` import paths to avoid changing
execution behavior while ownership moves between repositories.

## Retained

- Production paper API, worker, database migration, ledger, and account model.
- Taker/maker execution and deterministic event ordering.
- Risk, OMS, admission, finality, settlement, rewards, and account truth.
- Offline/live fidelity tools needed to validate execution accuracy.
- Paired paper/live probe, reconciler, and DB control tunnel operations.
- A minimal BookState/event-socket and archive-reader dependency boundary.
- Supported deployment units, dashboards, SDKs, UI, and canonical runbooks.
- The three checksum-pinned official documentation snapshots referenced by the
  simulator compliance index; all local documentation links resolve.

## Deliberately Excluded

- Full L2 collectors, archive writers, registry daemons, and market discovery.
- Broad strategy/backtest research unrelated to simulator execution fidelity.
- One-shot acceptance modules embedded in production packages when equivalent
  tests already exist under `tests/`.
- Obsolete binary-complete-set and logical-constraint service wrappers.
- Duplicate professional-soak wrappers superseded by the GCP soak chain.
- Generated reports, runtime output, Parquet, database exports, and credentials.

This is a code migration, not an evidence rewrite. Offline passes, live model
promotion, account truth, and long-duration soak remain separate gates.

## Cutover Rule

The source tree is intentionally not deleted during the copy-and-verify phase.
Its simulator files are currently untracked in that repository and production
service files still reference the old checkout. Removal is allowed only after:

1. this target is committed and recoverable;
2. deployment paths point to the target checkout;
3. API and worker smoke tests pass against the intended PostgreSQL instance;
4. the old services are stopped or redirected; and
5. a rollback test succeeds.

This prevents a repository cleanup from becoming an accidental production
outage or irreversible loss of untracked work.

The exact service-by-service procedure is in `SOURCE_CUTOVER_PLAN.md`.
