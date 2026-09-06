> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 流动性奖励

> 在 Polymarket 上提供流动性获得奖励

通过发布限价挂单，流动性提供者（maker）会自动获得参与 Polymarket 激励计划的资格。奖励每天在 UTC 午夜时分直接分发到 maker 地址。

该计划旨在：

* 促进所有市场的流动性
* 鼓励在市场整个生命周期中提供流动性
* 激励在市场中间价附近被动、平衡地报价
* 鼓励交易活动
* 阻止明显的剥削行为

<Note>最低奖励支付金额为 **\$1**；低于此金额的奖励将不会支付。</Note>

<Tip>
  每个受激励市场都会定义符合条件的最小订单规模、最大价差和奖励分配。请参阅[流动性奖励设置](/cn/market-data/market-details#流动性奖励设置)，读取市场的当前配置。
</Tip>

***

## Crypto TWAP 流动性奖励

为支持过渡期间的流动性，Polymarket 将在整个 8 月期间，为所有受影响的市场
增加 **\$100 万**流动性奖励。该分配仅适用于基于 TWAP 结算的加密货币
**5 分钟**、**15 分钟**和 **4 小时**市场。

<Note>
  下方奖池为配置的奖励上限。实际支付金额取决于符合条件的报价以及本页的评分方法。
</Note>

### 5 分钟市场 — \$550k

| 分配对象             | 金额          |
| ---------------- | ----------- |
| BTC              | \$300k      |
| SOL、ETH、HYPE、XRP | \$200k 平均分配 |
| BNB、DOGE         | \$50k 平均分配  |

### 15 分钟市场 — \$350k

| 分配对象             | 金额          |
| ---------------- | ----------- |
| BTC              | \$225k      |
| SOL、ETH、HYPE、XRP | \$100k 平均分配 |
| BNB、DOGE         | \$25k 平均分配  |

### 4 小时市场 — \$100k

| 分配对象             | 金额         |
| ---------------- | ---------- |
| BTC              | \$50k      |
| SOL、ETH、HYPE、XRP | \$40k 平均分配 |
| BNB、DOGE         | \$10k 平均分配 |

***

## 方法论

流动性提供者根据一个公式获得奖励，该公式奖励市场参与度，提升双边深度（单边订单仍然计分），以及相对于规模截止调整后中间价更紧的价差。每个市场配置一个最大价差和最小规模截止，在此范围内的订单才会被考虑。奖励的平均值由每个参与者在市场 m 中的 Q<sub>n</sub> 相对份额决定。

### 变量

| Variable       | Description                |
| -------------- | -------------------------- |
| S              | 订单位置评分函数                   |
| v              | 与中间价的最大价差（以美分计）            |
| s              | 与规模截止调整后中间价的价差             |
| b              | 游戏内乘数                      |
| m              | 市场                         |
| m'             | 市场补充（即如果 m = YES，则为 NO）    |
| n              | 交易者索引                      |
| u              | 样本索引                       |
| c              | 缩放因子（目前所有市场均为 3.0）         |
| Q<sub>ne</sub> | 样本中第一个订单簿的总分               |
| Q<sub>no</sub> | 样本中第二个订单簿的总分               |
| Spread%        | 市场 m 中订单 n 与中间价的距离（基点或相对值） |
| BidSize        | 以份额计价的买单数量                 |
| AskSize        | 以份额计价的卖单数量                 |

***

## 公式

### 1. 订单评分函数

基于调整后中间价和最小合格价差之间位置的订单二次评分规则：

$S(v,s)= (\frac{v-s}{v})^2 \cdot b$

### 2. 第一市场边分数

$Q_{one}= S(v,Spread_{m_1}) \cdot BidSize_{m_1} + S(v,Spread_{m_2}) \cdot BidSize_{m_2} + \dots $
$ + S(v, Spread*{m^\prime_1}) \cdot AskSize*{m^\prime*1} + S(v, Spread*{m^\prime*2}) \cdot AskSize*{m^\prime_2}$

### 3. 第二市场边分数

$Q_{two}= S(v,Spread_{m_1}) \cdot AskSize_{m_1} + S(v,Spread_{m_2}) \cdot AskSize_{m_2} + \dots $
$ + S(v, Spread*{m^\prime_1}) \cdot BidSize*{m^\prime*1} + S(v, Spread*{m^\prime*2}) \cdot BidSize*{m^\prime_2}$

### 4. 最小分数

通过取 Q<sub>ne</sub> 和 Q<sub>no</sub> 的最小值来提升双边流动性，同时仍以降低的比率（除以 c）奖励单边流动性。

**如果中间价在 \[0.10, 0.90] 范围内** ——单边流动性可以计分：

$Q_{\min} = \max(\min({Q_{one}, Q_{two}}), \max(Q_{one}/c, Q_{two}/c))$

**如果中间价在 \[0, 0.10) 或 (0.90, 1.0] 范围内** ——流动性必须是双边的才能计分：

$Q_{\min} = \min({Q_{one}, Q_{two}})$

### 5. 标准化分数

做市商的 Q<sub>min</sub> 除以给定样本中所有做市商的 Q<sub>min</sub> 总和：

$Q_{normal} = \frac{Q_{min}}{\sum_{n=1}^{N}{(Q_{min})_n}}$

### 6. 时期分数

交易者在一个时期中所有样本的 Q<sub>normal</sub> 总和：

$Q_{epoch} = \sum_{u=1}^{10,080}{(Q_{normal})_u}$

### 7. 最终分数

通过除以给定时期中所有做市商的 Q<sub>epoch</sub> 总和来标准化 Q<sub>epoch</sub>。该值乘以市场可用奖励即可得到交易者的奖励：

$Q_{final}=\frac{Q_{epoch}}{\sum_{n=1}^{N}{(Q_{epoch})_n}}$

***

## 实例演示

假设调整后的市场中间价为 0.50，m 和 m' 的最大价差配置均为 3 美分。

### 步骤 2 - 第一边分数

交易者有以下未成交订单：

* 在 m 上以 0.49 价格买入 100Q（价差 = 1 美分）
* 在 m 上以 0.48 价格买入 200Q（价差 = 2 美分）
* 在 m' 上以 0.51 价格卖出 100Q（价差 = 1 美分）

$$
Q_{ne} = \left( \frac{(3-1)}{3} \right)^2 \cdot 100 + \left( \frac{(3-2)}{3} \right)^2 \cdot 200 + \left( \frac{(3-1)}{3} \right)^2 \cdot 100
$$

Q<sub>ne</sub> 使用随机采样每分钟计算一次。

### 步骤 3 - 第二边分数

同一交易者还有：

* 在 m 上以 0.485 价格买入 100Q（价差 = 1.5 美分）
* 在 m' 上以 0.48 价格买入 100Q（价差 = 2 美分）
* 在 m' 上以 0.505 价格卖出 200Q（价差 = 0.5 美分）

$$
Q_{no} = \left( \frac{(3-1.5)}{3} \right)^2 \cdot 100 + \left( \frac{(3-2)}{3} \right)^2 \cdot 100 + \left( \frac{(3-.5)}{3} \right)^2 \cdot 200
$$

Q<sub>no</sub> 使用随机采样每分钟计算一次。

### 步骤 4-7

4. 取 Q<sub>ne</sub> 和 Q<sub>no</sub> 的最小值（如果中间价在 \[0.10, 0.90] 范围内则进行单边调整）
5. 对样本中的所有其他做市商进行标准化
6. 对时期中的所有 10,080 个样本求和
7. 再次标准化以获得最终奖励份额
