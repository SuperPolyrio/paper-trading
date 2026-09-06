> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 实时数据

> 通过 Polymarket 的实时数据流持续更新市场数据。

使用 Polymarket 的实时数据流，在市场状况和相关数据发生变化时及时响应。
这些数据流无需重复轮询，即可让应用所依赖的公开市场数据保持最新。
如需经过身份验证的订单和交易更新，请参阅
[实时订单更新](/cn/trading/realtime-order-updates)。

## 市场数据流

使用市场数据流，让应用与市场订单簿和交易状态的变化保持同步。

<Tabs>
  <Tab title="TypeScript">
    使用 `PublicClient` 或 `SecureClient`，通过一个或多个代币 ID 订阅
    `market` 主题：

    ```ts theme={null}
    const tokenId = "<token_id>";

    const stream = await client.subscribe([
      {
        topic: "market",
        tokenIds: [tokenId],
      },
    ]);

    for await (const event of stream) {
      switch (event.type) {
        case "book":
          // event: MarketBookEvent
          break;
        case "price_change":
          // event: MarketPriceChangeEvent
          break;
        case "last_trade_price":
          // event: MarketLastTradePriceEvent
          break;
        case "tick_size_change":
          // event: MarketTickSizeChangeEvent
          break;
      }
    }
    ```

    <Accordion title="标准市场事件">
      #### 订单簿

      <CodeGroup>
        ```ts MarketBookEvent Type theme={null}
        type OrderBookLevel = {
          price: DecimalString;
          size: DecimalString;
        };

        type MarketBookEvent = {
          topic: "market";
          type: "book";
          payload: {
            market: string;
            tokenId: TokenId;
            bids: OrderBookLevel[];
            asks: OrderBookLevel[];
            hash?: string | null;
            timestamp?: string | null;
            minOrderSize?: DecimalString | null;
            tickSize?: DecimalString | null;
            negRisk?: boolean | null;
            lastTradePrice?: DecimalString | null;
          };
        };
        ```

        ```json MarketBookEvent Example theme={null}
        {
          "topic": "market",
          "type": "book",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "bids": [{ "price": "0.08", "size": "33343.4" }],
            "asks": [{ "price": "0.09", "size": "163939.58" }],
            "hash": "0xabc123…",
            "timestamp": "1782753357257"
          }
        }
        ```
      </CodeGroup>

      #### 价格变化

      <CodeGroup>
        ```ts MarketPriceChangeEvent Type theme={null}
        type PriceChange = {
          tokenId: TokenId;
          price: DecimalString;
          size: DecimalString;
          side: OrderSide;
          hash?: string | null;
          bestBid?: DecimalString | null;
          bestAsk?: DecimalString | null;
        };

        type MarketPriceChangeEvent = {
          topic: "market";
          type: "price_change";
          payload: {
            market: string;
            priceChanges: PriceChange[];
            timestamp?: string | null;
          };
        };
        ```

        ```json MarketPriceChangeEvent Example theme={null}
        {
          "topic": "market",
          "type": "price_change",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "priceChanges": [
              {
                "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
                "price": "0.08",
                "size": "33343.4",
                "side": "BUY",
                "hash": "56621a121a47ed9333273e21c83b660cff37ae50",
                "bestBid": "0.08",
                "bestAsk": "0.09"
              }
            ],
            "timestamp": "1782753357257"
          }
        }
        ```
      </CodeGroup>

      #### 最新成交价

      <CodeGroup>
        ```ts MarketLastTradePriceEvent Type theme={null}
        type MarketLastTradePriceEvent = {
          topic: "market";
          type: "last_trade_price";
          payload: {
            market: string;
            tokenId: TokenId;
            price: DecimalString;
            size?: DecimalString | null;
            feeRateBps?: DecimalString | null;
            side: OrderSide;
            timestamp?: string | null;
            transactionHash?: string | null;
          };
        };
        ```

        ```json MarketLastTradePriceEvent Example theme={null}
        {
          "topic": "market",
          "type": "last_trade_price",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "price": "0.08",
            "size": "219.217767",
            "feeRateBps": "0",
            "side": "SELL",
            "timestamp": "1782753357257",
            "transactionHash": "0xeeefff…"
          }
        }
        ```
      </CodeGroup>

      #### 最小价格变动单位变化

      <CodeGroup>
        ```ts MarketTickSizeChangeEvent Type theme={null}
        type MarketTickSizeChangeEvent = {
          topic: "market";
          type: "tick_size_change";
          payload: {
            market: string;
            tokenId: TokenId;
            oldTickSize?: DecimalString | null;
            newTickSize: DecimalString;
            timestamp?: string | null;
          };
        };
        ```

        ```json MarketTickSizeChangeEvent Example theme={null}
        {
          "topic": "market",
          "type": "tick_size_change",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "oldTickSize": "0.01",
            "newTickSize": "0.001",
            "timestamp": "1782753357257"
          }
        }
        ```
      </CodeGroup>
    </Accordion>

    启用 `customFeatureEnabled`，以接收最优买卖价和市场生命周期更新：

    ```ts theme={null}
    const stream = await client.subscribe([
      {
        topic: "market",
        tokenIds: [tokenId],
        customFeatureEnabled: true,
      },
    ]);

    for await (const event of stream) {
      switch (event.type) {
        case "book":
          // event: MarketBookEvent
          break;
        case "price_change":
          // event: MarketPriceChangeEvent
          break;
        case "last_trade_price":
          // event: MarketLastTradePriceEvent
          break;
        case "tick_size_change":
          // event: MarketTickSizeChangeEvent
          break;
        case "best_bid_ask":
          // event: MarketBestBidAskEvent
          break;
        case "new_market":
          // event: NewMarketEvent
          break;
        case "market_resolved":
          // event: MarketResolvedEvent
          break;
      }
    }
    ```

    <Accordion title="其他市场事件">
      #### 最优买价和卖价

      <CodeGroup>
        ```ts MarketBestBidAskEvent Type theme={null}
        type MarketBestBidAskEvent = {
          topic: "market";
          type: "best_bid_ask";
          payload: {
            market: string;
            tokenId: TokenId;
            bestBid?: DecimalString | null;
            bestAsk?: DecimalString | null;
            spread?: DecimalString | null;
            timestamp?: string | null;
          };
        };
        ```

        ```json MarketBestBidAskEvent Example theme={null}
        {
          "topic": "market",
          "type": "best_bid_ask",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "bestBid": "0.08",
            "bestAsk": "0.09",
            "spread": "0.01",
            "timestamp": "1782753357257"
          }
        }
        ```
      </CodeGroup>

      #### 新市场

      <CodeGroup>
        ```ts NewMarketEvent Type theme={null}
        type NewMarketEvent = {
          topic: "market";
          type: "new_market";
          payload: {
            id: string;
            question?: string | null;
            market: string;
            slug?: string | null;
            description?: string | null;
            tokenIds?: TokenId[] | null;
            outcomes?: string[] | null;
            eventMessage?: {
              id: string;
              ticker?: string | null;
              slug?: string | null;
              title?: string | null;
              description?: string | null;
            } | null;
            timestamp?: string | null;
            tags?: string[] | null;
            conditionId?: CtfConditionId | null;
            active?: boolean | null;
            clobTokenIds?: string[] | null;
            sportsMarketType?: string | null;
            line?: DecimalString | null;
            gameStartTime?: IsoDateTimeString | null;
            orderPriceMinTickSize?: DecimalString | null;
            groupItemTitle?: string | null;
            takerBaseFee?: DecimalString | null;
            feesEnabled?: boolean | null;
            feeSchedule?: unknown;
          };
        };
        ```

        ```json NewMarketEvent Example theme={null}
        {
          "topic": "market",
          "type": "new_market",
          "payload": {
            "id": "123456",
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "question": "Will the US confirm that aliens exist before 2027?",
            "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "tokenIds": [
              "107505882767731489358349912513945399560393482969656700824895970500493757150417",
              "7305630249804085635496399869905769372294302716159034447326228509068694952392"
            ],
            "outcomes": ["Yes", "No"],
            "active": true,
            "timestamp": "1782753357257"
          }
        }
        ```
      </CodeGroup>

      #### 市场已结算

      <CodeGroup>
        ```ts MarketResolvedEvent Type theme={null}
        type MarketResolvedEvent = {
          topic: "market";
          type: "market_resolved";
          payload: {
            id: string;
            market: string;
            tokenIds?: TokenId[] | null;
            winningTokenId?: TokenId | null;
            winningOutcome?: string | null;
            eventMessage?: {
              id: string;
              ticker?: string | null;
              slug?: string | null;
              title?: string | null;
              description?: string | null;
            } | null;
            timestamp?: string | null;
            tags?: string[] | null;
          };
        };
        ```

        ```json MarketResolvedEvent Example theme={null}
        {
          "topic": "market",
          "type": "market_resolved",
          "payload": {
            "id": "123456",
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "tokenIds": [
              "107505882767731489358349912513945399560393482969656700824895970500493757150417",
              "7305630249804085635496399869905769372294302716159034447326228509068694952392"
            ],
            "winningTokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "winningOutcome": "Yes",
            "timestamp": "1782753357257"
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    使用 `AsyncPublicClient` 或 `AsyncSecureClient`，通过包含一个或多个代币
    ID 的 `MarketSpec` 订阅市场数据流：

    ```python theme={null}
    from polymarket.streams import MarketSpec

    token_id = "<token_id>"

    async with await client.subscribe(
        MarketSpec(token_ids=[token_id]),
    ) as stream:
        async for event in stream:
            if event.type == "book":
                ...  # event: MarketBookEvent
            elif event.type == "price_change":
                ...  # event: MarketPriceChangeEvent
            elif event.type == "last_trade_price":
                ...  # event: MarketLastTradePriceEvent
            elif event.type == "tick_size_change":
                ...  # event: MarketTickSizeChangeEvent
    ```

    <Accordion title="标准市场事件">
      #### 订单簿

      <CodeGroup>
        ```python MarketBookEvent Type theme={null}
        class OrderBookLevel:
            price: Decimal
            size: Decimal

        class MarketBookPayload:
            market: str
            token_id: TokenId
            bids: tuple[OrderBookLevel, ...]
            asks: tuple[OrderBookLevel, ...]
            hash: str | None
            timestamp: datetime | None
            min_order_size: Decimal | None
            tick_size: Decimal | None
            neg_risk: bool | None
            last_trade_price: Decimal | None

        class MarketBookEvent:
            topic: Literal["market"]
            type: Literal["book"]
            payload: MarketBookPayload
        ```

        ```json MarketBookEvent Example theme={null}
        {
          "topic": "market",
          "type": "book",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "bids": [{ "price": "0.08", "size": "33343.4" }],
            "asks": [{ "price": "0.09", "size": "163939.58" }],
            "hash": "0xabc123…",
            "timestamp": "2026-06-29T17:15:57.257000Z"
          }
        }
        ```
      </CodeGroup>

      #### 价格变化

      <CodeGroup>
        ```python MarketPriceChangeEvent Type theme={null}
        class PriceChange:
            token_id: TokenId
            price: Decimal
            size: Decimal
            side: Literal["BUY", "SELL"]
            hash: str | None
            best_bid: Decimal | None
            best_ask: Decimal | None

        class MarketPriceChangePayload:
            market: str
            price_changes: tuple[PriceChange, ...]
            timestamp: datetime | None

        class MarketPriceChangeEvent:
            topic: Literal["market"]
            type: Literal["price_change"]
            payload: MarketPriceChangePayload
        ```

        ```json MarketPriceChangeEvent Example theme={null}
        {
          "topic": "market",
          "type": "price_change",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "price_changes": [
              {
                "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
                "price": "0.08",
                "size": "33343.4",
                "side": "BUY",
                "hash": "56621a121a47ed9333273e21c83b660cff37ae50",
                "best_bid": "0.08",
                "best_ask": "0.09"
              }
            ],
            "timestamp": "2026-06-29T17:15:57.257000Z"
          }
        }
        ```
      </CodeGroup>

      #### 最新成交价

      <CodeGroup>
        ```python MarketLastTradePriceEvent Type theme={null}
        class MarketLastTradePricePayload:
            market: str
            token_id: TokenId
            price: Decimal
            size: Decimal | None
            side: Literal["BUY", "SELL"]
            fee_rate_bps: Decimal | None
            transaction_hash: str | None
            timestamp: datetime | None

        class MarketLastTradePriceEvent:
            topic: Literal["market"]
            type: Literal["last_trade_price"]
            payload: MarketLastTradePricePayload
        ```

        ```json MarketLastTradePriceEvent Example theme={null}
        {
          "topic": "market",
          "type": "last_trade_price",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "price": "0.08",
            "size": "219.217767",
            "side": "SELL",
            "fee_rate_bps": "0",
            "transaction_hash": "0xeeefff…",
            "timestamp": "2026-06-29T17:15:57.257000Z"
          }
        }
        ```
      </CodeGroup>

      #### 最小价格变动单位变化

      <CodeGroup>
        ```python MarketTickSizeChangeEvent Type theme={null}
        class MarketTickSizeChangePayload:
            market: str
            token_id: TokenId
            old_tick_size: Decimal | None
            new_tick_size: Decimal
            timestamp: datetime | None

        class MarketTickSizeChangeEvent:
            topic: Literal["market"]
            type: Literal["tick_size_change"]
            payload: MarketTickSizeChangePayload
        ```

        ```json MarketTickSizeChangeEvent Example theme={null}
        {
          "topic": "market",
          "type": "tick_size_change",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "old_tick_size": "0.01",
            "new_tick_size": "0.001",
            "timestamp": "2026-06-29T17:15:57.257000Z"
          }
        }
        ```
      </CodeGroup>
    </Accordion>

    启用 `custom_feature_enabled`，以接收最优买卖价和市场生命周期更新：

    ```python theme={null}
    async with await client.subscribe(
        MarketSpec(token_ids=[token_id], custom_feature_enabled=True),
    ) as stream:
        async for event in stream:
            if event.type == "book":
                ...  # event: MarketBookEvent
            elif event.type == "price_change":
                ...  # event: MarketPriceChangeEvent
            elif event.type == "last_trade_price":
                ...  # event: MarketLastTradePriceEvent
            elif event.type == "tick_size_change":
                ...  # event: MarketTickSizeChangeEvent
            elif event.type == "best_bid_ask":
                ...  # event: MarketBestBidAskEvent
            elif event.type == "new_market":
                ...  # event: NewMarketEvent
            elif event.type == "market_resolved":
                ...  # event: MarketResolvedEvent
    ```

    <Accordion title="其他市场事件">
      #### 最优买价和卖价

      <CodeGroup>
        ```python MarketBestBidAskEvent Type theme={null}
        class MarketBestBidAskPayload:
            market: str
            token_id: TokenId
            best_bid: Decimal | None
            best_ask: Decimal | None
            spread: Decimal | None
            timestamp: datetime | None

        class MarketBestBidAskEvent:
            topic: Literal["market"]
            type: Literal["best_bid_ask"]
            payload: MarketBestBidAskPayload
        ```

        ```json MarketBestBidAskEvent Example theme={null}
        {
          "topic": "market",
          "type": "best_bid_ask",
          "payload": {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "best_bid": "0.08",
            "best_ask": "0.09",
            "spread": "0.01",
            "timestamp": "2026-06-29T17:15:57.257000Z"
          }
        }
        ```
      </CodeGroup>

      #### 新市场

      <CodeGroup>
        ```python NewMarketEvent Type theme={null}
        class MarketEventMessage:
            id: str
            ticker: str | None
            slug: str | None
            title: str | None
            description: str | None

        class NewMarketPayload:
            id: str
            market: str
            question: str | None
            slug: str | None
            description: str | None
            token_ids: tuple[TokenId, ...] | None
            outcomes: tuple[str, ...] | None
            event_message: MarketEventMessage | None
            timestamp: datetime | None
            tags: tuple[str, ...] | None
            condition_id: CtfConditionId | None
            active: bool | None
            clob_token_ids: tuple[str, ...] | None
            sports_market_type: str | None
            line: Decimal | None
            game_start_time: datetime | None
            order_price_min_tick_size: Decimal | None
            group_item_title: str | None
            taker_base_fee: Decimal | None
            fees_enabled: bool | None
            fee_schedule: object | None

        class NewMarketEvent:
            topic: Literal["market"]
            type: Literal["new_market"]
            payload: NewMarketPayload
        ```

        ```json NewMarketEvent Example theme={null}
        {
          "topic": "market",
          "type": "new_market",
          "payload": {
            "id": "123456",
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "question": "Will the US confirm that aliens exist before 2027?",
            "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "token_ids": [
              "107505882767731489358349912513945399560393482969656700824895970500493757150417",
              "7305630249804085635496399869905769372294302716159034447326228509068694952392"
            ],
            "outcomes": ["Yes", "No"],
            "active": true,
            "timestamp": "2026-06-29T17:15:57.257000Z"
          }
        }
        ```
      </CodeGroup>

      #### 市场已结算

      <CodeGroup>
        ```python MarketResolvedEvent Type theme={null}
        class MarketResolvedPayload:
            id: str
            market: str
            token_ids: tuple[TokenId, ...] | None
            winning_token_id: TokenId | None
            winning_outcome: str | None
            event_message: MarketEventMessage | None
            timestamp: datetime | None
            tags: tuple[str, ...] | None

        class MarketResolvedEvent:
            topic: Literal["market"]
            type: Literal["market_resolved"]
            payload: MarketResolvedPayload
        ```

        ```json MarketResolvedEvent Example theme={null}
        {
          "topic": "market",
          "type": "market_resolved",
          "payload": {
            "id": "123456",
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "token_ids": [
              "107505882767731489358349912513945399560393482969656700824895970500493757150417",
              "7305630249804085635496399869905769372294302716159034447326228509068694952392"
            ],
            "winning_token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "winning_outcome": "Yes",
            "timestamp": "2026-06-29T17:15:57.257000Z"
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    连接市场 WebSocket：

    ```text theme={null}
    wss://ws-subscriptions-clob.polymarket.com/ws/market
    ```

    <Note>
      市场 WebSocket 使用应用层心跳。每 10 秒发送一次文本帧 `PING`； 服务器会回复
      `PONG`。
    </Note>

    连接后，发送包含一个或多个代币 ID 的 `market` 订阅帧：

    ```json theme={null}
    {
      "assets_ids": ["<token_id>"],
      "type": "market"
    }
    ```

    <Accordion title="标准市场事件">
      #### 订单簿

      ```json theme={null}
      {
        "event_type": "book",
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "timestamp": "1782753357257",
        "hash": "0xabc123…",
        "bids": [
          { "price": "0.08", "size": "33343.4" },
          { "price": "0.09", "size": "163939.58" }
        ],
        "asks": [
          { "price": "0.99", "size": "218442.27" },
          { "price": "0.98", "size": "13229.55" }
        ]
      }
      ```

      #### 价格变化

      ```json theme={null}
      {
        "event_type": "price_change",
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "price_changes": [
          {
            "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "price": "0.08",
            "size": "33343.4",
            "side": "BUY",
            "hash": "56621a121a47ed9333273e21c83b660cff37ae50",
            "best_bid": "0.08",
            "best_ask": "0.09"
          }
        ],
        "timestamp": "1782753357257"
      }
      ```

      #### 最新成交价

      ```json theme={null}
      {
        "event_type": "last_trade_price",
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "price": "0.08",
        "size": "219.217767",
        "fee_rate_bps": "0",
        "side": "SELL",
        "timestamp": "1782753357257",
        "transaction_hash": "0xeeefff…"
      }
      ```

      #### 最小价格变动单位变化

      ```json theme={null}
      {
        "event_type": "tick_size_change",
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "old_tick_size": "0.01",
        "new_tick_size": "0.001",
        "timestamp": "1782753357257"
      }
      ```
    </Accordion>

    启用 `custom_feature_enabled`，以接收最优买卖价和市场生命周期更新：

    ```json theme={null}
    {
      "assets_ids": ["<token_id>"],
      "type": "market",
      "custom_feature_enabled": true
    }
    ```

    <Accordion title="其他市场事件">
      #### 最优买价和卖价

      ```json theme={null}
      {
        "event_type": "best_bid_ask",
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "best_bid": "0.08",
        "best_ask": "0.09",
        "spread": "0.01",
        "timestamp": "1782753357257"
      }
      ```

      #### 新市场

      ```json theme={null}
      {
        "event_type": "new_market",
        "id": "123456",
        "question": "Will the US confirm that aliens exist before 2027?",
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
        "assets_ids": [
          "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "7305630249804085635496399869905769372294302716159034447326228509068694952392"
        ],
        "outcomes": ["Yes", "No"],
        "timestamp": "1782753357257"
      }
      ```

      #### 市场已结算

      ```json theme={null}
      {
        "event_type": "market_resolved",
        "id": "123456",
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "assets_ids": [
          "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "7305630249804085635496399869905769372294302716159034447326228509068694952392"
        ],
        "winning_asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "winning_outcome": "Yes",
        "timestamp": "1782753357257"
      }
      ```
    </Accordion>

    无需建立新连接，即可添加或移除代币 ID：

    <CodeGroup>
      ```json Subscribe theme={null}
      {
        "assets_ids": ["<new_token_id>"],
        "operation": "subscribe"
      }
      ```

      ```json Unsubscribe theme={null}
      {
        "assets_ids": ["<old_token_id>"],
        "operation": "unsubscribe"
      }
      ```
    </CodeGroup>

    这些帧只会更新当前市场数据流连接的代币集合。
  </Tab>
</Tabs>

## 体育数据流

使用体育数据流，让实时比赛信息与体育市场保持同步。比赛开始直播、比分或
节次发生变化以及比赛结束时，都会收到更新。NFL 和 CFB 的更新还可能反映
球权变化。

<Warning>
  体育数据仅供参考，可能存在延迟、错误或遗漏近期事件。Polymarket 不提供
  交易或投资建议，不应将这些数据作为交易决策的依据。
</Warning>

<Tabs>
  <Tab title="TypeScript">
    使用 `PublicClient` 或 `SecureClient`，订阅 `sports` 主题以接收所有
    比赛更新：

    ```ts theme={null}
    const stream = await client.subscribe([{ topic: "sports" }]);

    for await (const event of stream) {
      // event: SportsEvent
    }
    ```

    <Accordion title="体育事件">
      <CodeGroup>
        ```ts SportsEvent Type theme={null}
        type SportsEvent = {
          topic: "sports";
          type: "sport_result";
          payload: {
            gameId: number;
            sportradarGameId?: string | null;
            slug?: string | null;
            leagueAbbreviation: string;
            homeTeam?: string | null;
            awayTeam?: string | null;
            status: string;
            live: boolean;
            ended: boolean;
            score: string;
            period?: string | null;
            elapsed?: string | null;
            finishedAt?: IsoDateTimeString | null;
            turn?: string | null;
          };
        };
        ```

        ```json SportsEvent Example theme={null}
        {
          "topic": "sports",
          "type": "sport_result",
          "payload": {
            "gameId": 5127839,
            "leagueAbbreviation": "NBA",
            "homeTeam": "Los Angeles Lakers",
            "awayTeam": "Boston Celtics",
            "status": "InProgress",
            "live": true,
            "ended": false,
            "score": "98-94",
            "period": "Q4",
            "elapsed": "05:12"
          }
        }
        ```
      </CodeGroup>

      `score` 是组合后的 `"<home>-<away>"` 字符串，而不是主队和客队两个
      独立字段。
    </Accordion>
  </Tab>

  <Tab title="Python">
    使用 `AsyncPublicClient` 或 `AsyncSecureClient`，通过 `SportsSpec`
    订阅以接收所有比赛更新：

    ```python theme={null}
    from polymarket.streams import SportsSpec

    async with await client.subscribe(SportsSpec()) as stream:
        async for event in stream:
            ...  # event: SportsEvent
    ```

    <Accordion title="体育事件">
      <CodeGroup>
        ```python SportsEvent Type theme={null}
        class SportsGameResult:
            game_id: int
            sportradar_game_id: str | None
            slug: str | None
            league_abbreviation: str
            home_team: str | None
            away_team: str | None
            status: str
            live: bool
            ended: bool
            score: str
            period: str | None
            elapsed: str | None
            finished_at: datetime | None
            turn: str | None

        class SportsResultEvent:
            topic: Literal["sports"]
            type: Literal["sport_result"]
            payload: SportsGameResult

        SportsEvent = SportsResultEvent
        ```

        ```json SportsEvent Example theme={null}
        {
          "topic": "sports",
          "type": "sport_result",
          "payload": {
            "game_id": 5127839,
            "league_abbreviation": "NBA",
            "home_team": "Los Angeles Lakers",
            "away_team": "Boston Celtics",
            "status": "InProgress",
            "live": true,
            "ended": false,
            "score": "98-94",
            "period": "Q4",
            "elapsed": "05:12"
          }
        }
        ```
      </CodeGroup>

      `score` 是组合后的 `"<home>-<away>"` 字符串，而不是主队和客队两个
      独立字段。
    </Accordion>
  </Tab>

  <Tab title="API">
    连接体育 WebSocket：

    ```text theme={null}
    wss://sports-api.polymarket.com/ws
    ```

    <Note>
      体育 WebSocket 使用应用层心跳。服务器每 5 秒发送一次文本帧 `ping`； 请在 10
      秒内回复 `pong`，否则服务器会关闭连接。
    </Note>

    连接后，服务器会开始推送所有比赛更新，无需发送订阅帧。

    <Accordion title="体育事件">
      ```json theme={null}
      {
        "gameId": 5127839,
        "leagueAbbreviation": "NBA",
        "homeTeam": "Los Angeles Lakers",
        "awayTeam": "Boston Celtics",
        "status": "InProgress",
        "live": true,
        "ended": false,
        "score": "98-94",
        "period": "Q4",
        "elapsed": "05:12"
      }
      ```

      体育消息没有封装层或事件类型字段。每条消息本身就是比赛更新对象，
      `score` 是组合后的 `"<home>-<away>"` 字符串。
    </Accordion>
  </Tab>
</Tabs>

### 节次值

节次的含义和格式因运动项目而异：

| 值                      | 含义            |
| ---------------------- | ------------- |
| `1H`, `2H`             | 上半场或下半场       |
| `1Q`, `2Q`, `3Q`, `4Q` | 节             |
| `HT`                   | 中场休息          |
| `FT`                   | 常规时间结束        |
| `FT OT`                | 加时赛后全场结束      |
| `FT NR`                | 全场结束，无结果      |
| `End 1`, `End 2`, …    | MLB 某局结束      |
| `1/3`, `2/3`, `3/3`    | 三局两胜系列赛中的地图编号 |
| `1/5`, `2/5`, …        | 五局三胜系列赛中的地图编号 |

### 比赛状态值

状态值因运动项目而异，且区分大小写：

| 运动项目      | 值                                                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------------------------------ |
| NFL       | `Scheduled`, `InProgress`, `Final`, `F/OT`, `Suspended`, `Postponed`, `Delayed`, `Canceled`, `Forfeit`, `NotNecessary`         |
| NHL       | `Scheduled`, `InProgress`, `Final`, `F/OT`, `F/SO`, `Suspended`, `Postponed`, `Delayed`, `Canceled`, `Forfeit`, `NotNecessary` |
| MLB       | `Scheduled`, `InProgress`, `Final`, `Suspended`, `Delayed`, `Postponed`, `Canceled`, `Forfeit`, `NotNecessary`                 |
| NBA 和 CBB | `Scheduled`, `InProgress`, `Final`, `F/OT`, `Suspended`, `Postponed`, `Delayed`, `Canceled`, `Forfeit`, `NotNecessary`         |
| CFB       | `Scheduled`, `InProgress`, `Final`, `F/OT`, `Suspended`, `Postponed`, `Delayed`, `Canceled`, `Forfeit`                         |
| 足球        | `Scheduled`, `InProgress`, `Break`, `Suspended`, `PenaltyShootout`, `Final`, `Awarded`, `Postponed`, `Canceled`                |
| 电子竞技      | `not_started`, `running`, `finished`, `postponed`, `canceled`                                                                  |
| 网球        | `scheduled`, `inprogress`, `suspended`, `finished`, `postponed`, `cancelled`                                                   |

## RTDS 数据流

实时数据服务（RTDS）通过一个共享的实时连接传输参考价格和评论。只需订阅
应用所需的主题。

### 加密货币价格

使用加密货币价格数据流，让参考值与相关市场保持同步。

如需时间加权加密货币价格，请参阅 [Chainlink TWAP
价格](/cn/market-data/chainlink-twap)。

<Tip>
  交易 15 分钟加密货币市场？可[申请由 Chainlink 赞助的 API
  密钥](https://pm-ds-request.streams.chain.link/)，并获得 Chainlink
  的接入支持。
</Tip>

请使用所选价格来源对应的符号格式：

| 来源        | 支持的符号                                      |
| --------- | ------------------------------------------ |
| Binance   | `btcusdt`, `ethusdt`, `solusdt`, `xrpusdt` |
| Chainlink | `btc/usd`, `eth/usd`, `sol/usd`, `xrp/usd` |

<Tabs>
  <Tab title="TypeScript">
    使用 `PublicClient` 或 `SecureClient`，订阅所需的加密货币价格主题：

    ```ts theme={null}
    const stream = await client.subscribe([
      { topic: "prices.crypto.binance", symbols: ["btcusdt", "ethusdt"] },
      { topic: "prices.crypto.chainlink", symbols: ["eth/usd"] },
    ]);

    for await (const event of stream) {
      switch (event.topic) {
        case "prices.crypto.binance":
          // event: CryptoPricesBinanceEvent
          break;
        case "prices.crypto.chainlink":
          // event: CryptoPricesChainlinkEvent
          break;
      }
    }
    ```

    <Accordion title="加密货币价格事件">
      #### Binance 价格更新

      <CodeGroup>
        ```ts CryptoPricesBinanceEvent Type theme={null}
        type PriceUpdatePayload = {
          symbol: string;
          timestamp: EpochMilliseconds;
          value: DecimalString;
        };

        type CryptoPricesBinanceEvent = {
          topic: "prices.crypto.binance";
          type: "update";
          timestamp: EpochMilliseconds;
          payload: PriceUpdatePayload;
        };
        ```

        ```json CryptoPricesBinanceEvent Example theme={null}
        {
          "topic": "prices.crypto.binance",
          "type": "update",
          "timestamp": 1782753357257,
          "payload": {
            "symbol": "btcusdt",
            "timestamp": 1782753357213,
            "value": "67234.5"
          }
        }
        ```
      </CodeGroup>

      #### Chainlink 价格更新

      <CodeGroup>
        ```ts CryptoPricesChainlinkEvent Type theme={null}
        type CryptoPricesChainlinkEvent = {
          topic: "prices.crypto.chainlink";
          type: "update";
          timestamp: EpochMilliseconds;
          payload: PriceUpdatePayload;
        };
        ```

        ```json CryptoPricesChainlinkEvent Example theme={null}
        {
          "topic": "prices.crypto.chainlink",
          "type": "update",
          "timestamp": 1782753357257,
          "payload": {
            "symbol": "eth/usd",
            "timestamp": 1782753357213,
            "value": "3420.15"
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    使用 `AsyncPublicClient` 或 `AsyncSecureClient`，通过所需的加密货币
    价格规范进行订阅：

    ```python theme={null}
    from polymarket.streams import CryptoPricesSpec

    async with await client.subscribe(
        [
            CryptoPricesSpec(topic="prices.crypto.binance", symbols=["btcusdt", "ethusdt"]),
            CryptoPricesSpec(topic="prices.crypto.chainlink", symbols=["eth/usd"]),
        ],
    ) as stream:
        async for event in stream:
            if event.topic == "prices.crypto.binance":
                ...  # event: CryptoPricesBinanceEvent
            elif event.topic == "prices.crypto.chainlink":
                ...  # event: CryptoPricesChainlinkEvent
    ```

    <Accordion title="加密货币价格事件">
      #### Binance 价格更新

      <CodeGroup>
        ```python CryptoPricesBinanceEvent Type theme={null}
        class PriceUpdatePayload:
            symbol: str
            timestamp: int
            value: Decimal

        class CryptoPricesBinanceEvent:
            topic: Literal["prices.crypto.binance"]
            type: Literal["update"]
            timestamp: datetime
            payload: PriceUpdatePayload
        ```

        ```json CryptoPricesBinanceEvent Example theme={null}
        {
          "topic": "prices.crypto.binance",
          "type": "update",
          "timestamp": "2026-06-29T17:15:57.257000Z",
          "payload": {
            "symbol": "btcusdt",
            "timestamp": 1782753357213,
            "value": "67234.5"
          }
        }
        ```
      </CodeGroup>

      #### Chainlink 价格更新

      <CodeGroup>
        ```python CryptoPricesChainlinkEvent Type theme={null}
        class CryptoPricesChainlinkEvent:
            topic: Literal["prices.crypto.chainlink"]
            type: Literal["update"]
            timestamp: datetime
            payload: PriceUpdatePayload
        ```

        ```json CryptoPricesChainlinkEvent Example theme={null}
        {
          "topic": "prices.crypto.chainlink",
          "type": "update",
          "timestamp": "2026-06-29T17:15:57.257000Z",
          "payload": {
            "symbol": "eth/usd",
            "timestamp": 1782753357213,
            "value": "3420.15"
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    连接 RTDS：

    ```text theme={null}
    wss://ws-live-data.polymarket.com
    ```

    <Note>RTDS 使用应用层心跳。每 5 秒发送一次文本帧 `PING` 以维持连接。</Note>

    连接后，为所需的加密货币价格主题发送订阅帧：

    ```json theme={null}
    {
      "action": "subscribe",
      "subscriptions": [
        {
          "topic": "crypto_prices",
          "type": "update",
          "filters": "btcusdt,ethusdt"
        },
        {
          "topic": "crypto_prices_chainlink",
          "type": "*",
          "filters": "{\"symbol\":\"eth/usd\"}"
        }
      ]
    }
    ```

    Binance 过滤器使用逗号分隔符号，而 Chainlink 过滤器是包含单个符号的
    JSON 字符串。省略 `filters` 可接收某个主题和类型的所有事件。

    <Accordion title="加密货币价格事件">
      #### Binance 价格更新

      ```json theme={null}
      {
        "topic": "crypto_prices",
        "type": "update",
        "timestamp": 1782753357257,
        "payload": {
          "symbol": "btcusdt",
          "timestamp": 1782753357213,
          "value": 67234.5
        }
      }
      ```

      #### Chainlink 价格更新

      ```json theme={null}
      {
        "topic": "crypto_prices_chainlink",
        "type": "update",
        "timestamp": 1782753357257,
        "payload": {
          "symbol": "eth/usd",
          "timestamp": 1782753357213,
          "value": 3420.15
        }
      }
      ```
    </Accordion>
  </Tab>
</Tabs>

### 股票类资产价格

股票类资产价格数据流提供 Pyth Network 的股票、ETF、外汇货币对、贵金属和
大宗商品参考价格。

<Tip>
  交易股票类资产市场？可[订阅 Pyth Network 数据
  源](https://buy.stripe.com/cNi8wPeiq76FgQrbsD4ZG09)。前 30 天免费，之后每月
  费用为 99 美元。
</Tip>

#### 订阅股票类资产价格

<Tabs>
  <Tab title="TypeScript">
    使用 `PublicClient` 或 `SecureClient`，为所需符号订阅股票类资产
    价格主题：

    ```ts theme={null}
    const stream = await client.subscribe([
      { topic: "prices.equity.pyth", symbol: "AAPL" },
    ]);

    for await (const event of stream) {
      if (event.type === "update") {
        // event: EquityPricesUpdateEvent
      } else {
        // event: EquityPricesSubscribeEvent
      }
    }
    ```

    <Accordion title="股票类资产价格事件">
      #### 股票类资产价格更新

      <CodeGroup>
        ```ts EquityPricesUpdateEvent Type theme={null}
        type EquityPriceUpdatePayload = {
          symbol: string;
          value: DecimalString;
          timestamp: EpochMilliseconds;
          receivedAt?: EpochMilliseconds | null;
          isCarriedForward?: boolean | null;
        };

        type EquityPricesUpdateEvent = {
          topic: "prices.equity.pyth";
          type: "update";
          timestamp: EpochMilliseconds;
          payload: EquityPriceUpdatePayload;
        };
        ```

        ```json EquityPricesUpdateEvent Example theme={null}
        {
          "topic": "prices.equity.pyth",
          "type": "update",
          "timestamp": 1782753357257,
          "payload": {
            "symbol": "aapl",
            "timestamp": 1782753357213,
            "value": "189.4217",
            "receivedAt": 1782753357220,
            "isCarriedForward": false
          }
        }
        ```
      </CodeGroup>

      #### 股票类资产价格快照

      <CodeGroup>
        ```ts EquityPricesSubscribeEvent Type theme={null}
        type EquityPriceSnapshotPoint = {
          timestamp: number;
          value: DecimalString;
        };

        type EquityPriceSubscribePayload = {
          symbol: string;
          data: EquityPriceSnapshotPoint[];
        };

        type EquityPricesSubscribeEvent = {
          topic: "prices.equity.pyth";
          type: "subscribe";
          timestamp: EpochMilliseconds;
          payload: EquityPriceSubscribePayload;
        };
        ```

        ```json EquityPricesSubscribeEvent Example theme={null}
        {
          "topic": "prices.equity.pyth",
          "type": "subscribe",
          "timestamp": 1782753357257,
          "payload": {
            "symbol": "aapl",
            "data": [
              { "timestamp": 1782753297000, "value": "189.38" },
              { "timestamp": 1782753357000, "value": "189.42" }
            ]
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    使用 `AsyncPublicClient` 或 `AsyncSecureClient`，通过股票类资产价格
    规范订阅所需符号：

    ```python theme={null}
    from polymarket.streams import EquityPricesSpec

    async with await client.subscribe(EquityPricesSpec(symbol="AAPL")) as stream:
        async for event in stream:
            if event.type == "update":
                ...  # event: EquityPricesUpdateEvent
            else:
                ...  # event: EquityPricesSubscribeEvent
    ```

    <Accordion title="股票类资产价格事件">
      #### 股票类资产价格更新

      <CodeGroup>
        ```python EquityPricesUpdateEvent Type theme={null}
        class EquityPriceUpdatePayload:
            symbol: str
            value: Decimal
            timestamp: int
            received_at: int | None
            is_carried_forward: bool | None

        class EquityPricesUpdateEvent:
            topic: Literal["prices.equity.pyth"]
            type: Literal["update"]
            timestamp: datetime
            payload: EquityPriceUpdatePayload
        ```

        ```json EquityPricesUpdateEvent Example theme={null}
        {
          "topic": "prices.equity.pyth",
          "type": "update",
          "timestamp": "2026-06-29T17:15:57.257000Z",
          "payload": {
            "symbol": "aapl",
            "value": "189.4217",
            "timestamp": 1782753357213,
            "received_at": 1782753357220,
            "is_carried_forward": false
          }
        }
        ```
      </CodeGroup>

      #### 股票类资产价格快照

      <CodeGroup>
        ```python EquityPricesSubscribeEvent Type theme={null}
        class EquityPriceSnapshotEntry:
            timestamp: int
            value: Decimal

        class EquityPriceSubscribePayload:
            symbol: str
            data: tuple[EquityPriceSnapshotEntry, ...]

        class EquityPricesSubscribeEvent:
            topic: Literal["prices.equity.pyth"]
            type: Literal["subscribe"]
            timestamp: datetime
            payload: EquityPriceSubscribePayload
        ```

        ```json EquityPricesSubscribeEvent Example theme={null}
        {
          "topic": "prices.equity.pyth",
          "type": "subscribe",
          "timestamp": "2026-06-29T17:15:57.257000Z",
          "payload": {
            "symbol": "aapl",
            "data": [
              { "timestamp": 1782753297000, "value": "189.38" },
              { "timestamp": 1782753357000, "value": "189.42" }
            ]
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    连接 RTDS：

    ```text theme={null}
    wss://ws-live-data.polymarket.com
    ```

    <Note>RTDS 使用应用层心跳。每 5 秒发送一次文本帧 `PING` 以维持连接。</Note>

    连接后，为所需符号发送股票类资产价格订阅帧：

    ```json theme={null}
    {
      "action": "subscribe",
      "subscriptions": [
        {
          "topic": "equity_prices",
          "type": "*",
          "filters": "{\"symbol\":\"AAPL\"}"
        }
      ]
    }
    ```

    `filters` 是可选字符串，服务器会验证它是否为格式正确的 JSON：键和
    字符串值都必须加引号。如需过滤多个符号，请为每个符号发送一个订阅条目。
    `filters` 值为空或省略时表示不过滤。

    <Note>
      价格更新可能包含 `full_accuracy_value`。存在该字段时，应优先使用这个
      字符串，而不是数值型 `value`。
    </Note>

    <Accordion title="股票类资产价格事件">
      #### 股票类资产价格更新

      ```json theme={null}
      {
        "topic": "equity_prices",
        "type": "update",
        "timestamp": 1782753357257,
        "payload": {
          "symbol": "aapl",
          "timestamp": 1782753357213,
          "value": 189.42,
          "full_accuracy_value": "189.4217",
          "received_at": 1782753357220,
          "is_carried_forward": false
        }
      }
      ```

      #### 股票类资产价格快照

      ```json theme={null}
      {
        "topic": "equity_prices",
        "type": "subscribe",
        "timestamp": 1782753357257,
        "payload": {
          "symbol": "aapl",
          "data": [
            { "timestamp": 1782753297000, "value": 189.38 },
            { "timestamp": 1782753357000, "value": 189.42 }
          ]
        }
      }
      ```
    </Accordion>
  </Tab>
</Tabs>

#### 历史快照

订阅某个符号时，数据流会先发送该符号此前两分钟的价格数据，然后继续发送
实时更新。在处理新价格之前，可使用此快照初始化本地状态。

#### 支持的符号

| 资产类别 | 支持的符号                                                                                                           |
| ---- | --------------------------------------------------------------------------------------------------------------- |
| 股票   | `AAPL`, `TSLA`, `MSFT`, `GOOGL`, `AMZN`, `META`, `NVDA`, `NFLX`, `PLTR`, `OPEN`, `RKLB`, `ABNB`, `COIN`, `HOOD` |
| ETF  | `QQQ`, `SPY`, `EWY`, `VXX`                                                                                      |
| 外汇   | `EURUSD`, `GBPUSD`, `USDCAD`, `USDJPY`, `USDKRW`                                                                |
| 贵金属  | `XAUUSD`, `XAGUSD`                                                                                              |
| 大宗商品 | `WTI`, `CC`, `NGD`                                                                                              |

<Note>订阅符号不区分大小写，但数据流事件会返回小写符号。</Note>

#### 市场交易时间

当某项资产的市场休市时，数据流会继续发送其最后已知价格，并将该值标记为
沿用值。在市场交易时段内，每个数据源的价格每秒最多更新五次。

### 评论

在应用展示 Polymarket 讨论的任何位置，使用评论数据流让对话和表情回应保持
最新。评论可以发起一个讨论串，也可以回复另一条评论；回复事件包含父评论的 ID。

<Tabs>
  <Tab title="TypeScript">
    使用 `PublicClient` 或 `SecureClient`，订阅 `comments` 主题并选择
    所需的事件类型：

    ```ts theme={null}
    const stream = await client.subscribe([
      {
        topic: "comments",
        types: [
          "comment_created",
          "comment_removed",
          "reaction_created",
          "reaction_removed",
        ],
      },
    ]);

    for await (const event of stream) {
      switch (event.type) {
        case "comment_created":
          // event: CommentCreatedEvent
          break;
        case "comment_removed":
          // event: CommentRemovedEvent
          break;
        case "reaction_created":
          // event: ReactionCreatedEvent
          break;
        case "reaction_removed":
          // event: ReactionRemovedEvent
          break;
      }
    }
    ```

    使用 `parentEntityId` 和 `parentEntityType` 将订阅范围限定为单个
    事件或市场。

    <Accordion title="评论事件">
      #### 新评论

      <CodeGroup>
        ```ts CommentCreatedEvent Type theme={null}
        type Comment = {
          id: CommentId;
          body?: string | null;
          parentEntityType?: CommentParentEntityType | null;
          parentEntityID?: EventId | SeriesId | null;
          parentCommentID?: CommentId | null;
          userAddress?: string | null;
          replyAddress?: string | null;
          createdAt?: IsoDateTimeString | null;
          updatedAt?: IsoDateTimeString | null;
          media?: CommentMedia[] | null;
          profile?: CommentProfile | null;
          reactions?: Reaction[] | null;
          reportCount?: number | null;
          reactionCount?: number | null;
          tradeAsset?: string | null;
        };

        type CommentCreatedEvent = {
          topic: "comments";
          type: "comment_created";
          timestamp: EpochMilliseconds;
          payload: Comment;
        };
        ```

        ```json CommentCreatedEvent Example theme={null}
        {
          "topic": "comments",
          "type": "comment_created",
          "timestamp": 1782753357257,
          "payload": {
            "id": "1763355",
            "body": "That's a good point about the definition.",
            "parentEntityType": "Event",
            "parentEntityID": "18396",
            "parentCommentID": null,
            "userAddress": "0xce533188d53a16ed580fd5121dedf166d3482677",
            "replyAddress": "0x0bda5d16f76cd1d3485bcc7a44bc6fa7db004cdd",
            "createdAt": "2025-07-25T14:49:35.801298Z",
            "reactionCount": 0,
            "reportCount": 0,
            "profile": {
              "baseAddress": "0xce533188d53a16ed580fd5121dedf166d3482677",
              "displayUsernamePublic": true,
              "name": "salted.caramel",
              "wallet": "0x4ca749dcfa93c87e5ee23e2d21ff4422c7a4c1ee",
              "pseudonym": "Adored-Disparity"
            }
          }
        }
        ```
      </CodeGroup>

      #### 已移除的评论

      <CodeGroup>
        ```ts CommentRemovedEvent Type theme={null}
        type CommentRemovedPayload = {
          id: string;
          body?: string | null;
          parentEntityType?: CommentParentEntityType | null;
          parentEntityID?: number | null;
          parentCommentID?: string | null;
          userAddress?: string | null;
          replyAddress?: string | null;
          createdAt?: string | null;
          updatedAt?: string | null;
          media?: CommentMedia[] | null;
          profile?: CommentProfile | null;
          reactions?: Reaction[] | null;
          reportCount?: number | null;
          reactionCount?: number | null;
          tradeAsset?: string | null;
        };

        type CommentRemovedEvent = {
          topic: "comments";
          type: "comment_removed";
          timestamp: EpochMilliseconds;
          payload: CommentRemovedPayload;
        };
        ```

        ```json CommentRemovedEvent Example theme={null}
        {
          "topic": "comments",
          "type": "comment_removed",
          "timestamp": 1782753357257,
          "payload": {
            "id": "1763355",
            "body": "That's a good point about the definition.",
            "parentEntityType": "Event",
            "parentEntityID": 18396,
            "userAddress": "0xce533188d53a16ed580fd5121dedf166d3482677"
          }
        }
        ```
      </CodeGroup>

      #### 新表情回应

      <CodeGroup>
        ```ts ReactionCreatedEvent Type theme={null}
        type Reaction = {
          id: string;
          commentID?: number | null;
          reactionType?: ReactionType | null;
          icon?: string | null;
          userAddress?: string | null;
          createdAt?: IsoDateTimeString | null;
          profile?: CommentProfile | null;
        };

        type ReactionCreatedEvent = {
          topic: "comments";
          type: "reaction_created";
          timestamp: EpochMilliseconds;
          payload: Reaction;
        };
        ```

        ```json ReactionCreatedEvent Example theme={null}
        {
          "topic": "comments",
          "type": "reaction_created",
          "timestamp": 1782753357257,
          "payload": {
            "id": "8675309",
            "commentID": 1763355,
            "reactionType": "HEART",
            "icon": "❤️",
            "userAddress": "0xce533188d53a16ed580fd5121dedf166d3482677",
            "createdAt": "2025-07-25T14:50:04.120000Z"
          }
        }
        ```
      </CodeGroup>

      #### 已移除的表情回应

      <CodeGroup>
        ```ts ReactionRemovedEvent Type theme={null}
        type ReactionRemovedEvent = {
          topic: "comments";
          type: "reaction_removed";
          timestamp: EpochMilliseconds;
          payload: Reaction;
        };
        ```

        ```json ReactionRemovedEvent Example theme={null}
        {
          "topic": "comments",
          "type": "reaction_removed",
          "timestamp": 1782753357257,
          "payload": {
            "id": "8675309",
            "commentID": 1763355,
            "reactionType": "HEART",
            "userAddress": "0xce533188d53a16ed580fd5121dedf166d3482677"
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    使用 `AsyncPublicClient` 或 `AsyncSecureClient`，通过包含所需事件
    类型的 `CommentsSpec` 进行订阅：

    ```python theme={null}
    from polymarket.streams import CommentsSpec

    async with await client.subscribe(
        CommentsSpec(
            types=[
                "comment_created",
                "comment_removed",
                "reaction_created",
                "reaction_removed",
            ],
        ),
    ) as stream:
        async for event in stream:
            if event.type == "comment_created":
                ...  # event: CommentCreatedEvent
            elif event.type == "comment_removed":
                ...  # event: CommentRemovedEvent
            elif event.type == "reaction_created":
                ...  # event: ReactionCreatedEvent
            else:
                ...  # event: ReactionRemovedEvent
    ```

    使用 `parent_entity_id` 和 `parent_entity_type` 将订阅范围限定为
    单个事件或市场。

    <Accordion title="评论事件">
      #### 新评论

      <CodeGroup>
        ```python CommentCreatedEvent Type theme={null}
        class Comment:
            id: CommentId
            body: str | None
            parent_entity_type: str | None
            parent_entity_id: EventId | SeriesId | None
            parent_comment_id: CommentId | None
            user_address: EvmAddress | None
            reply_address: EvmAddress | None
            created_at: datetime | None
            updated_at: datetime | None
            media: tuple[CommentMedia, ...] | None
            profile: CommentProfile | None
            reactions: tuple[Reaction, ...] | None
            report_count: int | None
            reaction_count: int | None
            trade_asset: str | None

        class CommentCreatedEvent:
            topic: Literal["comments"]
            type: Literal["comment_created"]
            timestamp: datetime
            payload: Comment
        ```

        ```json CommentCreatedEvent Example theme={null}
        {
          "topic": "comments",
          "type": "comment_created",
          "timestamp": "2026-06-29T17:15:57.257000Z",
          "payload": {
            "id": "1763355",
            "body": "That's a good point about the definition.",
            "parent_entity_type": "Event",
            "parent_entity_id": "18396",
            "parent_comment_id": null,
            "user_address": "0xce533188d53a16ed580fd5121dedf166d3482677",
            "reply_address": "0x0bda5d16f76cd1d3485bcc7a44bc6fa7db004cdd",
            "created_at": "2025-07-25T14:49:35.801298Z",
            "reaction_count": 0,
            "report_count": 0,
            "profile": {
              "base_address": "0xce533188d53a16ed580fd5121dedf166d3482677",
              "display_username_public": true,
              "name": "salted.caramel",
              "wallet": "0x4ca749dcfa93c87e5ee23e2d21ff4422c7a4c1ee",
              "pseudonym": "Adored-Disparity"
            }
          }
        }
        ```
      </CodeGroup>

      #### 已移除的评论

      <CodeGroup>
        ```python CommentRemovedEvent Type theme={null}
        class CommentRemovedPayload:
            id: str
            body: str | None
            parent_entity_type: Literal["Event", "Market"] | None
            parent_entity_id: int | None
            parent_comment_id: str | None
            user_address: EvmAddress | None
            reply_address: EvmAddress | None
            created_at: datetime | None
            updated_at: datetime | None
            media: tuple[CommentMedia, ...] | None
            profile: CommentProfile | None
            reactions: tuple[Reaction, ...] | None
            report_count: int | None
            reaction_count: int | None
            trade_asset: str | None

        class CommentRemovedEvent:
            topic: Literal["comments"]
            type: Literal["comment_removed"]
            timestamp: datetime
            payload: CommentRemovedPayload
        ```

        ```json CommentRemovedEvent Example theme={null}
        {
          "topic": "comments",
          "type": "comment_removed",
          "timestamp": "2026-06-29T17:15:57.257000Z",
          "payload": {
            "id": "1763355",
            "body": "That's a good point about the definition.",
            "parent_entity_type": "Event",
            "parent_entity_id": 18396,
            "user_address": "0xce533188d53a16ed580fd5121dedf166d3482677"
          }
        }
        ```
      </CodeGroup>

      #### 新表情回应

      <CodeGroup>
        ```python ReactionCreatedEvent Type theme={null}
        class Reaction:
            id: str
            comment_id: int | None
            reaction_type: str | None
            icon: str | None
            user_address: EvmAddress | None
            created_at: datetime | None
            profile: CommentProfile | None

        class ReactionCreatedEvent:
            topic: Literal["comments"]
            type: Literal["reaction_created"]
            timestamp: datetime
            payload: Reaction
        ```

        ```json ReactionCreatedEvent Example theme={null}
        {
          "topic": "comments",
          "type": "reaction_created",
          "timestamp": "2026-06-29T17:15:57.257000Z",
          "payload": {
            "id": "8675309",
            "comment_id": 1763355,
            "reaction_type": "HEART",
            "icon": "❤️",
            "user_address": "0xce533188d53a16ed580fd5121dedf166d3482677",
            "created_at": "2025-07-25T14:50:04.120000Z"
          }
        }
        ```
      </CodeGroup>

      #### 已移除的表情回应

      <CodeGroup>
        ```python ReactionRemovedEvent Type theme={null}
        class ReactionRemovedEvent:
            topic: Literal["comments"]
            type: Literal["reaction_removed"]
            timestamp: datetime
            payload: Reaction
        ```

        ```json ReactionRemovedEvent Example theme={null}
        {
          "topic": "comments",
          "type": "reaction_removed",
          "timestamp": "2026-06-29T17:15:57.257000Z",
          "payload": {
            "id": "8675309",
            "comment_id": 1763355,
            "reaction_type": "HEART",
            "user_address": "0xce533188d53a16ed580fd5121dedf166d3482677"
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    连接 RTDS：

    ```text theme={null}
    wss://ws-live-data.polymarket.com
    ```

    <Note>RTDS 使用应用层心跳。每 5 秒发送一次文本帧 `PING` 以维持连接。</Note>

    连接后，为每种所需的评论事件类型发送一个订阅条目。使用 `filters`
    字符串将条目范围限定为单个实体：

    ```json theme={null}
    {
      "action": "subscribe",
      "subscriptions": [
        {
          "topic": "comments",
          "type": "comment_created",
          "filters": "{\"parentEntityID\":18396,\"parentEntityType\":\"Event\"}"
        }
      ]
    }
    ```

    如果提供 `filters`，它必须是格式正确的 JSON：键和字符串值都必须
    加引号。`filters` 值为空或省略时，将接收所有评论事件。

    <Accordion title="评论事件">
      #### 新评论

      ```json theme={null}
      {
        "topic": "comments",
        "type": "comment_created",
        "timestamp": 1782753357257,
        "payload": {
          "id": "1763355",
          "body": "That's a good point about the definition.",
          "parentEntityType": "Event",
          "parentEntityID": 18396,
          "parentCommentID": null,
          "userAddress": "0xce533188d53a16ed580fd5121dedf166d3482677",
          "replyAddress": "0x0bda5d16f76cd1d3485bcc7a44bc6fa7db004cdd",
          "createdAt": "2025-07-25T14:49:35.801298Z",
          "reactionCount": 0,
          "reportCount": 0,
          "profile": {
            "baseAddress": "0xce533188d53a16ed580fd5121dedf166d3482677",
            "displayUsernamePublic": true,
            "name": "salted.caramel",
            "proxyWallet": "0x4ca749dcfa93c87e5ee23e2d21ff4422c7a4c1ee",
            "pseudonym": "Adored-Disparity"
          }
        }
      }
      ```

      #### 已移除的评论

      ```json theme={null}
      {
        "topic": "comments",
        "type": "comment_removed",
        "timestamp": 1782753357257,
        "payload": {
          "id": "1763355",
          "body": "That's a good point about the definition.",
          "parentEntityType": "Event",
          "parentEntityID": 18396,
          "userAddress": "0xce533188d53a16ed580fd5121dedf166d3482677"
        }
      }
      ```

      #### 新表情回应

      ```json theme={null}
      {
        "topic": "comments",
        "type": "reaction_created",
        "timestamp": 1782753357257,
        "payload": {
          "id": "8675309",
          "commentID": 1763355,
          "reactionType": "HEART",
          "icon": "❤️",
          "userAddress": "0xce533188d53a16ed580fd5121dedf166d3482677",
          "createdAt": "2025-07-25T14:50:04.120000Z"
        }
      }
      ```

      #### 已移除的表情回应

      ```json theme={null}
      {
        "topic": "comments",
        "type": "reaction_removed",
        "timestamp": 1782753357257,
        "payload": {
          "id": "8675309",
          "commentID": 1763355,
          "reactionType": "HEART",
          "userAddress": "0xce533188d53a16ed580fd5121dedf166d3482677"
        }
      }
      ```
    </Accordion>
  </Tab>
</Tabs>
