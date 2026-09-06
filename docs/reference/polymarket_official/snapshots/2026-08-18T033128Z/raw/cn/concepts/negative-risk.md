> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 负风险市场

> 多结果事件的资本高效交易机制

\*\*负风险（Negative risk）**是一种用于多结果事件的机制，其中只有一个结果能够胜出。它通过**转换（conversion）\*\*操作关联同一事件中的所有结果持仓，从而提高资本效率。

## 运作方式

在标准多结果事件中，每个市场彼此独立。如果你想押注某个结果不会发生，就必须买入该结果的 No 代币，而这些 No 代币与其他结果没有关系。

负风险改变了这一点。在负风险事件中：

* 任一市场的 **1 份 No 份额**可以转换为**其他每个市场各 1 份 Yes 份额**
* 该转换通过 [Neg Risk Adapter 合约](https://github.com/Polymarket/neg-risk-ctf-adapter)完成

### 示例

假设有一个事件：“谁将赢得 2024 年美国总统大选？”，包含三个结果：

| 结果     | 你的持仓 |
| ------ | ---- |
| Trump  | —    |
| Harris | —    |
| Other  | 1 No |

借助负风险，“Other”的 1 份 No 可以转换为：

| 结果     | 转换后的持仓 |
| ------ | ------ |
| Trump  | 1 Yes  |
| Harris | 1 Yes  |
| Other  | —      |

这种机制提高了资本效率，因为押注一个结果不会发生，在经济上等同于押注所有其他结果会发生。

## 合约地址

负风险市场使用与标准市场不同的合约：

有关 Neg Risk Adapter 和 Neg Risk CTF Exchange 的地址，请参阅[合约](/cn/resources/contracts)。

## 增强型负风险

标准负风险要求在创建市场时已知全部结果。但有时交易开始后会出现新结果，例如新的候选人加入竞选。

\*\*增强型负风险（Augmented negative risk）\*\*通过以下结果类型解决这一问题：

| 结果类型          | 说明                         |
| ------------- | -------------------------- |
| **已命名结果**     | 已知结果（例如 “Trump”、“Harris”）  |
| **占位结果**      | 可在之后明确的保留位置（例如 “Person A”） |
| **明确的 Other** | 包含所有未明确命名的结果               |

### 占位结果的运作方式

1. 事件上线时包含已命名结果、占位结果和 “Other”
2. 出现新结果时，通过公告板明确一个占位结果
3. 随着占位结果被分配，“Other”的定义逐渐缩小

### 增强型负风险的交易规则

<Warning>
  只交易**已命名结果**。在占位结果被命名或市场进入判定前，应忽略这些结果。Polymarket
  UI 不会显示未命名结果。
</Warning>

* 如果判定时的正确结果尚未命名，市场将判定为 “Other”
* 随着占位结果被明确，“Other”的定义会发生变化，因此应避免直接交易它

## 技术细节

### 转换机制

转换操作是原子操作，通过 Neg Risk Adapter 完成：

1. 你持有结果 A 的 1 份 No 代币
2. 调用适配器上的 convert 函数
3. 你会收到事件中其他每个结果各 1 份 Yes 代币

## 后续步骤

<CardGroup cols={2}>
  <Card title="市场与事件" icon="calendar" href="/cn/concepts/markets-events">
    了解多市场事件的结构。
  </Card>

  <Card title="持仓与代币" icon="coins" href="/cn/concepts/positions-tokens">
    了解拆分、合并和赎回等代币操作。
  </Card>
</CardGroup>
