# Trading Fees

Polymarket charges a small taker fee on certain markets. Fees are set by the protocol and applied at match time — you don’t include fee information in your orders. These fees fund the **[Maker Rebates Program](https://docs.polymarket.com/market-makers/maker-rebates)**, which redistributes fees daily to market makers to incentivize deeper liquidity and tighter spreads. Takers can also earn a portion of fees back through the tiered **[Taker Rebate Program](https://docs.polymarket.com/trading/taker-rebates)**.

**Geopolitical and world events markets are fee-free.** Polymarket does not charge fees or profit from trading activity on these markets. There are also no Polymarket fees to deposit or withdraw USDC (though intermediaries like Coinbase or MoonPay may charge their own fees).

## **Fee Structure**

Fees are calculated using the following formula:

```
fee = C × feeRate × p × (1 - p)
```

Where **C** = number of shares traded and **p** = price of the shares.

**Makers are never charged fees.** Only takers pay fees. The fee parameters differ by market category:

| **Category**    | **Taker Fee Rate** | **Maker Fee Rate** | **Maker Rebate** |
| --------------- | ------------------ | ------------------ | ---------------- |
| Crypto          | 0.07               | 0                  | 20%              |
| Sports          | 0.05               | 0                  | 15%              |
| Finance         | 0.04               | 0                  | 25%              |
| Politics        | 0.04               | 0                  | 25%              |
| Economics       | 0.05               | 0                  | 25%              |
| Culture         | 0.05               | 0                  | 25%              |
| Weather         | 0.05               | 0                  | 25%              |
| Other / General | 0.05               | 0                  | 25%              |
| Mentions        | 0.04               | 0                  | 25%              |
| Tech            | 0.04               | 0                  | 25%              |
| Geopolitics     | 0                  | 0                  | —                |

Taker fees are calculated in USDC and vary based on the share price. The fee amount in USDC is symmetric around 50% probability — a trade at 30¢ incurs the same dollar fee as a trade at 70¢.

## **Fee Precision**

Fees are rounded to 5 decimal places. The smallest fee charged is**0.00001 USDC**. Anything smaller rounds to zero, so very small trades near the extremes may incur no fee at all.