# Polymarket Simulator Official Reference Index

Accepted snapshot: `2026-08-18T033128Z`

This index maps simulator behavior to the downloaded official documentation.
The complete corpus remains authoritative; this file only identifies the pages
that should be reviewed first during the simulator compliance audit.

## Evidence hierarchy

1. Use the snapshot for protocol semantics, documented statuses, API schemas,
   and venue behavior.
2. Use decision-time CLOB/Gamma metadata for fee rate, tick size, minimum size,
   order acceptance, delay mode, and market state because these can change.
3. Use authenticated order/trade APIs, User WebSocket events, and onchain
   finality for real execution truth.
4. Use the simulator ledger plus current/closed positions and settlement truth
   for realized and unrealized PnL. Public activity alone is not final PnL truth.

## Market and token lifecycle

- [Markets and events](./snapshots/2026-08-18T033128Z/raw/concepts/markets-events.md)
- [Discover markets](./snapshots/2026-08-18T033128Z/raw/market-data/discover-markets.md)
- [Market details and trading constraints](./snapshots/2026-08-18T033128Z/raw/market-data/market-details.md)
- [Resolution](./snapshots/2026-08-18T033128Z/raw/concepts/resolution.md)
- [Negative risk](./snapshots/2026-08-18T033128Z/raw/concepts/negative-risk.md)
- [Position tokens](./snapshots/2026-08-18T033128Z/raw/concepts/positions-tokens.md)

The registry and simulator must distinguish event, market, condition, and
outcome token identity; accepting orders, active, closed, archived, and
resolved are separate facts. Resolution requires winning-outcome truth rather
than inferring it from a closed flag.

## Order book and real-time data

- [Prices and order book](./snapshots/2026-08-18T033128Z/raw/concepts/prices-orderbook.md)
- [Price and order-book APIs](./snapshots/2026-08-18T033128Z/raw/market-data/prices-order-books.md)
- [Real-time market data](./snapshots/2026-08-18T033128Z/raw/market-data/realtime-data.md)
- [Market WebSocket channel](./snapshots/2026-08-18T033128Z/raw/api-reference/wss/market.md)
- [CLOB OpenAPI specification](./snapshots/2026-08-18T033128Z/raw/api-spec/clob-openapi.yaml)
- [Market AsyncAPI specification](./snapshots/2026-08-18T033128Z/raw/asyncapi.json)

Depth fills must consume the actual price levels available at the simulated
decision time. Snapshot, delta, reconnect, tick-size changes, and one-sided or
empty books need explicit states; a REST seed is a recovery baseline, not proof
that missing WebSocket deltas never existed.

## Order creation and execution

- [Place orders](./snapshots/2026-08-18T033128Z/raw/trading/place-orders.md)
- [Order lifecycle](./snapshots/2026-08-18T033128Z/raw/concepts/order-lifecycle.md)
- [Manage orders](./snapshots/2026-08-18T033128Z/raw/trading/manage-orders.md)
- [Real-time order updates](./snapshots/2026-08-18T033128Z/raw/trading/realtime-order-updates.md)
- [User WebSocket channel](./snapshots/2026-08-18T033128Z/raw/api-reference/wss/user.md)
- [User AsyncAPI specification](./snapshots/2026-08-18T033128Z/raw/asyncapi-user.json)
- [Error codes](./snapshots/2026-08-18T033128Z/raw/resources/error-codes.md)

The execution model must cover BUY and SELL, GTC, GTD, FOK, FAK, post-only,
partial fills, cancellation of the remaining quantity, price improvement,
balance and allowance reservation, minimum size, tick size, and the difference
between order acceptance, matching, mining, confirmation, retry, and permanent
failure. GTD timing and market-order amount units must follow the current SDK or
API contract rather than assumptions.

## Venue modes, delays, and operational behavior

- [Matching-engine restarts](./snapshots/2026-08-18T033128Z/raw/trading/matching-engine.md)
- [Trading rate limits](./snapshots/2026-08-18T033128Z/raw/api-reference/trading-rate-limits.md)
- [General rate limits](./snapshots/2026-08-18T033128Z/raw/api-reference/rate-limits.md)
- [Heartbeat endpoint](./snapshots/2026-08-18T033128Z/raw/api-reference/trade/send-heartbeat.md)
- [Market making](./snapshots/2026-08-18T033128Z/raw/trading/market-making.md)

Admission and timing need to model HTTP 425 maintenance, cancel-only mode,
the post-restart post-only window, heartbeat-triggered mass cancellation,
selected-market taker delay, configured sports delay, and rate-limit outcomes.
Retries cannot be treated as new independent orders unless idempotency and the
unknown-submission state have been reconciled.

## Fees, rebates, and rewards

- [Trading fees](./snapshots/2026-08-18T033128Z/raw/trading/fees.md)
- [Maker rebates](./snapshots/2026-08-18T033128Z/raw/programs/maker-rebates.md)
- [Taker rebates](./snapshots/2026-08-18T033128Z/raw/programs/taker-rebates.md)
- [Liquidity rewards](./snapshots/2026-08-18T033128Z/raw/programs/liquidity-rewards.md)
- [Builder fees](./snapshots/2026-08-18T033128Z/raw/programs/builders/fees.md)
- [Fee-rate endpoint](./snapshots/2026-08-18T033128Z/raw/api-reference/market-data/get-fee-rate.md)
- [Current maker rebated fees](./snapshots/2026-08-18T033128Z/raw/api-reference/rebates/get-current-rebated-fees-for-a-maker.md)

Platform taker fees are applied at match time and depend on current market
metadata. Maker rebates, taker tier rebates, liquidity rewards, referral
payments, and additive builder fees are separate cash flows with different
eligibility and settlement schedules. They must not be folded into one static
fee percentage or recognized before the venue reports them.

## Positions, SELL, PnL, and settlement

- [How positions work](./snapshots/2026-08-18T033128Z/raw/trading/positions/how-positions-work.md)
- [Manage positions](./snapshots/2026-08-18T033128Z/raw/trading/positions/manage.md)
- [Combinatorial positions](./snapshots/2026-08-18T033128Z/raw/trading/positions/combinatorial.md)
- [Wallet activity](./snapshots/2026-08-18T033128Z/raw/trading/wallet-activity.md)
- [Current positions API](./snapshots/2026-08-18T033128Z/raw/api-reference/core/get-current-positions-for-a-user.md)
- [Closed positions API](./snapshots/2026-08-18T033128Z/raw/api-reference/core/get-closed-positions-for-a-user.md)
- [Blockchain data](./snapshots/2026-08-18T033128Z/raw/resources/blockchain-data.md)

SELL accuracy requires inventory reservation, partial reduction, cash proceeds,
fees, cost-basis policy, and realized PnL. NAV additionally needs open-order
reserves, mark policy, unrealized PnL, rebates or rewards only when earned, and
winning-token redemption. Split, merge, redeem, and negative-risk conversions
must be separate ledger events and remain provisional until finality.

## Official user behavior and account operations

The separate [Help Center catalog](./help_center/snapshots/2026-08-18T073151Z/catalog.md)
contains every official sitemap article and collection. Important simulator and
accounting references include:

- [Can I Sell Early?](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364247-can-i-sell-early.md)
- [Limit Orders](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364444-limit-orders.md)
- [Trading Fees](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364478-trading-fees.md)
- [Trading Limits](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364481-does-polymarket-have-trading-limits.md)
- [How Prices Are Calculated](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364488-how-are-prices-calculated.md)
- [Holding Rewards](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364459-holding-rewards.md)
- [Liquidity Rewards](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364466-liquidity-rewards.md)
- [Maker Rebates](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364471-maker-rebates-program.md)
- [Sponsor Market Rewards](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13755867-sponsor-market-rewards.md)
- [Referral Program](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/14174498-referral-program.md)
- [Market Resolution](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364518-how-are-prediction-markets-resolved.md)
- [Market Clarification](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364548-how-are-markets-clarified.md)
- [Market Disputes](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364551-how-are-markets-disputed.md)
- [Combos](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/15458600-what-are-combos.md)
- [Exchange Upgrade and pUSD](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/14762452-polymarket-exchange-upgrade-april-28-2026.md)

User funding and account state are also part of the accounting boundary:

- [Sign-up](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13369877-how-to-sign-up.md)
- [Deposit](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13369887-how-to-deposit.md)
- [Withdraw](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13369898-how-to-withdraw.md)
- [Recover Missing Deposit](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364241-recover-missing-deposit.md)
- [Export Key](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364258-how-do-i-export-my-key.md)
- [Money Safety](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364260-is-my-money-safe.md)
- [Geographic Restrictions](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13364163-geographic-restrictions.md)
- [Network Error](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13510453-how-to-resolve-network-error.md)
- [Claim Failed](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/13550400-how-to-resolve-claim-failed.md)
- [Delete Account](./help_center/snapshots/2026-08-18T073151Z/markdown/en/articles/15458866-how-to-delete-your-polymarket-account.md)

These pages introduce cash flows and lifecycle events beyond order fills. The
ledger must keep deposits, withdrawals, recovery, holding rewards, liquidity
rewards, maker rebates, referrals, sponsorship commitments and refunds,
resolution bonds, claims, redemptions, and account restrictions distinct from
trading PnL.

## User eligibility and trading conduct

- [English Terms of Use HTML](./policies/snapshots/2026-08-18T074341Z/html/tos.html)
- [Simplified Chinese Terms HTML](./policies/snapshots/2026-08-18T074341Z/html/zh-tos.html)
- [English Privacy Policy HTML](./policies/snapshots/2026-08-18T074341Z/html/privacy.html)
- [Simplified Chinese Privacy HTML](./policies/snapshots/2026-08-18T074341Z/html/zh-privacy.html)
- [Market Integrity Markdown](./policies/snapshots/2026-08-18T074341Z/markdown/market-integrity.md)
- [All localized official policies](./policies/snapshots/2026-08-18T074341Z/catalog.md)
- [Policy integrity manifest](./policies/snapshots/2026-08-18T074341Z/manifest.json)

The shared admission layer for paper and live commands must represent blocked,
close-only, cancel-only, and fully enabled account states. It must fail closed
on unknown eligibility and must not use VPN or proxy routing to bypass venue
geographic restrictions. Own-order controls must also prevent self-dealing,
wash trading, spoofing, front-running, fictitious transactions, attempted
manipulation, and other prohibited conduct identified by the official policy.

## Advanced products and version changes

- [pUSD](./snapshots/2026-08-18T033128Z/raw/concepts/pusd.md)
- [Combo overview](./snapshots/2026-08-18T033128Z/raw/trading/combos/overview.md)
- [Combo builder execution](./snapshots/2026-08-18T033128Z/raw/trading/combos/builders.md)
- [Combo market makers](./snapshots/2026-08-18T033128Z/raw/trading/combos/market-makers.md)
- [Combo collateral return](./snapshots/2026-08-18T033128Z/raw/trading/combos/collateral-return.md)
- [Changelog](./snapshots/2026-08-18T033128Z/raw/changelog/predictions.md)
- [Complete catalog](./snapshots/2026-08-18T033128Z/catalog.md)
- [Hash manifest](./snapshots/2026-08-18T033128Z/manifest.json)

Perpetuals, bridge operations, Combo RFQ, and CLOB V2 are present in the full
mirror. They are not silently included in the binary-market simulator: each
requires an explicit product model and acceptance suite before being enabled.

## Mandatory audit output

For every simulator behavior, the next compliance audit should record:

- official snapshot path and SHA256;
- applicable market or product scope;
- current code owner and test;
- implemented, partial, missing, or intentionally out of scope;
- runtime field used when static documentation is insufficient;
- real holdout evidence required before claiming live-equivalent accuracy.
