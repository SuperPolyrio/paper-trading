> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Python SDK

> 开始使用统一的 Polymarket Python SDK。

`polymarket-client` 软件包是构建 Polymarket 集成的官方 Python SDK。

<CardGroup cols={2}>
  <Card title="GitHub" icon="github" href="https://github.com/Polymarket/py-sdk/">
    查看 Python SDK 源代码。
  </Card>

  <Card title="PyPI 软件包" icon="python" href="https://pypi.org/project/polymarket-client/">
    从 PyPI 安装 `polymarket-client`。
  </Card>
</CardGroup>

有关近期 Python 版本，请参阅 [SDK 更新日志](/changelog/sdks#python)。

## 快速入门

<Steps>
  <Step title="安装 SDK">
    使用你偏好的软件包管理器安装 SDK。

    <CodeGroup>
      ```bash uv theme={null}
      uv add polymarket-client
      ```

      ```bash pip theme={null}
      pip install polymarket-client
      ```

      ```bash poetry theme={null}
      poetry add polymarket-client
      ```
    </CodeGroup>
  </Step>

  <Step title="创建公共客户端">
    创建 `AsyncPublicClient` 以访问公开的 Polymarket 数据。

    ```python theme={null}
    from polymarket import AsyncPublicClient


    async with AsyncPublicClient() as client:
        ...
    ```
  </Step>

  <Step title="获取活跃市场">
    获取一页活跃市场，发现交易机会。

    ```python theme={null}
    import asyncio

    from polymarket import AsyncPublicClient


    async def main() -> None:
        async with AsyncPublicClient() as client:
            pages = client.list_markets(closed=False)
            first_page = await pages.first_page()

            for market in first_page.items:
                # market: Market
                ...


    asyncio.run(main())
    ```
  </Step>
</Steps>

## 异步与同步接口

公共客户端访问公开数据。[安全客户端](#安全客户端)还提供交易和账户访问。两种客户端均提供异步和同步形式。

| 接口 | 公开数据                | 交易和账户数据             |
| -- | ------------------- | ------------------- |
| 异步 | `AsyncPublicClient` | `AsyncSecureClient` |
| 同步 | `PublicClient`      | `SecureClient`      |

异步客户端适合已经运行事件循环的服务、机器人和应用。同步客户端适合脚本和笔记本。

<CodeGroup>
  ```python Async theme={null}
  from polymarket import AsyncPublicClient


  async with AsyncPublicClient() as client:
      pages = client.list_markets(closed=False)
      first_page = await pages.first_page()
  ```

  ```python Sync theme={null}
  from polymarket import PublicClient


  with PublicClient() as client:
      pages = client.list_markets(closed=False)
      first_page = pages.first_page()
  ```
</CodeGroup>

<Note>
  包括实时订阅在内的部分功能仅支持异步接口，因为它们依赖长连接数据流。
</Note>

## 分页

SDK 的列表方法使用统一的分页器接口。遍历分页器可获取所有页面；如果需要控制请求节奏，也可以逐页获取。

<CodeGroup>
  ```python Async theme={null}
  pages = client.list_markets(closed=False, page_size=10)

  async for page in pages:
      for market in page.items:
          # market: Market
          ...
  ```

  ```python Sync theme={null}
  pages = client.list_markets(closed=False, page_size=10)

  for page in pages:
      for market in page.items:
          # market: Market
          ...
  ```
</CodeGroup>

如果页面边界并不重要，可以直接遍历所有项目。

<CodeGroup>
  ```python Async theme={null}
  async for market in pages.iter_items():
      # market: Market
      ...
  ```

  ```python Sync theme={null}
  for market in pages.iter_items():
      # market: Market
      ...
  ```
</CodeGroup>

`page.next_cursor` 是不透明的 SDK 游标。如果希望稍后继续扫描，请原样保存；从第一页开始时则省略它。

<CodeGroup>
  ```python Async theme={null}
  pages = client.list_markets(closed=False, page_size=10)
  page = await pages.first_page()

  if page.next_cursor:
      second_page = await pages.from_cursor(page.next_cursor).first_page()
      # second_page.items: tuple[Market, ...]
  ```

  ```python Sync theme={null}
  pages = client.list_markets(closed=False, page_size=10)
  page = pages.first_page()

  if page.next_cursor:
      second_page = pages.from_cursor(page.next_cursor).first_page()
      # second_page.items: tuple[Market, ...]
  ```
</CodeGroup>

## 类型

SDK 方法返回带类型的模型，其公共类型由 `polymarket` 导出。

```python theme={null}
from polymarket import Event, Market, OrderBook, PriceHistoryPoint
```

SDK 还为领域标识符和 EVM 地址使用不同类型。对精度敏感的值使用 `decimal.Decimal`，日期和时间戳使用 Python 的 `datetime` 类型。

```python theme={null}
from polymarket import CtfConditionId, EvmAddress, MarketId, TokenId
```

## 错误处理

所有 SDK 异常都继承自 `PolymarketError`。当应用能够处理特定异常时先捕获该异常，再捕获基础异常作为后备。

```python theme={null}
from polymarket import PolymarketError, RateLimitError, UserInputError

pages = client.list_markets(closed=False)

try:
    page = await pages.first_page()
    # page.items: tuple[Market, ...]
except RateLimitError:
    # Retry later.
    ...
except UserInputError:
    # Fix the request parameters.
    ...
except PolymarketError:
    # Handle any other SDK error.
    raise
```

## 安全客户端

当集成需要交易或访问账户数据时，请创建 `AsyncSecureClient` 或 `SecureClient`。Python SDK 使用本地私钥创建签名器。

<CodeGroup>
  ```python Async theme={null}
  import os

  from polymarket import AsyncSecureClient


  async with await AsyncSecureClient.create(
      private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
  ) as client:
      ...
  ```

  ```python Sync theme={null}
  import os

  from polymarket import SecureClient


  with SecureClient.create(
      private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
  ) as client:
      ...
  ```
</CodeGroup>

接下来，请参阅[钱包与身份验证](/cn/trading/wallets-auth)，配置账户钱包和无 Gas 交易。

## 实时订阅

Python SDK 可以把多个实时数据源的更新合并为单一事件流。

<Note>
  实时订阅仅可通过 `AsyncPublicClient` 和 `AsyncSecureClient`
  使用。`PublicClient` 和 `SecureClient` 未实现 `subscribe()`。
</Note>

使用一个或多个订阅规范调用 `subscribe()`。`AsyncPublicClient` 和 `AsyncSecureClient` 都支持公共数据流；`AsyncSecureClient` 还提供已连接账户的私有数据流。

<CodeGroup>
  ```python Public Client theme={null}
  from polymarket.streams import MarketSpec, SportsSpec

  token_id = "<token_id>"

  async with await client.subscribe(
      [MarketSpec(token_ids=[token_id]), SportsSpec()],
  ) as stream:
      async for event in stream:
          # event:
          #   MarketBookEvent
          #   | MarketPriceChangeEvent
          #   | MarketLastTradePriceEvent
          #   | MarketTickSizeChangeEvent
          #   | MarketBestBidAskEvent
          #   | NewMarketEvent
          #   | MarketResolvedEvent
          #   | SportsEvent

          if should_close:
              break
  ```

  ```python Secure Client theme={null}
  from polymarket.streams import MarketSpec, UserSpec

  token_id = "<token_id>"

  async with await client.subscribe(
      [MarketSpec(token_ids=[token_id]), UserSpec()],
  ) as stream:
      async for event in stream:
          # event:
          #   MarketBookEvent
          #   | MarketPriceChangeEvent
          #   | MarketLastTradePriceEvent
          #   | MarketTickSizeChangeEvent
          #   | MarketBestBidAskEvent
          #   | NewMarketEvent
          #   | MarketResolvedEvent
          #   | UserEvent

          if should_close:
              break
  ```
</CodeGroup>

在处理特定事件结构之前，请先通过 `event.topic` 缩小类型范围。示例仅展示了部分数据流；有关各数据源的选项，请参阅[实时数据](/cn/market-data/realtime-data)、[实时订单更新](/cn/trading/realtime-order-updates)和 [Perps 实时更新](/perps/realtime-updates)。

## DataFrame 与 Jupyter

将页面和分页器直接转换为 [pandas DataFrame](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.html)、[Polars DataFrame](https://docs.pola.rs/py-polars/html/reference/dataframe/) 或 [Apache Arrow Table](https://arrow.apache.org/docs/python/generated/pyarrow.Table.html)。安装所用格式对应的 `pandas`、`polars` 或 `arrow` 额外依赖；`quant` 额外依赖会安装全部三项。

<CodeGroup>
  ```python Pandas theme={null}
  pages = client.list_markets(closed=False)
  df = await pages.to_pandas(limit=100)
  ```

  ```python Polars theme={null}
  pages = client.list_markets(closed=False)
  df = await pages.to_polars(limit=100)
  ```

  ```python Arrow theme={null}
  pages = client.list_markets(closed=False)
  table = await pages.to_arrow(limit=100)
  ```
</CodeGroup>

分页器转换必须明确指定限制。传入 `limit=None` 可获取所有项目。

常用 SDK 对象还会在 [Jupyter](https://docs.jupyter.org/en/stable/) 笔记本中呈现为紧凑卡片。将对象作为单元格的最后一个表达式返回即可显示。

```python theme={null}
pages = client.list_markets(closed=False)
page = await pages.first_page()
page.items[0]
```

## 后续步骤

<CardGroup cols={3}>
  <Card title="读取市场数据" icon="chart-line" href="/cn/market-data/overview">
    发现市场，并使用价格、订单簿和历史数据。
  </Card>

  <Card title="订阅实时更新" icon="radio" href="/cn/market-data/realtime-data">
    实时接收市场和账户更新。
  </Card>

  <Card title="下第一笔订单" icon="rocket" href="/cn/trading/quickstart">
    设置账户并完成第一笔经过身份验证的交易。
  </Card>
</CardGroup>
