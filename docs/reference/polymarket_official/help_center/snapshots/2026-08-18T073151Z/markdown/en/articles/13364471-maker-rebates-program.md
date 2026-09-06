# Maker Rebates Program

Polymarket has enabled taker fees across multiple market categories. These fees fund a **Maker Rebates** program that pays daily USDC rebates to liquidity providers.

---

## **​Why Maker Rebates?**

Deeper liquidity means tighter spreads, lower price impact, more reliable fills, and greater resilience during volatility. Maker Rebates **incentivize consistent, competitive quoting** so everyone gets a better trading experience.

---

## **​How Maker Rebates Work**

- **Paid daily in USDC:** Rebates are calculated and distributed every day.

- **Performance-based:** You earn based on the share of liquidity you provided that actually got taken.

### **Eligibility**

Place orders that add liquidity to the book and get filled (i.e., your liquidity is taken by another trader).

### **Payment**

Rebates are paid daily in USDC, directly to your wallet. A minimum accrued rebate of **$1 USDC** is required for a payout.

---

## **​Funding**

Maker Rebates are funded by taker fees collected in **eligible markets**. A percentage of these fees are redistributed to makers who keep the markets liquid. The rebate percentage differs by market type.

| **Category**    | **Maker Rebate** | **Distribution Method** |
| --------------- | ---------------- | ----------------------- |
| Crypto          | 20%              | Fee-curve weighted      |
| Sports          | 20%              | Fee-curve weighted      |
| Finance         | 25%              | Fee-curve weighted      |
| Politics        | 25%              | Fee-curve weighted      |
| Economics       | 25%              | Fee-curve weighted      |
| Culture         | 25%              | Fee-curve weighted      |
| Weather         | 25%              | Fee-curve weighted      |
| Other / General | 25%              | Fee-curve weighted      |
| Mentions        | 25%              | Fee-curve weighted      |
| Tech            | 25%              | Fee-curve weighted      |
| Geopolitics     | —                | Fee-free                |

Polymarket collects taker fees in **eligible markets** across **all fee-enabled categories**. The rebate percentage is at the sole discretion of Polymarket and may change over time.

---

## **Fee-Curve Weighted Rebates**

Rebates are distributed using the **same formula as taker fees**. This ensures makers are rewarded proportionally to the fee value their liquidity generates.

### **For each filled maker order:**

```
fee_equivalent = C × feeRate × p × (1 - p)
```

Where **C** = number of shares traded and **p** = price of the shares.

### The fee parameters differ by market type:**​**

| **Category**    | **Taker Fee Rate** | **Maker Fee Rate** |
| --------------- | ------------------ | ------------------ |
| Crypto          | 0.07               | 0                  |
| Sports          | 0.05               | 0                  |
| Finance         | 0.04               | 0                  |
| Politics        | 0.04               | 0                  |
| Economics       | 0.05               | 0                  |
| Culture         | 0.05               | 0                  |
| Weather         | 0.05               | 0                  |
| Other / General | 0.05               | 0                  |
| Mentions        | 0.04               | 0                  |
| Tech            | 0.04               | 0                  |
| Geopolitics     | 0                  | 0                  |

### **Your daily rebate:**

```
rebate = (your_fee_equivalent / total_fee_equivalent) * rebate_pool
```

Totals are calculated per market, so you only compete with other makers in the same market.

## **​**

---

## **Taker Fee Structure**

Taker fees are calculated in USDC and vary based on the share price. However, fees are collected in shares on buy orders and USDC on sell orders. The fee amount in USDC is symmetric around 50% probability — a trade at 30¢ incurs the same dollar fee as a trade at 70¢.

![](https://downloads.intercomcdn.com/i/o/zryw7npl/2275448700/e7dc5c5fdae14e2560f27d179a60/taker-fee-in-usdc-by-share-price-for-100-shares-nbsp-.png?expires=1787040000&amp;signature=3b73be2cb788e7e52945abe4fee20c68910396080fb8e8ce0e7ad6384cf32205&amp;req=diIgE816lYZfWfMW1HO4zW9gYj1IUMflEf%2FiA3bYlT7TYLeHoG0tiZ1sIl4E%0A5GWVX%2FvXLFI9vJbUmT4%3D%0A)

### **Fee Tables (100 Shares)**

For detailed fee tables for each market category, see the **[Fees](https://docs.polymarket.com/trading/fees)** page.

### **Fee Precision**

Fees are rounded to 5 decimal places. The smallest fee charged is 0.00001 USDC. Anything smaller rounds to zero, **so very small trades near the extremes may incur no fee at all.**

---

## **Which Markets Are Eligible**

The following market categories have taker fees enabled and are eligible for maker rebates: Crypto, Sports, Finance, Politics, Economics, Culture, Weather, Tech, Mentions, and Other / General.

Fees apply only to markets deployed on or after the activation date. Pre-existing markets are unaffected. Markets with fees enabled have **`feesEnabled`** set to **`true`** on the market object.

---

## **​FAQ**

**How do I qualify for maker rebates?**

Place orders that add liquidity to the book and get filled (i.e., your liquidity is taken by another trader).

**When are rebates paid?**

Daily, in USDC. **You must accrue at least $1 in rebates before a payout is issued.**

**How are rebates calculated?**

Rebates are proportional to your share of executed maker liquidity in each eligible market. Totals are calculated per market, so you only compete with other makers in the same market.

**Where does the rebate pool come from?**

Taker fees collected in eligible markets are allocated to the maker rebate pool and distributed daily.

**Which markets have fees enabled?**

Crypto, Sports, Finance, Politics, Economics, Culture, Weather, Tech, Mentions, and Other / General markets. Fees only apply to markets deployed on or after the activation date.

**Is Polymarket charging fees on all markets?**

Fees apply to markets in fee-enabled categories. Markets with fees enabled have **`feesEnabled`** set to **`true`** on the market object — check it per-market via **`getClobMarketInfo(conditionID)`.**