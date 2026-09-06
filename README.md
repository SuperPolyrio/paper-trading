# SuperPolyrio Paper Trading

This repository owns the Polymarket paper-trading product and its execution
fidelity tooling. It was extracted from `prediction-market-quant` so the live
simulator can evolve without carrying the market-data collector, strategy
research, and general platform code in the same deployment.

## Scope

Included:

- Taker and maker paper execution, order lifecycle, cancel/replace, and TIF.
- Tenant accounts, virtual cash, positions, fees, cost basis, PnL, and NAV.
- Risk, admission, OMS, liquidity reservation, finality, and recovery.
- Split, merge, convert, settlement, redeem, rewards, and account truth.
- Calibration, offline fidelity, live evidence, and operational health gates.
- Public Paper API, Python/TypeScript SDKs, and retail/professional web clients.

Not included:

- Full-market WebSocket collection, L2 archival, or market-registry ownership.
- Strategy discovery and general backtest orchestration.
- Main website, shared infrastructure, or production credentials.
- Runtime Parquet, database dumps, wallet secrets, and historical evidence.

The live worker consumes the existing colocated market-data feed through the
Unix event socket. The small `quant.orderbook` subset in this repository is the
BookState consumer and deterministic replay boundary, not another collector.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
```

Run local commands after supplying a dedicated paper-only environment:

```bash
paper-db apply-schema
paper-api --host 127.0.0.1 --port 18510
paper-worker --help
paper-health --help
```

Live calibration and on-chain operation adapters are deliberately optional:

```bash
python -m pip install -e '.[live]'
```

Paper runtime startup fails closed when live-order credentials leak into its
environment. Real-order probes are separate calibration tools and are never
invoked by the paper worker.

## Repository Map

- `quant/paper`: authoritative worker, API backend, ledger, tenancy, and health.
- `quant/execution`: venue semantics and taker/maker execution models.
- `quant/simulator`: OMS, risk, accounting, lifecycle, and fidelity modules.
- `quant/calibration` and `quant/maker`: paper/live evidence and model gates.
- `scripts`: supported operators and service entry points.
- `deploy`: PostgreSQL, systemd, GCP, Prometheus, and Grafana assets.
- `webpage`: retail and professional paper-trading clients.
- `docs`: product ideas, official-contract references, and runbooks.

See `docs/MIGRATION_AUDIT.md` for the extraction boundary and verification, and
`docs/FEATURE_OWNERSHIP_MATRIX.md` for the code-to-test ownership map.
