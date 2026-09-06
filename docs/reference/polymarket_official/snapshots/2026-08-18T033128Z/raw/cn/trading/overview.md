> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 概览

> 了解 Polymarket 中订单、结算和持仓如何协同运作。

交易将 CLOB 上的已签名订单与 Polygon 上的结算连接起来。你需要选择一个结果 token，使用签名者授权订单，然后将其提交到订单簿。订单撮合后，结算过程会在交易账户之间转移 pUSD 和结果 token。

## 交易流程

先完成一次账户设置，然后为每笔订单重复其余步骤。

<Steps>
  <Step title="设置账户">
    将签名者连接到账户钱包，为钱包充值，并授予所需的交易授权。
  </Step>

  <Step title="选择结果">
    选择要买入或卖出的结果 token，并查看当前的市场限制。
  </Step>

  <Step title="下单">选择价格和数量，签署订单，然后将其提交到 CLOB。</Step>

  <Step title="管理订单">
    监控成交情况，并取消剩余的未成交数量。已撮合的交易会在 Polygon
    上结算，并更新你的余额和持仓。
  </Step>
</Steps>

<Info>
  Exchange
  运营方可以撮合订单并执行订单排序，但不能设定价格，也不能执行未经用户授权的交易。
</Info>

## 开始交易

<CardGroup cols={3}>
  <Card title="完成第一笔订单" icon="rocket" href="/cn/trading/quickstart">
    完成首个需要身份验证的交易流程。
  </Card>

  <Card title="钱包与身份验证" icon="key" href="/cn/trading/wallets-auth">
    将签名者连接到持有资金和持仓的账户钱包。
  </Card>

  <Card title="订单生命周期" icon="repeat" href="/cn/concepts/order-lifecycle">
    了解订单从提交到成交、取消或到期的完整过程。
  </Card>
</CardGroup>
