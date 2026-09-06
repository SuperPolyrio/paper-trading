> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 价格与订单簿

> 了解价格与订单簿如何反映 Polymarket 上的交易活动。

Polymarket 的每个结果都对应一种在 CLOB 上交易的代币。代币价格反映交易者愿意为该结果支付的金额，订单簿则显示当前挂出的买单和卖单。

以下示例假设你已经有一个市场对象，可以从中读取结果代币 ID。要查找或获取市场，请参阅
[**发现市场**](/cn/market-data/discover-markets)。

<Tabs>
  <Tab title="TypeScript">
    从市场对象中读取结果代币 ID：

    ```ts theme={null}
    const yesTokenId = market.outcomes.yes.tokenId!;
    const noTokenId = market.outcomes.no.tokenId!;
    ```
  </Tab>

  <Tab title="Python">
    从市场对象中读取结果代币 ID：

    ```python theme={null}
    if market.outcomes.yes.token_id is None or market.outcomes.no.token_id is None:
        raise RuntimeError("Market token IDs not found")

    yes_token_id = market.outcomes.yes.token_id
    no_token_id = market.outcomes.no.token_id
    ```
  </Tab>

  <Tab title="API">
    在市场对象中，结果代币 ID 存储为经过 JSON 编码的数组：

    ```json theme={null}
    {
      "clobTokenIds": "[\"<yes_token_id>\", \"<no_token_id>\"]"
    }
    ```

    解析数组，然后选择要读取的结果：

    ```bash theme={null}
    TOKEN_ID="<yes_token_id>"
    ```
  </Tab>
</Tabs>

## 订单簿

获取某个结果代币当前挂出的买单和卖单。买单按价格升序排列，卖单按价格降序排列，因此最优买价和卖价分别位于对应数组的最后一项。每个响应还包含订单簿状态的 `hash`。将其与上一次响应的哈希比较，即可判断两次读取之间订单簿是否发生变化。

### 获取订单簿

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchOrderBook()`，获取某个结果的订单簿。

    ```ts theme={null}
    const book = await client.fetchOrderBook({ tokenId: yesTokenId });

    // book: OrderBook
    ```

    返回的 `OrderBook` 描述各价格档位，以及解读这些档位所需的市场详情：

    <CodeGroup>
      ```ts OrderBook Type theme={null}
      type OrderBookLevel = {
        price: DecimalString;
        size: DecimalString;
      };

      type OrderBook = {
        conditionId: CtfConditionId;
        tokenId: TokenId;
        timestamp?: EpochMilliseconds | null;
        bids: OrderBookLevel[];
        asks: OrderBookLevel[];
        minOrderSize: DecimalString;
        tickSize: DecimalString;
        negRisk: boolean;
        lastTradePrice?: DecimalString | null;
        hash: OrderBookHash;
      };
      ```

      ```json OrderBook Example theme={null}
      {
        "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "timestamp": 1782753404902,
        "bids": [
          { "price": "0.01", "size": "2116131.59" },
          { "price": "0.02", "size": "139963.89" },
          { "price": "0.03", "size": "208169.44" },
          "..."
        ],
        "asks": [
          { "price": "0.99", "size": "93442.27" },
          { "price": "0.98", "size": "13229.55" },
          { "price": "0.97", "size": "4338.7" },
          "..."
        ],
        "minOrderSize": "5",
        "tickSize": "0.01",
        "negRisk": false,
        "lastTradePrice": "0.090",
        "hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_order_book()`，获取某个结果的订单簿。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    book = await client.get_order_book(token_id=yes_token_id)

    # book: OrderBook
    ```

    返回的 `OrderBook` 描述各价格档位，以及解读这些档位所需的市场详情：

    <CodeGroup>
      ```python OrderBook Type theme={null}
      class OrderBookLevel:
          price: Decimal
          size: Decimal

      class OrderBook:
          condition_id: CtfConditionId
          token_id: TokenId
          timestamp: datetime | None
          bids: tuple[OrderBookLevel, ...]
          asks: tuple[OrderBookLevel, ...]
          min_order_size: Decimal
          tick_size: Decimal
          neg_risk: bool
          last_trade_price: Decimal | None
          hash: str
      ```

      ```json OrderBook Example theme={null}
      {
        "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "timestamp": "2026-06-29T17:16:44.902000+00:00",
        "bids": [
          { "price": "0.01", "size": "2116131.59" },
          { "price": "0.02", "size": "139963.89" },
          { "price": "0.03", "size": "208169.44" },
          "..."
        ],
        "asks": [
          { "price": "0.99", "size": "93442.27" },
          { "price": "0.98", "size": "13229.55" },
          { "price": "0.97", "size": "4338.7" },
          "..."
        ],
        "min_order_size": "5",
        "tick_size": "0.01",
        "neg_risk": false,
        "last_trade_price": "0.090",
        "hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    获取某个结果的订单簿：

    ```bash theme={null}
    curl "https://clob.polymarket.com/book?token_id=$TOKEN_ID"
    ```

    除价格档位外，响应还包含验证订单所需的交易限制：

    ```json theme={null}
    {
      "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
      "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
      "timestamp": "1782753357257",
      "hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
      "bids": [
        { "price": "0.01", "size": "2151131.59" },
        { "price": "0.02", "size": "139963.89" },
        { "price": "0.03", "size": "208169.44" },
        "..."
      ],
      "asks": [
        { "price": "0.99", "size": "218442.27" },
        { "price": "0.98", "size": "13229.55" },
        { "price": "0.97", "size": "4338.7" },
        "..."
      ],
      "min_order_size": "5",
      "tick_size": "0.01",
      "neg_risk": false,
      "last_trade_price": "0.090"
    }
    ```
  </Tab>
</Tabs>

### 获取多个订单簿

批量读取订单簿可在一次请求中返回多个结果当前挂出的买单和卖单。

<Info>每次请求最多 500 项。</Info>

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchOrderBooks()`，通过一次请求获取多个订单簿。

    ```ts theme={null}
    const books = await client.fetchOrderBooks([
      { tokenId: yesTokenId },
      { tokenId: noTokenId },
    ]);

    // books: OrderBook[]
    ```

    每一项都采用上文所述的 `OrderBook` 结构：

    ```json theme={null}
    [
      {
        "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "bids": [
          { "price": "0.01", "size": "2116131.59" },
          { "price": "0.02", "size": "139963.89" },
          { "price": "0.03", "size": "208169.44" },
          "..."
        ],
        "asks": [
          { "price": "0.99", "size": "93442.27" },
          { "price": "0.98", "size": "13229.55" },
          { "price": "0.97", "size": "4338.7" },
          "..."
        ]
      },
      {
        "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "bids": [
          { "price": "0.01", "size": "93442.27" },
          { "price": "0.02", "size": "13229.55" },
          { "price": "0.03", "size": "4338.7" },
          "..."
        ],
        "asks": [
          { "price": "0.99", "size": "2116131.59" },
          { "price": "0.98", "size": "139963.89" },
          { "price": "0.97", "size": "208169.44" },
          "..."
        ]
      }
    ]
    ```
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_order_books()`，通过一次请求获取多个订单簿。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    books = await client.get_order_books(token_ids=[yes_token_id, no_token_id])

    # books: tuple[OrderBook, ...]
    ```

    每一项都采用上文所述的 `OrderBook` 结构：

    ```json theme={null}
    [
      {
        "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "bids": [
          { "price": "0.01", "size": "2116131.59" },
          { "price": "0.02", "size": "139963.89" },
          { "price": "0.03", "size": "208169.44" },
          "..."
        ],
        "asks": [
          { "price": "0.99", "size": "93442.27" },
          { "price": "0.98", "size": "13229.55" },
          { "price": "0.97", "size": "4338.7" },
          "..."
        ]
      },
      {
        "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "bids": [
          { "price": "0.01", "size": "93442.27" },
          { "price": "0.02", "size": "13229.55" },
          { "price": "0.03", "size": "4338.7" },
          "..."
        ],
        "asks": [
          { "price": "0.99", "size": "2116131.59" },
          { "price": "0.98", "size": "139963.89" },
          { "price": "0.97", "size": "208169.44" },
          "..."
        ]
      }
    ]
    ```
  </Tab>

  <Tab title="API">
    通过一次请求获取多个订单簿：

    ```bash theme={null}
    curl -X POST "https://clob.polymarket.com/books" \
      -H "Content-Type: application/json" \
      --data '[{"token_id":"<yes_token_id>"},{"token_id":"<no_token_id>"}]'
    ```

    响应会为每个请求的代币 ID 返回一个订单簿：

    ```json theme={null}
    [
      {
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "timestamp": "1782753357257",
        "hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "bids": [
          { "price": "0.01", "size": "2151131.59" },
          { "price": "0.02", "size": "139963.89" },
          { "price": "0.03", "size": "208169.44" },
          "..."
        ],
        "asks": [
          { "price": "0.99", "size": "218442.27" },
          { "price": "0.98", "size": "13229.55" },
          { "price": "0.97", "size": "4338.7" },
          "..."
        ],
        "min_order_size": "5",
        "tick_size": "0.01",
        "neg_risk": false,
        "last_trade_price": "0.090"
      },
      {
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "asset_id": "7305630249804085635496399869905769372294302716159034447326228509068694952392",
        "timestamp": "1782753357257",
        "hash": "f6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3b2a1f6e5",
        "bids": [
          { "price": "0.01", "size": "218442.27" },
          { "price": "0.02", "size": "13229.55" },
          { "price": "0.03", "size": "4338.7" },
          "..."
        ],
        "asks": [
          { "price": "0.99", "size": "2151131.59" },
          { "price": "0.98", "size": "139963.89" },
          { "price": "0.97", "size": "208169.44" },
          "..."
        ],
        "min_order_size": "5",
        "tick_size": "0.01",
        "neg_risk": true,
        "last_trade_price": "0.910"
      }
    ]
    ```
  </Tab>
</Tabs>

## 最优市场价格

读取某个结果和方向的最优可成交价格。`BUY` 返回最低卖价，即买入时需要支付的价格；`SELL` 返回最高买价，即卖出时可以获得的价格。

### 获取价格

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchPrice()`，获取某一方向的最优价格。对于 `BUY`，该方法返回最低卖价。

    ```ts theme={null}
    import { OrderSide } from "@polymarket/client";

    const price = await client.fetchPrice({
      tokenId: yesTokenId,
      side: OrderSide.BUY,
    });

    // price: DecimalString
    ```

    该方法以十进制字符串返回价格：

    ```json theme={null}
    "0.08"
    ```
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_price()`，获取某一方向的最优价格。对于 `BUY`，该方法返回最低卖价。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    price = await client.get_price(token_id=yes_token_id, side="BUY")

    # price: Decimal
    ```

    该方法以 `Decimal` 返回价格：

    ```python theme={null}
    Decimal("0.08")
    ```
  </Tab>

  <Tab title="API">
    获取买方可成交的最低卖价：

    ```bash theme={null}
    curl "https://clob.polymarket.com/price?token_id=$TOKEN_ID&side=BUY"
    ```

    响应包含以十进制字符串表示的价格：

    ```json theme={null}
    { "price": "0.08" }
    ```
  </Tab>
</Tabs>

### 获取多个价格

批量读取价格可在一次请求中返回多组“结果与方向”组合的最优市场价格。当同一视图或计算需要多个结果时，请使用此方式。

<Info>每次请求最多 500 项。</Info>

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchPrices()`，通过一次请求获取多个价格。

    ```ts theme={null}
    import { OrderSide } from "@polymarket/client";

    const prices = await client.fetchPrices([
      { tokenId: yesTokenId, side: OrderSide.BUY },
      { tokenId: noTokenId, side: OrderSide.BUY },
    ]);

    // prices: Prices (map of tokenId -> side -> price)
    ```

    `Prices` 将每个代币 ID 映射到请求的方向和价格：

    <CodeGroup>
      ```ts Prices Type theme={null}
      type Prices = Record<TokenId, Partial<Record<OrderSide, DecimalString>>>;
      ```

      ```json Prices Example theme={null}
      {
        "107505882767731489358349912513945399560393482969656700824895970500493757150417": {
          "BUY": "0.08"
        },
        "7305630249804085635496399869905769372294302716159034447326228509068694952392": {
          "BUY": "0.91"
        }
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_prices()`，通过一次请求获取多个价格。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    from polymarket import PriceRequest

    prices = await client.get_prices(
        requests=[
            PriceRequest(token_id=yes_token_id, side="BUY"),
            PriceRequest(token_id=no_token_id, side="BUY"),
        ],
    )

    # prices: dict[TokenId, dict[OrderSide, Decimal]]
    ```

    结果将每个代币 ID 映射到请求的方向和价格：

    ```json theme={null}
    {
      "107505882767731489358349912513945399560393482969656700824895970500493757150417": {
        "BUY": "0.08"
      },
      "7305630249804085635496399869905769372294302716159034447326228509068694952392": {
        "BUY": "0.91"
      }
    }
    ```
  </Tab>

  <Tab title="API">
    通过一次请求获取多个价格：

    ```bash theme={null}
    curl -X POST "https://clob.polymarket.com/prices" \
      -H "Content-Type: application/json" \
      --data '[{"token_id":"<yes_token_id>","side":"BUY"},{"token_id":"<no_token_id>","side":"BUY"}]'
    ```

    响应将每个代币 ID 映射到请求的方向和价格：

    ```json theme={null}
    {
      "107505882767731489358349912513945399560393482969656700824895970500493757150417": {
        "BUY": 0.08
      },
      "7305630249804085635496399869905769372294302716159034447326228509068694952392": {
        "BUY": 0.91
      }
    }
    ```
  </Tab>
</Tabs>

## 中间价

中间价是最优买价与最优卖价的平均值，用作订单簿买卖双方之间的参考价格。

### 获取中间价

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchMidpoint()`，获取中间价。

    ```ts theme={null}
    const midpoint = await client.fetchMidpoint({ tokenId: yesTokenId });

    // midpoint: DecimalString
    ```

    该方法以十进制字符串返回中间价：

    ```json theme={null}
    "0.085"
    ```
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_midpoint()`，获取中间价。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    midpoint = await client.get_midpoint(token_id=yes_token_id)

    # midpoint: Decimal
    ```

    该方法以 `Decimal` 返回中间价：

    ```python theme={null}
    Decimal("0.085")
    ```
  </Tab>

  <Tab title="API">
    获取中间价：

    ```bash theme={null}
    curl "https://clob.polymarket.com/midpoint?token_id=$TOKEN_ID"
    ```

    响应包含以十进制字符串表示的中间价：

    ```json theme={null}
    { "mid": "0.085" }
    ```
  </Tab>
</Tabs>

### 获取多个中间价

通过一次请求获取多个结果的中间价。

<Info>每次请求最多 500 项。</Info>

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchMidpoints()`，获取多个中间价。

    ```ts theme={null}
    const midpoints = await client.fetchMidpoints([
      { tokenId: yesTokenId },
      { tokenId: noTokenId },
    ]);

    // midpoints: Midpoints
    ```

    `Midpoints` 将每个代币 ID 映射到其中间价：

    <CodeGroup>
      ```ts Midpoints Type theme={null}
      type Midpoints = Record<TokenId, DecimalString>;
      ```

      ```json Midpoints Example theme={null}
      {
        "107505882767731489358349912513945399560393482969656700824895970500493757150417": "0.085",
        "7305630249804085635496399869905769372294302716159034447326228509068694952392": "0.915"
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_midpoints()`，获取多个中间价。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    midpoints = await client.get_midpoints(token_ids=[yes_token_id, no_token_id])

    # midpoints: dict[TokenId, Decimal]
    ```

    结果将每个代币 ID 映射到其中间价：

    ```json theme={null}
    {
      "107505882767731489358349912513945399560393482969656700824895970500493757150417": "0.085",
      "7305630249804085635496399869905769372294302716159034447326228509068694952392": "0.915"
    }
    ```
  </Tab>

  <Tab title="API">
    通过一次请求获取多个中间价：

    ```bash theme={null}
    curl -X POST "https://clob.polymarket.com/midpoints" \
      -H "Content-Type: application/json" \
      --data '[{"token_id":"<yes_token_id>"},{"token_id":"<no_token_id>"}]'
    ```

    响应将每个代币 ID 映射到其中间价：

    ```json theme={null}
    {
      "107505882767731489358349912513945399560393482969656700824895970500493757150417": "0.085",
      "7305630249804085635496399869905769372294302716159034447326228509068694952392": "0.915"
    }
    ```
  </Tab>
</Tabs>

## 买卖价差

买卖价差是最优卖价与最优买价之间的差值。价差越窄，表示订单簿买卖双方的价格越接近。

### 获取买卖价差

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchSpread()`，获取买卖价差。

    ```ts theme={null}
    const spread = await client.fetchSpread({ tokenId: yesTokenId });

    // spread: DecimalString
    ```

    该方法以十进制字符串返回买卖价差：

    ```json theme={null}
    "0.01"
    ```
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_spread()`，获取买卖价差。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    spread = await client.get_spread(token_id=yes_token_id)

    # spread: Decimal
    ```

    该方法以 `Decimal` 返回买卖价差：

    ```python theme={null}
    Decimal("0.01")
    ```
  </Tab>

  <Tab title="API">
    获取买卖价差：

    ```bash theme={null}
    curl "https://clob.polymarket.com/spread?token_id=$TOKEN_ID"
    ```

    响应包含以十进制字符串表示的买卖价差：

    ```json theme={null}
    { "spread": "0.01" }
    ```
  </Tab>
</Tabs>

### 获取多个买卖价差

批量读取价差可在一次请求中返回多个结果的买卖价差。

<Info>每次请求最多 500 项。</Info>

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchSpreads()`，通过一次请求获取多个买卖价差。

    ```ts theme={null}
    const spreads = await client.fetchSpreads([
      { tokenId: yesTokenId },
      { tokenId: noTokenId },
    ]);

    // spreads: Spreads
    ```

    结果将每个代币 ID 映射到其买卖价差：

    <CodeGroup>
      ```ts Spreads Type theme={null}
      type Spreads = Record<TokenId, DecimalString>;
      ```

      ```json Spreads Example theme={null}
      {
        "107505882767731489358349912513945399560393482969656700824895970500493757150417": "0.01",
        "7305630249804085635496399869905769372294302716159034447326228509068694952392": "0.02"
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_spreads()`，通过一次请求获取多个买卖价差。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    spreads = await client.get_spreads(token_ids=[yes_token_id, no_token_id])

    # spreads: dict[TokenId, Decimal]
    ```

    结果将每个代币 ID 映射到其买卖价差：

    ```json theme={null}
    {
      "107505882767731489358349912513945399560393482969656700824895970500493757150417": "0.01",
      "7305630249804085635496399869905769372294302716159034447326228509068694952392": "0.02"
    }
    ```
  </Tab>

  <Tab title="API">
    通过一次请求获取多个买卖价差：

    ```bash theme={null}
    curl -X POST "https://clob.polymarket.com/spreads" \
      -H "Content-Type: application/json" \
      --data '[{"token_id":"<yes_token_id>"},{"token_id":"<no_token_id>"}]'
    ```

    响应将每个代币 ID 映射到其买卖价差：

    ```json theme={null}
    {
      "107505882767731489358349912513945399560393482969656700824895970500493757150417": "0.01",
      "7305630249804085635496399869905769372294302716159034447326228509068694952392": "0.02"
    }
    ```
  </Tab>
</Tabs>

## 最新成交价

最新成交价记录某个结果最近一次撮合成交的信息，包括成交价格和方向。

### 获取最新成交价

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchLastTradePrice()`，获取最新成交记录。

    ```ts theme={null}
    const lastTrade = await client.fetchLastTradePrice({ tokenId: yesTokenId });

    // lastTrade: LastTradePrice
    ```

    `LastTradePrice` 将成交价格与订单方向组合在一起：

    <CodeGroup>
      ```ts LastTradePrice Type theme={null}
      type LastTradePrice = {
        price: DecimalString;
        side: OrderSide;
      };
      ```

      ```json LastTradePrice Example theme={null}
      {
        "price": "0.08",
        "side": "SELL"
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_last_trade_price()`，获取最新成交记录。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    last_trade = await client.get_last_trade_price(token_id=yes_token_id)

    # last_trade: LastTradePrice
    ```

    `LastTradePrice` 将成交价格与订单方向组合在一起：

    <CodeGroup>
      ```python LastTradePrice Type theme={null}
      class LastTradePrice:
          price: Decimal
          side: OrderSide
      ```

      ```json LastTradePrice Example theme={null}
      {
        "price": "0.08",
        "side": "SELL"
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    获取最新成交记录：

    ```bash theme={null}
    curl "https://clob.polymarket.com/last-trade-price?token_id=$TOKEN_ID"
    ```

    响应包含成交价格和方向：

    ```json theme={null}
    {
      "price": "0.08",
      "side": "SELL"
    }
    ```
  </Tab>
</Tabs>

### 获取多个最新成交价

通过一次请求获取多个结果最近一次撮合成交的信息。

<Info>每次请求最多 500 项。</Info>

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchLastTradePrices()`，获取多条最新成交记录。

    ```ts theme={null}
    const lastTrades = await client.fetchLastTradePrices([
      { tokenId: yesTokenId },
      { tokenId: noTokenId },
    ]);

    // lastTrades: LastTradePriceForToken[]
    ```

    每个 `LastTradePriceForToken` 都标识相应结果及其成交价格和方向：

    <CodeGroup>
      ```ts LastTradePriceForToken Type theme={null}
      type LastTradePriceForToken = {
        tokenId: TokenId;
        price: DecimalString;
        side: OrderSide;
      };
      ```

      ```json LastTradePriceForToken Example theme={null}
      [
        {
          "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "price": "0.08",
          "side": "SELL"
        },
        {
          "tokenId": "7305630249804085635496399869905769372294302716159034447326228509068694952392",
          "price": "0.91",
          "side": "BUY"
        }
      ]
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_last_trade_prices()`，获取多条最新成交记录。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    last_trades = await client.get_last_trade_prices(
        token_ids=[yes_token_id, no_token_id]
    )

    # last_trades: tuple[LastTradePriceForToken, ...]
    ```

    每个 `LastTradePriceForToken` 都标识相应结果及其成交价格和方向：

    <CodeGroup>
      ```python LastTradePriceForToken Type theme={null}
      class LastTradePriceForToken:
          token_id: TokenId
          price: Decimal
          side: OrderSide
      ```

      ```json LastTradePriceForToken Example theme={null}
      [
        {
          "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "price": "0.08",
          "side": "SELL"
        },
        {
          "token_id": "7305630249804085635496399869905769372294302716159034447326228509068694952392",
          "price": "0.91",
          "side": "BUY"
        }
      ]
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    通过一次请求获取多个最新成交价：

    ```bash theme={null}
    curl -X POST "https://clob.polymarket.com/last-trades-prices" \
      -H "Content-Type: application/json" \
      --data '[{"token_id":"<yes_token_id>"},{"token_id":"<no_token_id>"}]'
    ```

    响应会标识每个结果及其成交价格和方向：

    ```json theme={null}
    [
      {
        "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "price": "0.08",
        "side": "SELL"
      },
      {
        "token_id": "7305630249804085635496399869905769372294302716159034447326228509068694952392",
        "price": "0.91",
        "side": "BUY"
      }
    ]
    ```
  </Tab>
</Tabs>

## 价格历史

价格历史是某个结果的一组观测价格时间序列。你可以获取相对时间窗口或绝对时间范围，但不能同时使用这两种形式。还可以控制返回观测值之间的间隔；时间范围较长时必须设置采样间隔。

| 范围设置   | 行为                                               |
| ------ | ------------------------------------------------ |
| 相对时间窗口 | 获取最近 `1h`、`6h`、`1d`、`1w` 的数据，或使用 `max` 获取所有可用数据。 |
| 绝对时间范围 | 获取以 Unix 秒表示的开始时间与结束时间之间的观测值。                    |
| 采样间隔   | 以分钟为单位设置观测值之间的间隔。                                |

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchPriceHistory()`，获取历史价格。设置 `interval` 可指定相对时间窗口；设置 `startTs` 和 `endTs` 可指定绝对时间范围。使用 `fidelity` 以分钟为单位设置采样间隔。

    <CodeGroup>
      ```ts Relative Window theme={null}
      const history = await client.fetchPriceHistory({
        tokenId: yesTokenId,
        interval: "1d",
        fidelity: 60,
      });

      // history: PriceHistoryPoint[]
      ```

      ```ts Absolute Range theme={null}
      const history = await client.fetchPriceHistory({
        tokenId: yesTokenId,
        startTs: 1782666000,
        endTs: 1782676800,
        fidelity: 60,
      });

      // history: PriceHistoryPoint[]
      ```
    </CodeGroup>

    每个 `PriceHistoryPoint` 都包含时间戳及其观测价格：

    <CodeGroup>
      ```ts PriceHistoryPoint Type theme={null}
      type PriceHistoryPoint = {
        t: number;
        p: number;
      };
      ```

      ```json PriceHistoryPoint Example theme={null}
      [
        { "t": 1782666007, "p": 0.085 },
        { "t": 1782669606, "p": 0.085 },
        { "t": 1782673206, "p": 0.085 },
        "..."
      ]
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_price_history()`，获取历史价格。设置 `interval` 可指定相对时间窗口；设置 `start_ts` 和 `end_ts` 可指定绝对时间范围。使用 `fidelity` 以分钟为单位设置采样间隔。同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    <CodeGroup>
      ```python Relative Window theme={null}
      history = await client.get_price_history(
          token_id=yes_token_id,
          interval="1d",
          fidelity=60,
      )

      # history: tuple[PriceHistoryPoint, ...]
      ```

      ```python Absolute Range theme={null}
      history = await client.get_price_history(
          token_id=yes_token_id,
          start_ts=1782666000,
          end_ts=1782676800,
          fidelity=60,
      )

      # history: tuple[PriceHistoryPoint, ...]
      ```
    </CodeGroup>

    每个 `PriceHistoryPoint` 都包含时间戳及其观测价格：

    <CodeGroup>
      ```python PriceHistoryPoint Type theme={null}
      class PriceHistoryPoint:
          t: int
          p: float
      ```

      ```json PriceHistoryPoint Example theme={null}
      [
        { "t": 1782666007, "p": 0.085 },
        { "t": 1782669606, "p": 0.085 },
        { "t": 1782673206, "p": 0.085 },
        "..."
      ]
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    设置 `interval` 可指定相对时间窗口；设置 `startTs` 和 `endTs` 可指定绝对时间范围。使用 `fidelity` 以分钟为单位设置采样间隔。

    <CodeGroup>
      ```bash Relative Window theme={null}
      curl "https://clob.polymarket.com/prices-history?market=$TOKEN_ID&interval=1d&fidelity=60"
      ```

      ```bash Absolute Range theme={null}
      curl "https://clob.polymarket.com/prices-history?market=$TOKEN_ID&startTs=1782666000&endTs=1782676800&fidelity=60"
      ```
    </CodeGroup>

    响应的 `history` 字段包含价格点：

    ```json theme={null}
    {
      "history": [
        { "t": 1782666007, "p": 0.085 },
        { "t": 1782669606, "p": 0.085 },
        { "t": 1782673206, "p": 0.085 },
        "..."
      ]
    }
    ```
  </Tab>
</Tabs>
