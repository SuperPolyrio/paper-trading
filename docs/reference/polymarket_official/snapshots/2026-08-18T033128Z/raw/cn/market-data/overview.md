> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 概览

> 了解 Polymarket 如何组织市场数据，以及应从何处开始。

使用市场数据的第一步，是确定应用关注的事件、市场或结果。本节将从这一选择出发，引导你获取应用所需的数据。

## 了解数据模型

在 Polymarket 中，一个事件包含一个或多个市场。每个市场都是一个可交易的问题，包含 YES 和 NO 两种结果，每种结果都有自己的 token ID。读取某个结果的价格或订单簿时，请使用该结果的 token ID。

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/event.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=0c9a264aec9a22ce5a20c4cc7980806d" alt="" className="dark:hidden" width="1540" height="952" data-path="images/core-concepts/event.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/event.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=912e41bebfe8c1a43ef53b89685ca3d2" alt="" className="hidden dark:block" width="1540" height="952" data-path="images/dark/core-concepts/event.png" />
</Frame>

<Steps>
  <Step title="发现事件">
    浏览活跃事件、按主题搜索，或通过 Polymarket URL 直接获取事件。
  </Step>

  <Step title="选择市场">查看事件中的市场，并选择应用关注的具体问题。</Step>

  <Step title="选择结果">
    选择 YES 或 NO 并保存其 token ID。你将使用它读取价格、访问订单簿和下单。
  </Step>

  <Step title="读取或流式接收数据">
    使用已收集的标识符，获取或流式接收应用所需的市场数据。
  </Step>
</Steps>

## 选择工作流

如果你刚开始使用 Polymarket 市场数据，请先阅读[发现市场](/cn/market-data/discover-markets)。

<CardGroup cols={2}>
  <Card title="发现市场" icon="magnifying-glass" href="/cn/market-data/discover-markets">
    查找应用关注的事件和市场。
  </Card>

  <Card title="市场详情" icon="list-tree" href="/cn/market-data/market-details">
    了解市场的结果、状态、交易约束、费用及其他属性。
  </Card>

  <Card title="价格和订单簿" icon="chart-line" href="/cn/market-data/prices-order-books">
    了解当前价格和流动性，或查看价格如何变化。
  </Card>

  <Card title="分析" icon="chart-column" href="/cn/market-data/public-analytics">
    分析市场活动，并比较交易者和 Builder 的表现。
  </Card>

  <Card title="实时数据" icon="radio" href="/cn/market-data/realtime-data">
    随市场及相关数据的变化，让应用保持最新状态。
  </Card>

  <Card title="Chainlink TWAP 价格" icon="clock" href="/cn/market-data/chainlink-twap">
    流式接收 30 秒和 60 秒时间加权加密货币价格。
  </Card>
</CardGroup>
