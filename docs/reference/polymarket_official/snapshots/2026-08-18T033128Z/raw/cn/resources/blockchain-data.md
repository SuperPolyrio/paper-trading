> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 数据资源

> 访问 Polymarket 链上活动数据并进行分析

交易、余额、持仓和赎回等写入区块链的 Polymarket 数据，可以通过多种链上分析平台和区块链数据提供商访问。Polymarket 也提供自己的 API 和 WebSocket。更多信息请参阅 [API 参考](/cn/api-reference/predictions/overview)。

本页旨在为 Polymarket Builder、研究人员和分析师提供公共资源。

***

## 数据

### Goldsky

[Goldsky](https://docs.goldsky.com/chains/polymarket) 提供实时数据管道，可将 Polymarket 链上活动（如交易、余额和持仓等）传输到你自己的数据库或数据仓库。

Goldsky 还与 [ClickHouse](https://clickhouse.com) 合作创建了 [CryptoHouse](https://crypto.clickhouse.com)，你可以使用 SQL 查询 Polymarket 链上数据。

### Dune

[Dune](https://dune.com) 是一个区块链分析平台，提供 Polymarket 链上活动数据（如交易、余额和持仓等）。你可以使用 SQL 查询 Polymarket 数据并创建自定义仪表板。

以下查询可以帮助你快速开始：

| 查询   | 说明                             | 链接                                             |
| ---- | ------------------------------ | ---------------------------------------------- |
| 交易量  | 名义交易量以及 Maker 和 Taker USDC 交易量 | [查看 Dune 查询](https://dune.com/queries/6545441) |
| TVL  | 锁定在 Polymarket 智能合约中的 USDC     | [查看 Dune 查询](https://dune.com/queries/6588784) |
| 未平仓量 | 市场当前及历史预估未平仓量                  | [查看 Dune 查询](https://dune.com/queries/6555478) |

### Allium

[Allium](https://docs.allium.so/historical-data/predictions) 是一个区块链分析平台，提供 Polymarket 链上活动数据（如交易、余额和持仓等）。你可以使用 SQL 查询 Polymarket 数据并创建自定义仪表板。

\--

## 仪表板

以下第三方区块链分析平台汇总并可视化 Polymarket 数据：

<CardGroup cols={4}>
  <Card title="Blockworks" img="https://blockworks.com/apple-touch.png" href="https://blockworks.com/analytics/polymarket" />

  <Card title="Artemis" img="https://pbs.twimg.com/profile_images/1896982195723546624/2XeO9mPb_400x400.png" href="https://app.artemisanalytics.com/asset/polymarket?from=assets" />

  <Card title="Dune" img="https://pbs.twimg.com/profile_images/1986458079248986112/qq80s3hx_400x400.jpg" href="https://dune.com/discover/content/popular?q=polymarket&resource-type=dashboards" />

  <Card title="DeFiLlama" img="https://pbs.twimg.com/profile_images/1915756547705036800/rAeLzZqs_400x400.jpg" href="https://defillama.com/protocol/polymarket" />

  <Card title="The Block" img="https://pbs.twimg.com/profile_images/1944749695525425152/9babG7Df_400x400.jpg" href="https://www.theblock.co/data/decentralized-finance/prediction-markets-and-betting" />

  <Card title="Token Terminal" img="https://pbs.twimg.com/profile_images/1594678659222306817/SMum_RcQ_400x400.jpg" href="https://tokenterminal.com/explorer/projects/polymarket" />

  <Card title="Allium" img="https://pbs.twimg.com/profile_images/1778926940407132160/UEwR3lHt_400x400.jpg" href="https://predictions.allium.so" />
</CardGroup>

### 社区仪表板

由社区创建的 Polymarket 链上分析 Dune 仪表板：

| 仪表板                                               | 创建者                                             | 链接                                                                  |
| ------------------------------------------------- | ----------------------------------------------- | ------------------------------------------------------------------- |
| Polymarket Overview                               | [@datadashboards](https://x.com/datadashboards) | [查看仪表板](https://dune.com/datadashboards/polymarket-overview)        |
| Polymarket Volume, OI, Markets, Addresses and TVL | [@hildobby](https://x.com/hildobby)             | [查看仪表板](https://dune.com/hildobby/polymarket)                       |
| Polymarket Historical Accuracy                    | [@alexmccullaaa](https://x.com/alexmccullaaa)   | [查看仪表板](https://dune.com/alexmccullough/how-accurate-is-polymarket) |
| Polymarket Builders Dashboard                     | [@defioasis](https://x.com/defioasis)           | [查看仪表板](https://dune.com/gateresearch/pmbuilders)                   |
