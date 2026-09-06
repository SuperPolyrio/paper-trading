# Offline Execution Fidelity Benchmark

## Purpose

This benchmark validates execution accuracy without submitting new live orders.
It replays the project's historical L2 archive together with Polygon
`OrderFilled` evidence and preserves the information boundary of public market
data.

The benchmark may establish:

- `OFFLINE_CALIBRATED_SMALL_ORDER` for small taker orders inside visible L2.
- `STRICT_LOWER_BOUND_VALIDATED` for conservative maker fill lower bounds.
- `RESEARCH_CALIBRATED` for maker probability research.
- `DETERMINISTIC_ACCOUNTING_PASS` for deterministic ledger behavior.

It must not claim:

- `LIVE_MAKER_CALIBRATED` without authenticated outcomes for our own orders.
- Exact FIFO position from public L2.
- Large-order market-impact fidelity from immutable historical replay.
- An authenticated NO_FILL merely because public tape contains no compatible fill.

## Evidence

The L2 source is mounted read-only at:

```text
/data/jiahuaiyu/prediction-market-quant/lob_l2_archive_xue
```

Its source is the Western Digital archive on XUE-LAB. Large files are filtered
on XUE-LAB with DuckDB before the selected rows are copied into the local cache.
The report retains the source path and SHA256 evidence. `OrderFilled` data is
read through `OrderFilledEvidenceClient`; external API access does not appear in
the benchmark business logic.

Candidates are selected using activity observed in the previous hour. Label
window data is never used for candidate selection. Samples are split by UTC
date and event into train, calibration, and holdout partitions. Events crossing
partition boundaries are excluded.

Only intervals with a full book baseline, a stable queue epoch, and no proven
gap enter the strict domain. Outcomes are classified as:

- `STRICT_CONFIRMED_FILL`: compatible `OrderFilled` volume conservatively
  consumes queue-ahead and order size.
- `OBSERVED_TAPE_NO_FILL`: a complete public window contains no compatible
  trade; this is a proxy label, not an authenticated order outcome.
- `AMBIGUOUS_ABSTAIN`: incomplete L2, reconnect/rebase, crossing-only evidence,
  or another condition prevents a defensible label.

## Acceptance

The acceptance run checks:

1. Production taker depth walking against an independent reference walker.
2. Differential results from NautilusTrader and HftBacktest.
3. Duplicate, out-of-order, disconnect, restart, and cross-hour fault cases.
4. Deterministic accounting fixtures and a read-only link to official account
   truth without overwriting the paper ledger. Historical calibration-delta
   evidence must match size, initial cost, entry fees, realized PnL, and cash.
5. Source hashes, split leakage, category coverage, and explicit abstention.

Run:

```bash
/home/jiahuaiyu/.conda/envs/prediction-market-quant/bin/python \
  scripts/run_offline_execution_fidelity_benchmark.py \
  --start 2026-07-25T00:00:00Z \
  --end 2026-08-25T00:00:00Z \
  --max-days 9 \
  --per-category-per-day 1 \
  --max-samples 48 \
  --maker-horizon-seconds 300
```

The CLI prints a compact acceptance summary. Add `--print-full-report` only when
all sample rows are needed on stdout. The complete report is always written to
`runtime_outputs/offline_execution_fidelity/latest/summary.json`.

## Aggregate Accuracy Gate

`PASS_OFFLINE` only means that the selected offline run satisfied its own
deterministic and evidence-safety gates. It does not by itself establish broad
cross-validation or live calibration. Build the aggregate report with:

```bash
/home/jiahuaiyu/.conda/envs/prediction-market-quant/bin/python \
  scripts/run_simulator_accuracy_acceptance.py \
  --offline-summary \
  runtime_outputs/offline_execution_fidelity/broad-20260901-v2/summary.json
```

The aggregate report separately publishes:

- distinct simulated orders and independent events;
- accepted, failed, data-insufficient, and abstained scenarios;
- UTC-day, category, price, and liquidity-regime coverage;
- `SMOKE`, `REPRESENTATIVE`, or `BROAD_CROSS_VALIDATED` scale;
- Taker and Maker live-promotion decisions;
- deterministic accounting, latest comparable account truth, and latest
  official-capture health as separate fields;
- every claim that remains unestablished.

A healthy `OFFICIAL_ONLY` capture does not prove a Paper account comparison.
Conversely, an older calibration-delta comparison remains historical evidence
but is reported as `PASS_HISTORICAL` once it is more than 24 hours behind the
latest official capture. It cannot close the current-account gate.

`BROAD_CROSS_VALIDATED` requires at least 10,000 simulated orders plus broad
non-overlapping event/date/category/price/liquidity coverage. Repeating several
quote positions or horizons at one market timestamp increases the order count,
but never the independent-event count.
