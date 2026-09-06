> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 订单生命周期

> 了解订单从创建到结算的完整流程

Polymarket 上的每笔交易都遵循一个特定的生命周期。订单在链下创建，由运营方撮合，最终通过智能合约在链上结算。这种混合架构兼具中心化撮合的速度和区块链结算的安全性。

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/order-lifecycle.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=4db07008193421bfe359afe44b5f604e" alt="" className="dark:hidden" width="2336" height="952" data-path="images/core-concepts/order-lifecycle.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/order-lifecycle.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=5a0f3eba2f20c44471bae05c0670de4a" alt="" className="hidden dark:block" width="2336" height="952" data-path="images/dark/core-concepts/order-lifecycle.png" />
</Frame>

## 订单运作方式

Polymarket 上所有订单都是**限价单**。限价单指定你愿意支付（或接受）的价格和交易数量。

<Note>"市价单"本质上是一种价格设定为可立即与最优挂单成交的限价单。</Note>

订单是 **EIP712 签名消息**。下单时，你用私钥签署一个结构化消息。这个签名授权 Exchange 合约代你执行交易——而无需接管你的资金。

## 订单类型

| 类型      | 行为                               | 适用场景   |
| ------- | -------------------------------- | ------ |
| **GTC** | Good Till Cancelled — 挂单直到成交或被取消 | 标准限价单  |
| **GTD** | Good Till Date — 到指定时间自动过期       | 有时效的订单 |
| **FOK** | Fill Or Kill — 全部成交或立即取消         | 要求全额成交 |
| **FAK** | Fill And Kill — 成交可成交的部分，取消剩余    | 接受部分成交 |

### Post-Only 订单

Post-Only 订单只会作为挂单存在。如果 Post-Only 订单会立即成交（穿越价差），则会被拒绝而非执行。这保证你始终是 maker，而非 taker。

<Steps>
  <Step title="创建与签名">
    你的客户端创建一个包含以下内容的订单对象：

    * Token ID（你要交易的结果）
    * 方向（买入或卖出）
    * 价格和数量
    * 过期时间
    * 时间戳（毫秒，用于订单唯一性）

    你用私钥对订单进行签名，生成 EIP712 签名。
  </Step>

  <Step title="提交至 CLOB">
    签名后的订单被提交到中央限价订单簿（CLOB）运营方。运营方会验证：

    * 签名有效性
    * 余额是否充足
    * 是否设置了必要的授权（allowance）
    * 价格是否满足最小价格单位要求
  </Step>

  <Step title="撮合或挂单">
    **如果订单可成交**（你的买价 ≥ 最低卖价，或你的卖价 ≤ 最高买价），则会与挂单撮合。部分市场会先应用短暂的 taker delay：

    * **Taker delay：** 用于部分加密货币和金融 up/down 市场。订单会先保留 250 ms，然后重新运行校验，再进行撮合或进入订单簿。API 会等待这段保留期结束，并返回最终下单结果。要检查某个具体市场，请调用公开 CLOB 接口 `GET https://clob.polymarket.com/clob-markets/{condition_id}` 或 SDK 方法 `getClobMarketInfo(conditionID)`，并查看是否为 `itode: true`。
    * **体育 / 比赛延迟：** 在围绕实时比赛条件配置了延迟的体育市场启用。订单会等待该市场配置的延迟窗口后再撮合。

    在任一延迟窗口内，订单都处于 pending 状态，不能取消。如果延迟结束后市场、余额、授权额度或风险校验失败，订单会被拒绝，不会撮合。

    **如果订单不可立即成交**，则挂在订单簿上等待对手方。订单将保持挂单状态直到：

    * 其他订单与之匹配
    * 你取消订单
    * 订单过期（仅限 GTD 订单）
  </Step>

  <Step title="结算">
    订单撮合后，运营方将交易提交到区块链。Exchange 合约会：

    * 验证双方签名
    * 将代币从卖方转给买方
    * 将 pUSD 从买方转给卖方

    结算是**原子性**的——要么整笔交易成功，要么什么都不发生。
  </Step>

  <Step title="确认">
    交易在 Polygon 上达成最终性。你的代币余额更新，交易记录出现在你的历史中。
  </Step>
</Steps>

## 订单状态

下单后，订单会进入以下状态之一：

| 状态          | 说明                                      |
| ----------- | --------------------------------------- |
| `live`      | 订单挂在订单簿上                                |
| `matched`   | 订单立即成交                                  |
| `delayed`   | 可成交订单进入异步延迟窗口，例如配置了 seconds-delay 的体育市场 |
| `unmatched` | 可成交订单在延迟期结束后未成交，被放入订单簿                  |

## 交易状态

撮合后，交易经历以下状态：

| 状态          | 是否终态 | 说明               |
| ----------- | ---- | ---------------- |
| `MATCHED`   | 否    | 已撮合，发送至执行器进行链上提交 |
| `MINED`     | 否    | 交易已被区块链打包        |
| `CONFIRMED` | 是    | 交易达成最终性，执行成功     |
| `RETRYING`  | 否    | 交易失败，正在重试        |
| `FAILED`    | 是    | 交易永久失败           |

## Maker 与 Taker

| 角色        | 说明        | 触发条件           |
| --------- | --------- | -------------- |
| **Maker** | 为订单簿提供流动性 | 你的订单挂单后被其他订单成交 |
| **Taker** | 从订单簿获取流动性 | 你的订单立即与挂单成交    |

价格改善始终有利于 taker。如果你挂买单出价 `$0.55`，与挂卖单价格 `$0.52` 成交，你实际支付 `$0.52`。

## 取消订单

你可以在订单被撮合之前通过 CLOB API 取消，但可成交订单处于 pending 延迟窗口时不能取消。

已部分成交的部分无法取消——只能取消未成交的部分。

## 下单前提条件

下单前请确保满足以下条件：

| 要求         | 说明                    |
| ---------- | --------------------- |
| **余额**     | 足够的 pUSD（买入时）或代币（卖出时） |
| **授权**     | 已授权 Exchange 合约使用你的资产 |
| **API 凭证** | 认证接口所需的有效 API 密钥      |

<Info>
  订单数量受你的可用余额限制，需扣除现有挂单占用的金额。

  $$
  \text{maxOrderSize} = \text{balance} - \sum(\text{openOrderSize} - \text{filledAmount})
  $$
</Info>

## 下一步

<CardGroup cols={2}>
  <Card title="判定" icon="gavel" href="/cn/concepts/resolution">
    了解市场如何判定以及获胜代币如何兑换。
  </Card>

  <Card title="交易指南" icon="book" href="/cn/trading/quickstart">
    按照分步指南开始下单交易。
  </Card>
</CardGroup>
