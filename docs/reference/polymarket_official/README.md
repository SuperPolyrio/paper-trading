# Polymarket Official Documentation Mirror

This directory stores reproducible snapshots of the official Polymarket
documentation. Discovery uses both official sources:

- `https://docs.polymarket.com/llms.txt`
- `https://docs.polymarket.com/sitemap.xml`

Each snapshot contains the complete English and Chinese Markdown corpus,
OpenAPI and AsyncAPI specifications, the official concatenated
`llms-full.txt`, and a SHA256 manifest.

The current simulator-oriented reading map is
[SIMULATOR_REFERENCE_INDEX.md](./SIMULATOR_REFERENCE_INDEX.md). It is a derived
index; the per-snapshot manifest remains the integrity truth for official files.

Official end-user behavior is mirrored separately from the developer corpus:

- [Help Center latest snapshot](./help_center/snapshots/2026-08-18T073151Z/README.md)
- [Help Center complete catalog](./help_center/snapshots/2026-08-18T073151Z/catalog.md)
- [Help Center hash manifest](./help_center/snapshots/2026-08-18T073151Z/manifest.json)

The Help Center mirror covers every sitemap article and collection and follows
official JSON links recursively. It preserves article HTML, Markdown, and JSON,
including account, funding, rewards, market resolution, disputes, and support
behavior that is not fully described in the developer documentation.

Official Terms, Privacy, and trading-conduct policies are also mirrored:

- [Policy latest snapshot](./policies/snapshots/2026-08-18T074341Z/README.md)
- [Policy complete catalog](./policies/snapshots/2026-08-18T074341Z/catalog.md)
- [Policy hash manifest](./policies/snapshots/2026-08-18T074341Z/manifest.json)

The policy mirror follows every localized Terms and Privacy URL linked by the
official site and preserves the separate Market Integrity policy.

Refresh without changing the machine-wide Clash selection:

```bash
python scripts/sync_polymarket_official_docs.py \
  --proxy-url http://127.0.0.1:17980

python scripts/sync_polymarket_help_center.py \
  --proxy-url http://127.0.0.1:17980

python scripts/sync_polymarket_official_policies.py \
  --proxy-url http://127.0.0.1:17980
```

`latest.json` identifies the newest zero-failure snapshot, while
`latest_attempt.json` records the newest synchronization attempt even if it was
partial. Simulator behavior should cite a snapshot URL and hash, while
decision-time fee, rebate, tick-size, minimum-size, delay, and market-state
values must still come from live venue metadata.
