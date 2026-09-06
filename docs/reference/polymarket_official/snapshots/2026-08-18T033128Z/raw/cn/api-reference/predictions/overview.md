> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 概览

> 了解用于构建 Polymarket 预测市场集成的 API。

## API

Polymarket 预测市场由多个 API 组成，每个 API 负责集成中的不同部分。

<CardGroup cols={2}>
  <Card title="Gamma API" icon="database">
    **`https://gamma-api.polymarket.com`**

    发现事件和市场，并获取使用它们所需的元数据。
  </Card>

  <Card title="CLOB API" icon="arrows-rotate">
    **`https://clob.polymarket.com`**

    读取实时市场状态，然后下单和管理订单。
  </Card>

  <Card title="Data API" icon="chart-line">
    **`https://data-api.polymarket.com`**

    在发现市场和交易后，了解账户与市场活动。
  </Card>

  <Card title="Relayer API" icon="bolt">
    **`https://relayer-v2.polymarket.com`**

    提交受支持的钱包交易，而无需账户持有用于支付 Gas 的 POL。
  </Card>

  <Card title="WebSocket API" icon="radio">
    **实时数据流**

    通过市场、账户、体育和 RFQ 事件持续更新集成。
  </Card>

  <Card title="Bridge API" icon="arrow-right-arrow-left">
    **`https://bridge.polymarket.com`**

    使用受支持的资产向 Polymarket 充值或从中提现。
  </Card>
</CardGroup>

## 集成前须知

<CardGroup cols={2}>
  <Card title="速率限制" icon="gauge" href="/cn/api-reference/rate-limits">
    查看各服务和端点的请求限制。
  </Card>

  <Card title="地区限制" icon="globe" href="/cn/api-reference/geoblock">
    查看哪些地区可以下单。
  </Card>
</CardGroup>
