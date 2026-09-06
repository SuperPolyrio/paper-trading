> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Tiers

> 速率限制、奖励以及如何升级

Builder Program 使用分层系统来管理速率限制,同时奖励高性能的集成。更高的等级可解锁更高的限制、每周奖励、收入分成和优先支持。

## 功能定义

| Feature                     | Description                                                     |
| --------------------------- | --------------------------------------------------------------- |
| **Daily Relayer Txn Limit** | Deposit Wallet、Safe 和 Proxy Wallet 操作的每日 Relayer 交易上限           |
| **API Rate Limits**         | 非 relayer 端点(CLOB、Gamma 等)的速率限制                                 |
| **Gasless Trading**         | 为受支持的智能钱包操作补贴 gas 费用                                            |
| **Order Attribution**       | 订单被追踪并归因到你的 Builder 个人资料                                        |
| **Builder Fees**            | 路由订单的 Builder 可以收取费用，并通过订单流实现变现                                 |
| **Leaderboard Visibility**  | 在 [Builder Leaderboard](https://builders.polymarket.com/) 上的可见性 |
| **Telegram Channel**        | 用于公告和支持的私密 Builders 频道                                          |
| **Engineering Support**     | 直接联系工程团队                                                        |
| **Marketing Support**       | 通过 Polymarket 官方社交账号进行推广                                        |
| **Priority Access**         | 优先体验新功能和产品                                                      |

***

## 等级对比

| Feature                     | Unverified |  Verified  |  Partner  |
| --------------------------- | :--------: | :--------: | :-------: |
| **Daily Relayer Txn Limit** |   100/day  | 10,000/day | Unlimited |
| **API Rate Limits**         |  Standard  |  Standard  |  Highest  |
| **Subsidized Transactions** |     Yes    |     Yes    |    Yes    |
| **Order Attribution**       |     Yes    |     Yes    |    Yes    |
| **RevShare Protocol**       |      —     |     Yes    |    Yes    |
| **Leaderboard Visibility**  |      —     |     Yes    |    Yes    |
| **Telegram Channel**        |      —     |     Yes    |    Yes    |
| **Engineering Support**     |      —     |  Standard  |  Elevated |
| **Marketing Support**       |      —     |  Standard  |  Elevated |
| **Priority Access**         |      —     |      —     |    Yes    |

***

## Unverified

<Card title="100 transactions/day" icon="seedling">
  所有新 builders 的默认等级。无需审批即可立即开始。
</Card>

**如何开始:**

1. 前往 [polymarket.com/settings?tab=builder](https://polymarket.com/settings?tab=builder)
2. 创建 builder 个人资料
3. 点击 **"+ Create New"** 生成 API keys
4. 将 [builder code](/cn/trading/place-orders#构建者归因) 附加到 CLOB 订单以完成归因；gasless 钱包操作使用 Relayer API key

**包含内容:**

* 通过 Safe/Proxy 钱包在所有 CLOB 订单上进行无 gas 交易
* 通过 Safe/Proxy 钱包在每日限制内的所有 Relayer 交易上补贴 gas
* 订单归因到你的 builder 个人资料
* 访问所有客户端库和文档

***

## Verified

<Card title="10,000 transactions/day" icon="badge-check">
  适用于需要更高吞吐量的 builders。需要人工审批。
</Card>

**如何升级:**

联系我们时提供:

* 你的 Builder API Key
* 用例描述
* 预期交易量
* 你的应用、文档或 X 个人资料的链接

**相比 Unverified 解锁:**

* 每日 Relayer 交易限制提升 100 倍
* 访问 RevShare Protocol
* 在 [builders.polymarket.com](https://builders.polymarket.com) 上的排行榜可见性
* 基于交易量的每周 USDC 奖励
* 用于公告和支持的私密 Telegram 频道
* 已验证的附属徽章并获得 [@PolymarketBuild](https://x.com/PolymarketBuild) 的推广
* 资助(需经审批)

***

## Partner

<Card title="Unlimited transactions/day" icon="handshake">
  适用于高交易量集成和战略合作伙伴的企业级等级。
</Card>

**如何申请:**

联系 [builder@polymarket.com](mailto:builder@polymarket.com) 讨论合作机会。

**相比 Verified 解锁:**

* 无限 Relayer 交易
* 最高 API 速率限制
* 升级的工程支持
* 升级且协调的营销支持
* 优先体验新功能和产品
* 每周奖励计划的倍数加成

***

## 如何升级

<Steps>
  <Step title="构建和发布">从 Unverified 等级开始构建你的集成。</Step>
  <Step title="产生交易量">通过 Polymarket 路由订单并展示持续的使用情况。</Step>

  <Step title="申请验证">
    发送邮件至 [builder@polymarket.com](mailto:builder@polymarket.com),附上你的
    builder key 和用例。
  </Step>

  <Step title="获得批准">Polymarket 团队会审核申请并在几个工作日内回复。</Step>
</Steps>

## 联系方式

准备好升级或有疑问?

<Card title="builder@polymarket.com" icon="envelope" href="mailto:builder@polymarket.com">
  发送邮件给我们,附上你的 Builder API Key 和用例详情。
</Card>

## FAQ

<AccordionGroup>
  <Accordion title="如何知道我是否已验证">
    验证状态会显示在你的 [Builder Profile](https://polymarket.com/settings?tab=builder) 设置中。
  </Accordion>

  <Accordion title="如果我超出每日限制会怎样">
    超出每日限制的 Relayer
    请求会受到速率限制并返回错误。如果你经常达到限制,考虑升级到 Verified 或
    Partner 等级。
  </Accordion>

  <Accordion title="我能获得临时限制提升吗">
    对于特殊活动或产品发布,请联系 [builder@polymarket.com](mailto:builder@polymarket.com)。
  </Accordion>
</AccordionGroup>
