# Polymarket Exchange Upgrade: April 28, 2026

On **April 28, 2026 at ~11:00 UTC**, Polymarket is rolling out a coordinated upgrade of the exchange stack: new smart contracts, a rewritten order book, and a new collateral token. Trading will be paused for approximately 1 hour (until ~ 12:00 UTC) during the cutover.

## Key Details

- **When:** April 28, 2026, starting around 11:00 UTC

- **Downtime:** ~1 hour of paused trading

- **What you need to do:** For most users, nothing. You just need to approve the conversion of your balance to pUSD, and approve trading on the upgraded contracts.

- **Your open orders:** All existing order books will be cleared during the maintenance window. You'll need to re-place orders after the upgrade.

- **Your funds:** Your balance carries over. pUSD is backed 1:1 by USDC, with backing enforced onchain by the smart contract.

**Are you a builder or dev?** See the dev migration guide [here](https://docs.polymarket.com/v2-migration).

## What's changing

### New Exchange contracts (CTF Exchange V2)

The core smart contracts have been rebuilt. Improvements include:

- Simplified order structure

- Optimized order matching

- Improved fee collection and distribution

- Lower gas costs (new contracts use more efficient libraries)

### Updated order book

- **Reliability:** The new architecture reduces the balance-check race conditions and nonce invalidation issues that caused most failed trades. Nonce-related execution failures go away, along with edge cases that caused failed trades.

- **Fee handling:** Fees are now calculated at match time instead of order placement time, which removes a category of edge-case failures.

- **Order management:** Better order lifecycle tracking.

### A new collateral token: Polymarket USD (pUSD)

Polymarket is migrating from USDC.e to **Polymarket USD (pUSD)** as the collateral token.

- pUSD is a standard ERC-20 token on Polygon, backed 1:1 by USDC.

- The backing is enforced onchain by the smart contract — no algorithmic peg, no fractional reserve.

- The protocol settles all trading activity in native USDC, providing a more capital efficient and scalable settlement standard.

- **Day to day, nothing changes.** You load funds, you see a balance, you trade, you withdraw. pUSD is the technical layer underneath.

## What to expect on April 28

### During the window

Trading will be paused for approximately 1 hour on April 28, starting around 11:00 UTC. All existing order books will be cleared.

Follow [Discord](https://discord.gg/polymarket), Telegram, and [status.polymarket.com](https://status.polymarket.com) for the exact start time and live updates.

### After the upgrade

- **Open orders from before the upgrade will have been cleared.** You'll need to re-place them.

- **You'll see a one-time approval prompt** the first time you trade to convert your balance to pUSD and approve the new exchange contracts.

- The migration itself is a one-time action.

### Security

The CTFv2 contracts have been audited by **Cantina** and **Quantstamp**. The smart contracts are open sourced, and a bug bounty program with Cantina is live [here](https://cantina.xyz/bounties/ff945ca2-2a6e-4b83-b1b6-7a0cd3b94bea).

---

**## FAQ - Frequently Asked Questions**

#### Will I lose access to my funds?

No. Your balance and positions stay with you. During the 1-hour maintenance window you won't be able to trade, but nothing is moving anywhere. When you come back, you'll sign a one-time prompt to convert your USDC balance into pUSD (1:1, no fee).

#### When can I trade again?

April 28, around 12:00 PM UTC. Trading pauses at 11:00 AM UTC for the upgrade and reopens roughly an hour later. This will be shown on the site, as well as broadcasted on [Discord](https://discord.com/channels/710897173927297116/1495823545605951719) and X.
​

#### What happens to my open limit orders?

They get cancelled. The v1 orderbook gets cleared as part of the upgrade. After you log back in, you'll see exactly which of your orders were cancelled, and you can re-place anything you still want resting on the book. If you're a market maker, it's worth pulling your orders before 7am UTC so nothing gets caught mid-cancel.

#### Do I have to do anything?

For most people, one click. The first time you load Polymarket after the upgrade, you'll see a guided flow asking you to:

1. Sign to convert your USDC balance to pUSD, and approve the new exchange contracts.

#### What is pUSD?

pUSD is the new collateral token for Polymarket. It's a regular ERC-20 on Polygon, backed 1:1 by USDC. The smart contract enforces the 1:1 backing, so one pUSD always converts back to one USDC. Deposit and withdrawal may have conversion/routing costs.
​

#### What happens to my existing positions?

Positions in active markets carry over and resolve normally.
​

#### Are fees changing?

Fees are now charged in USDC instead of in shares, which makes them readable on your trade history rather than baked into share counts. The fee is calculated when your order matches, not when you place it.
​

#### What if I'm trading via the API?

You'll need to switch to the v2 endpoints and update your client library. Migration guide: <https://docs.polymarket.com/v2-migration>. The v1 client libraries won't work with the new contracts.
​

#### What if something looks wrong on Tuesday?

If anything looks off (missing balance, signature stuck, anything that looks off), reach out to Support through our chat or [email](mailto:support@polymarket.com) and the team will route it.

#### Video walk through

Guide for converting your USDC.e to pUSD [here](https://www.loom.com/share/3a53c868229046d19a115b874251a6d8)