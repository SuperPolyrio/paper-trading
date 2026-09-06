> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 组合仓位

> 由现有 Polymarket 结果构建的多腿仓位

组合仓位让交易者能够针对多个 Polymarket 结果表达一个统一观点。组合仓位
不是每次交易一个市场，而是将现有结果代币组合成一对新的 YES/NO 代币。

## 它们代表什么

组合 **YES** 仓位代表多个交易腿的合取：

```text theme={null}
YES(
  YES(Market A) and YES(Market B) and NO(Market C)
)
```

只有组合中的每个交易腿都有收益时，它才会产生收益——在上述示例中，即
Market A 结算为 YES、Market B 结算为 YES，并且 Market C 结算为 NO。
对应的组合 **NO** 仓位是其补集：

```text theme={null}
NO(
  YES(Market A) and YES(Market B) and NO(Market C)
)
```

当完整合取没有收益时，它会产生收益。在此示例中，只要 Market A 结算为 NO、
Market B 结算为 NO，或 Market C 结算为 YES，它就会产生收益。

## 代币的工作原理

组合条件代表多个交易腿的合取，但不代表方向。上述组合条件为：

```text theme={null}
YES(Market A) and YES(Market B) and NO(Market C)
```

每个组合条件都有两个**组合仓位**：YES 和 NO，并且各自拥有 ERC 1155 代币 ID：

| 仓位  | 含义        |
| --- | --------- |
| YES | 完整组合产生收益  |
| NO  | 完整组合不产生收益 |

与标准 CTF 仓位一样，YES 和 NO 对都有全额抵押。拆分抵押品会创建匹配的
YES 和 NO 组合代币，合并匹配的代币对会返还抵押品。

但是，这些仓位*不*属于 Conditional Tokens Framework。它们存在于一个名为
Positions Framework 的新框架中。

## 结算

对于普通二元结果，只有每个交易腿都获胜时，组合 YES 仓位才会产生收益。
如果任一交易腿失败，对应的组合 NO 仓位会产生收益。

如果部分交易腿已经结算而其他交易腿仍未结束，可以将仓位压缩为只保留
未结算交易腿的较简单仓位，并实现已结算的抵押品价值。

## 相关页面

<CardGroup cols={3}>
  <Card title="CTF 概览" icon="coins" href="/cn/trading/positions/how-positions-work">
    Conditional Tokens 的简要概览
  </Card>

  <Card title="拆分代币" icon="scissors" href="/cn/trading/positions/manage#拆分仓位">
    创建 YES 和 NO 代币对
  </Card>

  <Card title="Combos" icon="code" href="/cn/trading/combos/overview">
    通过 RFQ 为组合仓位报价
  </Card>
</CardGroup>
