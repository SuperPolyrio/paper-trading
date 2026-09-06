> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 概览

> 构建通过 Polymarket 路由订单的应用

**Builder** 是将用户订单路由到 Polymarket 的个人、团体或组织。如果你创建的平台允许用户通过你的系统在 Polymarket 上交易，则可以加入此计划。

## 计划权益

<CardGroup cols={2}>
  <Card title="免 Gas 交易" icon="gas-pump">
    通过 relayer 免费执行所有链上操作
  </Card>

  <Card title="订单归因" icon="tag">
    获得订单归因，并在 Builder 排行榜上竞争资助
  </Card>
</CardGroup>

### 你将获得

| 权益               | 说明                                                                 |
| ---------------- | ------------------------------------------------------------------ |
| **Relayer 访问权限** | 免费部署钱包、设置授权、执行订单和 CTF 操作                                           |
| **交易量追踪**        | 将所有订单归因到你的 Builder 资料                                              |
| **排行榜**          | 在 [builders.polymarket.com](https://builders.polymarket.com) 上公开展示 |
| **支持**           | Telegram 频道和工程支持（Verified+）                                        |

## 工作原理

<Steps>
  <Step title="用户下单">用户通过你的应用下单。</Step>
  <Step title="附加 Builder Code">应用为每个订单添加你的 Builder Code。</Step>

  <Step title="提交到 CLOB">
    订单提交到 Polymarket CLOB；Builder Code
    作为已签名订单的一部分序列化到链上。
  </Step>

  <Step title="执行交易">Polymarket 撮合订单，并承担链上操作的 Gas 费用。</Step>

  <Step title="交易量归因">
    对于每笔附有你的 Builder Code 的已撮合交易，其交易量都会计入你的 Builder
    账户。
  </Step>
</Steps>

## 开始使用

<Steps>
  <Step title="创建 Builder 资料">
    打开 polymarket.com → Settings →
    [Builders](https://polymarket.com/settings?tab=builder)，然后复制你的 Builder Code。
  </Step>

  <Step title="附加 Builder Code">
    为提交的每个订单附加 Builder Code。请参阅 [Builder
    归因](/cn/trading/place-orders#构建者归因)。
  </Step>

  <Step title="启用免 Gas 交易">
    使用 Polymarket relayer 部署钱包并[执行免 Gas
    交易](/cn/trading/wallets-auth#执行免-gas-交易)。
  </Step>

  <Step title="追踪表现">
    在 [Builder 排行榜](https://builders.polymarket.com)上监控交易量。

    已撮合交易量最多可能需要 **24 小时**才会显示在排行榜上。
  </Step>
</Steps>

***

## 后续步骤

<CardGroup cols={2}>
  <Card title="创建新账户" icon="key" href="/cn/trading/wallets-auth#创建新账户">
    创建 Builder 账户，以及用于为用户配置账户的凭据。
  </Card>

  <Card title="了解等级" icon="layer-group" href="/cn/programs/builders/tiers">
    了解速率限制和升级方式。
  </Card>

  <Card title="订单归因" icon="tag" href="/cn/trading/place-orders#构建者归因">
    配置客户端，将交易归因到你的账户。
  </Card>

  <Card title="执行免 Gas 交易" icon="gas-pump" href="/cn/trading/wallets-auth#执行免-gas-交易">
    无需支付 Gas 即可授权 token、转移资金和管理仓位。
  </Card>
</CardGroup>
