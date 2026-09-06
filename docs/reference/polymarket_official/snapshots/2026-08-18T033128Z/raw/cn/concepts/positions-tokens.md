> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 持仓与代币

> 了解 Polymarket 上的结果代币和持仓机制

Polymarket 上的每个预测都由**结果代币**表示。当你交易时，你实际上在买卖这些代币。你的**持仓**就是你在某个市场中持有的代币余额。

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/token-lifecycle.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=cad279109c43c68c541123c2d348c4c5" alt="" className="dark:hidden" width="1596" height="952" data-path="images/core-concepts/token-lifecycle.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/token-lifecycle.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=204d16c1d89892a3c8573060aa04780e" alt="" className="hidden dark:block" width="1596" height="952" data-path="images/dark/core-concepts/token-lifecycle.png" />
</Frame>

## 结果代币

每个市场有且仅有两种结果代币：

| 代币      | 可兑换金额  | 条件    |
| ------- | ------ | ----- |
| **Yes** | \$1.00 | 事件发生  |
| **No**  | \$1.00 | 事件未发生 |

代币是 Polygon 上的 **ERC1155** 资产，基于 [Gnosis Conditional Token Framework](https://github.com/gnosis/conditional-tokens-contracts/)（CTF）。它们完全在链上运行，是标准的 ERC1155 代币。

<Note>
  结果代币始终有完全的资金支撑。每一对已发行的 Yes/No 代币都有恰好 `$1` 的 pUSD
  抵押品锁定在 CTF 合约中。
</Note>

### 拆分 - Split

将 pUSD 转换为结果代币。拆分 \$1 可生成 1 个 Yes 代币和 1 个 No 代币。

```
$100 pUSD → 100 Yes tokens + 100 No tokens
```

适用于：

* 为做市创建库存
* 同时获取市场的两侧头寸

### 交易 - Trade

在订单簿上买卖代币。这是大多数用户获取持仓的方式。

* 以 `$0.60` **买入 Yes** → 支付 `$0.60`，获得 1 个 Yes 代币
* 以 `$0.60` **卖出 Yes** → 交出 1 个 Yes 代币，获得 `$0.60`

你可以在判定结果公布前随时卖出持仓。

### 合并 - Merge

将一组完整的代币转换回 pUSD。合并需要等量的 Yes 和 No 代币。

```
100 Yes tokens + 100 No tokens → $100 pUSD
```

适用于：

* 不通过交易退出持仓
* 将积累的代币转换回抵押品

### 兑换 - Redeem

市场判定结束后，将获胜代币兑换为 pUSD。

| 结果    | Yes 代币   | No 代币    |
| ----- | -------- | -------- |
| 事件发生  | 每个价值 \$1 | 价值 \$0   |
| 事件未发生 | 价值 \$0   | 每个价值 \$1 |

```
100 winning tokens → $100 pUSD
```

### 持仓价值

你的持仓价值取决于当前市场价格：

```
Position value = Token balance × Current price
```

假设你持有 100 个 Yes 代币，Yes 当前交易价为 \$0.75：

```
Position value = 100 × $0.75 = $75
```

## 盈亏

你的利润取决于市场判定结果与你的入场价格之间的差异。

### 示例 - 以 0.40 买入 Yes

| 场景    | 结果     | 回报     | 盈亏                  |
| ----- | ------ | ------ | ------------------- |
| 事件发生  | Yes 获胜 | \$1.00 | 每个代币 +\$0.60（150%）  |
| 事件未发生 | No 获胜  | \$0.00 | 每个代币 -\$0.40（-100%） |

### 持仓奖励

Polymarket 根据你在符合条件的市场中的总持仓价值，支付**年化 4.00%** 的持仓奖励。系统每小时随机采样一次你的持仓价值，奖励按天发放。利率为浮动利率，Polymarket 保留随时调整的权利。

### 示例 - 判定前卖出

你可以在市场判定结果公布前卖出，锁定利润或止损：

* 以 `$0.40` 买入 Yes
* 价格涨到 `$0.70`
* 以 `$0.70` 卖出 → 每个代币盈利 `$0.30`（75%）
