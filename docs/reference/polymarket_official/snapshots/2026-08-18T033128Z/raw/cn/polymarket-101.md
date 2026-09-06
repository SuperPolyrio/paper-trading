> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Polymarket 101

> Polymarket 简介——全球最大的预测市场

Polymarket 是一个预测市场平台，用户可以在这里交易现实世界事件的结果。你并非与庄家对赌，而是在开放的点对点市场中与其他用户交易份额。价格反映了市场对某一事件发生概率的集体判断。

平台采用非托管模式，你始终掌控自己的资金。所有交易通过区块链上的智能合约结算，确保操作透明且无需信任第三方。

## 自托管

Polymarket 采用非托管模式，你始终对自己的资金拥有完全控制权。

* 资产存放在你的钱包中，由你的私钥保护。
* 交易通过经过审计的智能合约自动执行。
* Polymarket 永远不会占有你的资金。
* 所有交易和持仓均记录在链上，可公开验证。
* 结算根据市场判定结果自动完成。

<Warning>
  请妥善保管你的私钥，切勿与任何人分享。如果丢失私钥，你将无法访问资金。如果你是通过
  Magic Link 注册的或拥有代理钱包，可以通过
  [recovery.polymarket.com](https://recovery.polymarket.com) 尝试恢复。
</Warning>

## Polymarket 运作机制

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/polymarket-101.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=059e9831d1c51b99996d9747c0139d49" alt="Polymarket Overview" className="dark:hidden" width="1526" height="952" data-path="images/core-concepts/polymarket-101.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/polymarket-101.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=4e929eca98a2bb83ef7421f7bbaf9f1d" alt="Polymarket Overview" className="hidden dark:block" width="1526" height="952" data-path="images/dark/core-concepts/polymarket-101.png" />
</Frame>

### 价格即概率

Polymarket 上每个份额的价格在 `$0.00` 到 `$1.00` 之间。价格代表市场对该结果发生概率的判断。

例如，如果某事件的 "Yes" 份额交易价格为 `$0.65`，意味着市场认为该事件发生的概率约为 `65%`。

### 抵押品与代币

Polymarket 使用 pUSD（Polymarket USD）作为抵押品。每一对 Yes/No 份额都有完全的资金支撑：

* `$1 pUSD` 可铸造一份 Yes 份额和一份 No 份额
* 获胜份额可兑换 `$1.00`
* 失败份额价值 `$0.00`

份额以代币形式表示，基于 [Gnosis Conditional Token Framework](https://github.com/gnosis/conditional-tokens-contracts/)（ERC1155 标准），实现无缝的链上交易与结算。

### 交易

Polymarket 使用点对点订单簿（CLOB）进行交易。你直接与其他用户交易，而非与庄家对赌。

* **买入份额** — 当你认为市场低估了某一结果的概率
* **卖出份额** — 当你认为市场高估了某一结果的概率
* **随时退出** — 在判定结果公布前卖出持仓，锁定利润或止损

| 操作     | 适用场景        | 盈利条件      |
| ------ | ----------- | --------- |
| 买入 Yes | 你认为概率被市场低估了 | 事件发生      |
| 买入 No  | 你认为概率被市场高估了 | 事件未发生     |
| 卖出     | 锁定收益或控制亏损   | 价格朝有利方向变动 |

### 判定

当事件结束后，市场通过 **UMA Optimistic Oracle** 进行判定：

1. 提议者提交结果并缴纳保证金
2. 进入争议期，任何人都可以提出异议
3. 如有争议，UMA 代币持有者投票决定正确的判定结果
4. 获胜代币可兑换 \$1 pUSD

这一社区驱动的流程确保了市场判定的公正性和准确性。

## 为什么选择区块链

Polymarket 构建在 **Polygon** 区块链网络上，主要基于以下原因：

* **全球可访问** — 任何有互联网连接的人都可以参与
* **非托管** — 资金由你掌控，而非中心化机构
* **透明** — 所有活动均可在链上公开验证
* **快速且低成本** — Polygon 提供快速、低费用的交易体验
* **价值稳定** — pUSD 是由 USDC 背书的标准 ERC-20 代币，背书由智能合约在链上强制执行 — 避免加密货币的价格波动

## 智能钱包

Polymarket 使用智能钱包，使用户无需手动提交每一笔链上交易即可交易。新的 API 用户使用存款钱包；现有 Safe 和 Proxy 用户可以继续使用当前钱包。

存款钱包在 Polygon 上持有用户的 pUSD 和结果代币，并通过 ERC-1271 验证订单。现有用户和集成仍可使用 Safe 和 Proxy 钱包。

智能钱包让 Polymarket 能够提供更好的用户体验：多步骤交易可以原子化执行，交易也可以由 Polymarket 的中继器转发。如果你是希望以编程方式访问现有 Polymarket 账户持仓的开发者，请继续使用该账户当前的智能钱包类型。

### 部署地址

每位智能钱包用户都有自己的钱包地址。有关 Polygon 上所有已部署的工厂和交易合约地址，请参阅[合约](/cn/resources/contracts)。

***

## 开始使用

准备好开始交易了吗？

<CardGroup cols={2}>
  <Card title="交易快速入门" icon="rocket" href="/cn/trading/quickstart">
    设置账户并完成第一笔交易。
  </Card>

  <Card title="浏览市场" icon="chart-line" href="https://polymarket.com">
    在 Polymarket 上浏览活跃的预测市场。
  </Card>
</CardGroup>
