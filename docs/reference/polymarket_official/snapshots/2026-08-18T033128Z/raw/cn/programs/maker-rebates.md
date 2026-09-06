> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Maker 返利计划

> 通过在 Polymarket 提供流动性赚取每日 pUSD 返利

Polymarket 在多个市场类别收取 taker 费用。费用由协议在撮合时确定,并用于资助 **Maker 返利**计划,向流动性提供者支付每日 pUSD 返利。

***

## 为什么要有 Maker 返利

更深的流动性意味着更紧的价差、更低的价格影响、更可靠的成交以及在波动期间更强的韧性。Maker 返利激励**持续的、有竞争力的报价**，让每个人都能获得更好的交易体验。

***

## Maker 返利如何运作

* **每日以 pUSD 支付:** 返利每天计算和分配。
* **基于表现:** 你根据实际被成交的流动性份额获得收益。

### 资格

下达为订单簿增加流动性并被成交的订单(即,你的流动性被其他交易者成交)。

### 支付

返利每日以 pUSD 支付,直接到你的钱包。

***

## 资金来源

Maker 返利由符合条件的市场中收取的 taker 费用资助。这些费用的一定百分比重新分配给保持市场流动性的 maker。返利百分比因市场类型而异。

| 类别       | Maker 返利 | 分配方式   |
| -------- | -------- | ------ |
| 加密货币     | 20%      | 费用曲线加权 |
| 体育       | 15%      | 费用曲线加权 |
| 金融       | 25%      | 费用曲线加权 |
| 政治       | 25%      | 费用曲线加权 |
| 经济       | 25%      | 费用曲线加权 |
| 文化       | 25%      | 费用曲线加权 |
| 天气       | 25%      | 费用曲线加权 |
| 其他 / 通用  | 25%      | 费用曲线加权 |
| Mentions | 25%      | 费用曲线加权 |
| 科技       | 25%      | 费用曲线加权 |
| 地缘政治     | —        | 免费     |

<Note>
  Polymarket 在所有启用费用的市场类别中收取 taker 费用。返利百分比由 Polymarket
  全权决定,可能会随时间变化。
</Note>

***

## 费用曲线加权返利

返利使用**与 taker 费用相同的公式**分配。这确保 maker 按其流动性产生的费用价值成比例获得奖励。

对于每个成交的 maker 订单:

```text theme={null}
fee_equivalent = C × feeRate × p × (1 - p)
```

其中 **C** = 交易的份额数量,**p** = 份额价格。费用参数因市场类型而异:

| 类别       | Taker Fee Rate | Maker Fee Rate |
| -------- | -------------- | -------------- |
| 加密货币     | 0.07           | 0              |
| 体育       | 0.05           | 0              |
| 金融       | 0.04           | 0              |
| 政治       | 0.04           | 0              |
| 经济       | 0.05           | 0              |
| 文化       | 0.05           | 0              |
| 天气       | 0.05           | 0              |
| 其他 / 通用  | 0.05           | 0              |
| Mentions | 0.04           | 0              |
| 科技       | 0.04           | 0              |
| 地缘政治     | 0              | 0              |

你的每日返利:

```text theme={null}
rebate = (your_fee_equivalent / total_fee_equivalent) * rebate_pool
```

总额按市场计算,所以你只与同一市场中的其他 maker 竞争。

***

## Taker 费用结构

Taker 费用以 pUSD 计算,并根据份额价格变化。费用金额（以 pUSD 计）关于 50% 概率对称 — 30¢ 的交易与 70¢ 的交易产生相同的美元费用。

<Frame>
  <div className="p-3 bg-white rounded-xl">
    <iframe title="Fee Curves" aria-label="Line chart" id="datawrapper-chart-dJ74e" src="https://datawrapper.dwcdn.net/dJ74e/" scrolling="no" frameborder="0" width={700} style={{ width: "0", minWidth: "100% !important", border: "none" }} height="450" data-external="1" />
  </div>
</Frame>

### 费用表 - 100 份额

有关每个市场类别的详细费用表,请参阅[费用](/cn/trading/fees)页面。

### 费用精度

费用四舍五入到小数点后 5 位。收取的最小费用为 0.00001 pUSD。任何更小的费用都会四舍五入为零,因此接近极端的非常小的交易可能根本不产生费用。

***

## 哪些市场符合条件

以下市场类别已启用 taker 费用,符合 maker 返利条件:加密货币、体育、金融、政治、经济、文化、天气、科技、Mentions 和其他 / 通用。

要确认某个市场是否收取费用，请参阅“市场详情”中的[交易费用](/cn/market-data/market-details#交易费用)。

***

## 常见问题

<AccordionGroup>
  <Accordion title="如何获得 maker 返利资格">
    下达为订单簿增加流动性并被成交的订单(即,你的流动性被其他交易者成交)。
  </Accordion>

  <Accordion title="返利何时支付">每日,以 pUSD 支付。</Accordion>

  <Accordion title="返利如何计算">
    返利与你在每个符合条件的市场中已执行的 maker
    流动性份额成正比。总额按市场计算,所以你只与同一市场中的其他 maker 竞争。
  </Accordion>

  <Accordion title="返利池从哪里来">
    在符合条件的市场中收取的 taker 费用被分配到 maker 返利池并每日分配。
  </Accordion>

  <Accordion title="哪些市场启用了费用">
    加密货币、体育、金融、政治、经济、文化、天气、科技、Mentions 和其他 /
    通用市场。
  </Accordion>
</AccordionGroup>
