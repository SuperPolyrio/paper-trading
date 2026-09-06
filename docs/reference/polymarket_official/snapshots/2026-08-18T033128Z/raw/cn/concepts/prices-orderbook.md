> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 价格与订单簿

> 价格机制以及订单簿如何实现点对点交易

Polymarket 使用\*\*中央限价订单簿（CLOB）\*\*进行交易。价格不是由 Polymarket 设定的，而是随着用户之间的交易，由供需关系自然形成。

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/orderbook.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=119174bcaaeb3b9abbd4c2d94b7bdae6" alt="" className="dark:hidden" width="1540" height="952" data-path="images/core-concepts/orderbook.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/orderbook.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=b940f4b5f28ab6ed5845dda2bfe03edb" alt="" className="hidden dark:block" width="1540" height="952" data-path="images/dark/core-concepts/orderbook.png" />
</Frame>

## 价格即概率

Polymarket 上每个份额的价格在 `$0.00` 到 `$1.00` 之间。价格直接反映市场对该结果发生概率的判断。

| 价格     | 隐含概率 |
| ------ | ---- |
| \$0.25 | 25%  |
| \$0.50 | 50%  |
| \$0.75 | 75%  |

<Note>
  显示的价格是买卖价差的**中间价**。如果价差超过 \$0.10，则显示最近成交价。
</Note>

### 示例

如果 "Yes" 的最优买价为 `$0.34`，最优卖价为 `$0.40`：

```
Displayed price = ($0.34 + $0.40) / 2 = $0.37 (37% probability)
```

你不一定能以 `$0.37` 成交——买入时你将支付卖价（`$0.40`），卖出时你将获得买价（`$0.34`）。

## 订单簿

订单簿是某一市场中所有未成交买单和卖单的列表，分为两侧：

| 侧  | 说明           |
| -- | ------------ |
| 买单 | 交易者愿意支付的最高价格 |
| 卖单 | 交易者愿意接受的最低价格 |

**价差**是最高买价与最低卖价之间的差距。价差越小，市场流动性越好。

## 订单类型

### 市价单

以当前最优价格立即成交。适合希望即时执行、愿意承担价差成本的场景。

* **买入**：按最低卖价成交
* **卖出**：按最高买价成交

### 限价单

仅以你指定的价格或更优价格成交。适合追求价格控制、愿意等待的场景。

* 你的订单挂在订单簿上，等待对手方成交
* 订单可以**部分成交**——不同交易者匹配你订单的不同部分
* 你可以随时取消未成交的订单

<Note>
  Polymarket
  上所有订单本质上都是限价单。"市价单"只是设定了一个能立即与挂单成交的价格的限价单。
</Note>

## 交易流程

Polymarket 的 CLOB 采用**混合去中心化**架构：

1. **链下撮合** — 运营方撮合匹配的订单
2. **链上结算** — 成交的交易通过智能合约结算

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/trade-lifecycle.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=2acec8befdfbba57fb554170f7d5813c" alt="" className="dark:hidden" width="1540" height="952" data-path="images/core-concepts/trade-lifecycle.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/trade-lifecycle.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=d18b22ad7629820ad554dda8cb83ec18" alt="" className="hidden dark:block" width="1540" height="952" data-path="images/dark/core-concepts/trade-lifecycle.png" />
</Frame>

这种设计兼具中心化撮合的速度和链上结算的安全性。你始终保持对资金的控制权。

## 价格发现

新市场上线时没有初始价格。第一个价格的产生过程如下：

1. 有人挂限价单以某个价格买入 Yes（例如 `$0.60`）
2. 有人挂限价单以互补价格买入 No（例如 `$0.40`）
3. 由于 `$0.60` + `$0.40` = `$1.00`，订单得以匹配

匹配成功后，`$1.00` 被转换为 1 个 Yes 代币和 1 个 No 代币，分别分配给各自的买方。

## 下一步

<Note>
  Polymarket
  的订单簿**没有交易数量限制**——它撮合任意金额的买卖双方。但大额订单可能会显著影响价格。大笔交易前请务必查看订单簿深度。
</Note>

<CardGroup cols={2}>
  <Card title="持仓与代币" icon="coins" href="/cn/concepts/positions-tokens">
    了解结果代币以及持仓的运作方式。
  </Card>

  <Card title="订单生命周期" icon="arrows-spin" href="/cn/concepts/order-lifecycle">
    了解从下单到结算的完整流程。
  </Card>
</CardGroup>
