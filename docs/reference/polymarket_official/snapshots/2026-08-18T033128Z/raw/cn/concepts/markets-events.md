> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 市场与事件

> 了解 Polymarket 的基本构成要素

Polymarket 上的每个预测都围绕两个核心概念构建：**市场（Market）**和**事件（Event）**。理解它们之间的关系是在平台上进行开发的基础。

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/event-market.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=4c62bd08a405868307cdd6799b368ca5" alt="" className="dark:hidden" width="1540" height="952" data-path="images/core-concepts/event-market.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/event-market.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=2eb5c9b0f8a2afe52bc2e717b7b796a2" alt="" className="hidden dark:block" width="1540" height="952" data-path="images/dark/core-concepts/event-market.png" />
</Frame>

## 市场

**市场**是 Polymarket 上最基本的可交易单元。每个市场代表一个 Yes/No 二元结果的问题。

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/event.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=0c9a264aec9a22ce5a20c4cc7980806d" alt="" className="dark:hidden" width="1540" height="952" data-path="images/core-concepts/event.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/event.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=912e41bebfe8c1a43ef53b89685ca3d2" alt="" className="hidden dark:block" width="1540" height="952" data-path="images/dark/core-concepts/event.png" />
</Frame>

每个市场都有以下标识符：

| 标识符              | 说明                                            |
| ---------------- | --------------------------------------------- |
| **Condition ID** | 市场条件在 CTF 合约中的唯一标识符                           |
| **Question ID**  | 用于判定的市场问题哈希值                                  |
| **Token IDs**    | 在 CLOB 上进行交易的 ERC1155 代币 ID——一个对应 Yes，一个对应 No |

<Note>
  只有当 `enableOrderBook` 为 `true` 时，市场才可以通过 CLOB
  进行交易。部分市场可能存在于链上，但不支持订单簿交易。
</Note>

### 市场示例

一个简单的市场可能是：

> **"比特币能否在 2026 年 12 月前达到 \$150,000？"**

这会产生两种结果代币：

* **Yes 代币** — 如果比特币达到 $150k，可兑换 `$1\`
* **No 代币** — 如果比特币未达到 $150k，可兑换 `$1\`

## 事件

**事件**是将一个或多个相关市场组合在一起的容器。事件提供组织结构，并支持多结果预测。

### 单市场事件

当事件只包含一个市场时，形成一个简单的市场对。事件和市场实质上是等价的。

```
Event: Will Bitcoin reach $100,000 by December 2024?
└── Market: Will Bitcoin reach $100,000 by December 2024? (Yes/No)
```

### 多市场事件

当事件包含两个或更多市场时，会在一个事件下组合相关的二元问题。部分多市场事件表示互斥的结果。

Polymarket 会将这些互斥市场关联为一个[负风险组](/cn/concepts/negative-risk)。组内只会有一个市场判定为 Yes，其余市场都会判定为 No。

```
Event: Who will win the 2024 Presidential Election?
├── Market: Donald Trump? (Yes/No)
├── Market: Joe Biden? (Yes/No)
├── Market: Kamala Harris? (Yes/No)
└── Market: Other? (Yes/No)
```

## 市场标识

每个市场和事件都有一个唯一的 **slug**，出现在 Polymarket 的 URL 中：

```
https://polymarket.com/event/fed-decision-in-october
                              └── slug: fed-decision-in-october
```

你可以使用 slug 从 API 获取特定的市场或事件：

```bash theme={null}
# Fetch event by slug
curl "https://gamma-api.polymarket.com/events?slug=fed-decision-in-october"
```

## 体育市场

对于体育市场，未成交的限价单会在比赛开始时**自动取消**，在官方开赛时间清空订单簿。但比赛开始时间可能会变动——如果比赛提前开始，订单可能来不及清除。请在临近开赛时密切关注你的订单。

***

## 下一步

<CardGroup cols={2}>
  <Card title="价格与订单簿" icon="chart-line" href="/cn/concepts/prices-orderbook">
    了解价格如何形成以及订单簿的运作方式。
  </Card>

  <Card title="获取市场数据" icon="code" href="/cn/market-data/overview">
    开始通过 API 查询市场和事件。
  </Card>
</CardGroup>
