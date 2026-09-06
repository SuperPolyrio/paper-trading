> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Builder 费用

> 了解 Builder 如何从通过其应用路由的订单中赚取费用，以及如何完成集成。

CLOB V2 引入了一个费用层，让 Builder 可以从通过其应用路由的每个订单中赚取费用。当 Builder 为订单附加其唯一的 **Builder Code** 且订单成交时，系统会在收取平台费用的同时收取 **Builder 费用**。

Builder 费用是交易名义价值的固定百分比，由各 Builder 在规定的限制内配置。它属于附加费用：会叠加在平台费用之上，而不会取代平台费用。

<Note>
  第一次使用 Builder 计划？请先阅读 [Builder
  计划](/cn/programs/builders/overview)。本页专门介绍费用层。
</Note>

***

## 工作原理

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/lWwKl4XdsXxYugaA/images/core-concepts/builder-fee.png?fit=max&auto=format&n=lWwKl4XdsXxYugaA&q=85&s=9287dc95f24f07bcb9f33c4d7d6ed0f2" alt="" className="dark:hidden" width="2068" height="952" data-path="images/core-concepts/builder-fee.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/lWwKl4XdsXxYugaA/images/dark/core-concepts/builder-fee.png?fit=max&auto=format&n=lWwKl4XdsXxYugaA&q=85&s=31b5e9be53b16738ad9f2b833b1eb02a" alt="" className="hidden dark:block" width="2068" height="952" data-path="images/dark/core-concepts/builder-fee.png" />
</Frame>

Builder 费用与平台费用相互独立。用户实际支付的费用取决于市场配置，以及订单是否附有 Builder Code：

| 市场      | 附有 Builder Code | 用户支付              |
| ------- | --------------- | ----------------- |
| 无平台费用   | 否               | 无费用               |
| 无平台费用   | 是               | 仅 Builder 费用      |
| 已启用平台费用 | 否               | 仅平台费用             |
| 已启用平台费用 | 是               | 平台费用 + Builder 费用 |

Builder 费用绝不会取代平台费用，而是始终叠加收取。

<Warning>
  Polymarket 保留自行决定撤销你收取 Builder
  费用权限的权利，无论是否说明理由，包括但不限于费用被认定为通过欺诈、欺骗、误导、自动化、自我推荐或其他非真实交易活动收取的情况。
</Warning>

***

## 注册

通过你的 Polymarket 账户注册 Builder Code。

<Steps>
  <Step title="创建 Builder 资料">
    打开 polymarket.com → Settings →
    [Builders](https://polymarket.com/settings?tab=builder)，然后设置你的
    Builder 资料。
  </Step>

  <Step title="设置费率">
    在资料中配置两种费率： - **Taker Fee Rate** — 对通过你的应用路由的 Taker
    订单收取 - **Maker Fee Rate** — 对通过你的应用路由的 Maker 订单收取
  </Step>

  <Step title="复制 Builder Code">
    系统会为你的资料分配一个 `bytes32` Builder Code。[将其附加到你提交的每个订单
    上](/cn/trading/place-orders#构建者归因)。
  </Step>
</Steps>

### 费率限制

| 参数       | 默认值        | 最大值           |
| -------- | ---------- | ------------- |
| Taker 费率 | 0 bps (0%) | 100 bps (1%)  |
| Maker 费率 | 0 bps (0%) | 50 bps (0.5%) |
| 最小调整幅度   | —          | 1 bp (0.01%)  |

### 费率变更政策

费率变更受到以下限制，以便用户提前知晓：

* \*\*冷却期。\*\*每 7 天只能变更一次费率。
* \*\*提前通知。\*\*变更在安排后的 3 天生效。
* \*\*一次只能有一项待处理变更。\*\*不能将多项变更排队；必须等待当前变更生效（或将其取消），才能安排下一项。

***

## 费用计算

### 平台费用

平台费用使用按市场动态计算的公式：

```
platform_fee = C × feeRate × p × (1 - p)
```

其中，`C` 是交易规模，`p` 是订单价格，`feeRate` 是按市场设置的参数。目前仅向 Taker 收取平台费用，Builder 无法配置该费用。

### Builder 费用

Builder 费用是名义价值的固定百分比：

```
builder_fee = notional × builder_fee_rate_bps / 10000
```

\*\*示例。\*\*一笔通过某 Builder 路由的 1,000 pUSD Taker 买单，该 Builder 的 Taker 费率为 100 bps (1%)：

```
builder_fee = 1000 × 100 / 10000 = 10 pUSD
```

一笔交易的 Maker 和 Taker 两侧可以使用不同的 Builder Code 和不同的费率。如果 Builder A（Maker 费率 0.3%）提交挂单，而 Builder B（Taker 费率 0.8%）提交与之撮合的订单，则双方分别从各自一侧赚取相应费用。

### 余额检查

账户必须有足够的 pUSD 支付交易以及所有适用的平台费用和 Builder 费用。对于市价买单，请[设置包含费用的支出上限](/cn/trading/place-orders#限制市价买单支出)，以便在签名前根据费用调整订单金额。

***

## 链上归因

Builder 归因是已签名 V2 订单结构的一部分，而不是链下标签。CTF Exchange V2 合约发出的每个 `OrderFilled` 事件都包含 `builder` 字段。

### V2 订单结构

```
salt, maker, signer, tokenId, makerAmount, takerAmount,
side, signatureType, timestamp, metadata, builder
```

`builder` 字段是一个与你已注册 Builder Code 匹配的 `bytes32` 值。

***

## 费用处理和支付

当用户下单并附加你的 `builderCode` 时：

1. CLOB 验证订单和 Builder Code。
2. 撮合时，Fees Service 分别计算交易两侧的平台费用和 Builder 费用。
3. 交易通过 `CTFExchangeV2.matchOrders()` 在链上结算，并发出 `OrderFilled` 事件。
4. Builders Service 索引这些事件，将链上归因与你的 Builder 资料关联，并累计你赚取的费用。

收取的 Builder 费用会分发到与你的 Builder 资料关联的钱包。

***

## 计划政策

### 已停用的 Code

Polymarket 可随时因违反 Builder 计划条款、滥用费率或平台完整性问题而停用 Builder Code。携带已停用 Code 的订单会被 CLOB 拒绝。

### 公开可见性

Builder 资料和费率可供公开查询。这一设计让用户和第三方可以在使用 Builder 应用前了解其收费标准。
