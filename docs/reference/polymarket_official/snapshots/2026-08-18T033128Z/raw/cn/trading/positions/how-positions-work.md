> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 仓位的工作原理

> 了解支撑 Polymarket 仓位的链上代币机制。

Polymarket 使用 [Conditional Token Framework
（CTF）](https://github.com/gnosis/conditional-tokens-contracts) 将市场结果
代币化。CTF 是由 Gnosis 开发的开放标准。了解 CTF 有助于理解仓位如何在
链上创建、组合和赎回。

## 什么是 CTF？

CTF 创建代表预测市场结果的 ERC-1155 代币。每个二元市场都有两种结果代币：

| 代币      | 可赎回         | 条件    |
| ------- | ----------- | ----- |
| **YES** | \$1.00 pUSD | 事件发生  |
| **NO**  | \$1.00 pUSD | 事件未发生 |

这些代币都有全额抵押。每对 YES 和 NO 代币都由通过 CTF 合约锁定的
恰好 `$1` 抵押品支持。

## 核心操作

CTF 提供三种在抵押品和仓位之间转换的操作：

<CardGroup cols={3}>
  <Card title="拆分" icon="scissors" href="/cn/trading/positions/manage#拆分仓位">
    将 pUSD 转换为一对 YES 和 NO 代币。
  </Card>

  <Card title="合并" icon="merge" href="/cn/trading/positions/manage#合并仓位">
    将一对 YES 和 NO 代币转换回 pUSD。
  </Card>

  <Card title="赎回" icon="hand-holding-dollar" href="/cn/trading/positions/manage#赎回已结算仓位">
    将已结算的结果代币兑换为其收益。
  </Card>
</CardGroup>

## 代币流转

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/token-flow.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=36f5a57946ac2b83136e17b6c06b358c" alt="pUSD 可以拆分为 YES 和 NO 结果代币，这些代币可以交易、合并，或在结算后赎回。" className="dark:hidden" width="1596" height="952" data-path="images/core-concepts/token-flow.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/token-flow.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=69d150ea49ffa18cd7f24689342b1bec" alt="pUSD 可以拆分为 YES 和 NO 结果代币，这些代币可以交易、合并，或在结算后赎回。" className="hidden dark:block" width="1596" height="952" data-path="images/dark/core-concepts/token-flow.png" />
</Frame>

## 代币标识符

每种结果代币都有唯一的**仓位 ID**，作为其 ERC-1155 代币 ID。CTF 分三步
在链上计算该 ID。

<Steps>
  <Step title="计算条件 ID">
    ```solidity theme={null}
    getConditionId(oracle, questionId, outcomeSlotCount)
    ```

    | 参数                 | 类型        | 值                                                                |
    | ------------------ | --------- | ---------------------------------------------------------------- |
    | `oracle`           | `address` | [UMA CTF Adapter](https://github.com/Polymarket/uma-ctf-adapter) |
    | `questionId`       | `bytes32` | UMA ancillary data 的哈希                                           |
    | `outcomeSlotCount` | `uint`    | 二元市场为 `2`                                                        |
  </Step>

  <Step title="计算集合 ID">
    ```solidity theme={null}
    getCollectionId(parentCollectionId, conditionId, indexSet)
    ```

    | 参数                   | 类型        | 值                                     |
    | -------------------- | --------- | ------------------------------------- |
    | `parentCollectionId` | `bytes32` | 顶层仓位为 `bytes32(0)`                    |
    | `conditionId`        | `bytes32` | 上一步得到的条件 ID                           |
    | `indexSet`           | `uint`    | 第一个结果为 `1`（`0b01`），第二个结果为 `2`（`0b10`） |

    `indexSet` 是用于标识集合包含哪些结果槽位的位掩码。它必须是条件结果槽位的
    非空真子集。二元市场的每个结果各有一个集合。
  </Step>

  <Step title="计算仓位 ID">
    ```solidity theme={null}
    getPositionId(collateralToken, collectionId)
    ```

    | 参数                | 类型        | 值                    |
    | ----------------- | --------- | -------------------- |
    | `collateralToken` | `IERC20`  | Polygon 上的 pUSD 合约地址 |
    | `collectionId`    | `bytes32` | 一个市场结果的集合 ID         |

    得到的仓位 ID 是市场 YES 和 NO 结果的 ERC-1155 代币 ID。大多数集成应从
    市场数据读取这些代币 ID。只有直接集成合约时才需要手动计算。
  </Step>
</Steps>

## 标准市场与负风险市场

Polymarket 对标准市场和负风险市场使用不同的 CTF 配置：

| 功能          | 标准市场              | 负风险市场                      |
| ----------- | ----------------- | -------------------------- |
| CTF 合约      | ConditionalTokens | ConditionalTokens          |
| Exchange 合约 | CTF Exchange      | Negative Risk CTF Exchange |
| 多个结果        | 相互独立的市场           | 通过转换关联                     |

对于负风险市场，转换操作可以将一个 NO 代币换成事件中其他结果的 YES 代币。
有关详情，请参阅[负风险市场](/cn/concepts/negative-risk)。

## 合约地址

有关 Polymarket 当前在 Polygon 上的智能合约地址，请参阅
[合约](/cn/resources/contracts)。

## 后续步骤

<CardGroup cols={3}>
  <Card title="拆分仓位" icon="scissors" href="/cn/trading/positions/manage#拆分仓位">
    从 pUSD 创建结果代币对。
  </Card>

  <Card title="合并仓位" icon="merge" href="/cn/trading/positions/manage#合并仓位">
    将配对的代币转换回 pUSD。
  </Card>

  <Card title="赎回仓位" icon="hand-holding-dollar" href="/cn/trading/positions/manage#赎回已结算仓位">
    在结算后领取收益。
  </Card>
</CardGroup>
