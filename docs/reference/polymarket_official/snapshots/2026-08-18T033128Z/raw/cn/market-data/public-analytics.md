> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 分析

> 了解 Polymarket 上的活动与表现。

使用这些读取操作，了解交易活动集中在哪些市场，以及交易者和集成应用随时间推移的表现。

## 市场活动

以下示例假设你已经有一个市场对象。要查找或获取市场，请参阅
[**发现市场**](/cn/market-data/discover-markets)。

<Tabs>
  <Tab title="TypeScript">
    给定一个市场，读取其条件 ID 和事件 ID：

    ```ts theme={null}
    const conditionId = market.conditionId;
    const eventId = market.events[0].id;
    ```
  </Tab>

  <Tab title="Python">
    给定一个市场，读取其条件 ID 和事件 ID：

    ```python theme={null}
    if market.condition_id is None:
        raise RuntimeError("Market condition ID not found")

    condition_id = market.condition_id
    event_id = market.events[0].id
    ```
  </Tab>

  <Tab title="API">
    给定一个市场对象，可以从以下字段获取其条件 ID 和事件 ID：

    ```json theme={null}
    {
      "conditionId": "<condition_id>",
      "events": [{ "id": "<event_id>" }]
    }
    ```

    为下方请求设置这些标识符：

    ```bash theme={null}
    CONDITION_ID="<condition_id>"
    EVENT_ID="<event_id>"
    ```
  </Tab>
</Tabs>

### 最近成交

查看市场中最近撮合的成交，包括方向、价格、数量、结果、钱包和时间戳。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listTrades()`。

    ```ts theme={null}
    const pages = client.listTrades({ market: [conditionId], pageSize: 1 });

    for await (const page of pages) {
      // page.items: Trade[]
    }
    ```

    <Accordion title="输出：Trade[]">
      <CodeGroup>
        ```ts Trade Type theme={null}
        type Trade = {
          side?: Side | null;
          conditionId?: CtfConditionId | null;
          size?: DecimalString | null;
          price?: DecimalString | null;
          timestamp?: EpochMilliseconds | null;
          title?: string | null;
          slug?: string | null;
          icon?: string | null;
          eventSlug?: string | null;
          outcome?: string | null;
          outcomeIndex?: number | null;
          name?: string | null;
          pseudonym?: string | null;
          bio?: string | null;
          profileImage?: string | null;
          profileImageOptimized?: string | null;
          transactionHash?: string | null;
          wallet?: Address | null;
          tokenId?: TokenId | null;
        };
        ```

        ```json Trade Example theme={null}
        [
          {
            "side": "SELL",
            "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "size": "42.62",
            "price": "0.91",
            "timestamp": 1782752879000,
            "title": "Will the US confirm that aliens exist before 2027?",
            "outcome": "No",
            "wallet": "0x50A0cebecFb81DbCbFA6d38F82040343F1E3D95F",
            "tokenId": "7305630249804085635496399869905769372294302716159034447326228509068694952392"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_trades()`。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_trades(market=[condition_id], page_size=1)

    async for page in pages:
        ...  # page.items: tuple[Trade, ...]
    ```

    <Accordion title="输出：tuple[Trade, ...]">
      <CodeGroup>
        ```python Trade Type theme={null}
        class Trade:
            side: Literal["BUY", "SELL"] | None
            condition_id: CtfConditionId | None
            size: Decimal | None
            price: Decimal | None
            timestamp: datetime | None
            title: str | None
            slug: str | None
            icon: str | None
            event_slug: str | None
            outcome: str | None
            outcome_index: int | None
            name: str | None
            pseudonym: str | None
            bio: str | None
            profile_image: str | None
            profile_image_optimized: str | None
            transaction_hash: TransactionHash | None
            wallet: EvmAddress | None
            token_id: TokenId | None
        ```

        ```json Trade Example theme={null}
        [
          {
            "side": "SELL",
            "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "size": "42.62",
            "price": "0.91",
            "timestamp": "2026-06-29T17:07:59Z",
            "title": "Will the US confirm that aliens exist before 2027?",
            "outcome": "No",
            "wallet": "0x50A0cebecFb81DbCbFA6d38F82040343F1E3D95F",
            "token_id": "7305630249804085635496399869905769372294302716159034447326228509068694952392"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    列出某个市场的最近成交：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/trades?market=$CONDITION_ID&limit=1"
    ```

    响应包含最近成交：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "proxyWallet": "0x50A0cebecFb81DbCbFA6d38F82040343F1E3D95F",
          "side": "SELL",
          "asset": "7305630249804085635496399869905769372294302716159034447326228509068694952392",
          "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "size": 42.62,
          "price": 0.91,
          "timestamp": 1782752879,
          "title": "Will the US confirm that aliens exist before 2027?",
          "outcome": "No"
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

### 未平仓量

衡量一个或多个市场当前未平仓头寸中持有的价值。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listOpenInterest()`。

    ```ts theme={null}
    const openInterest = await client.listOpenInterest({
      market: [conditionId],
    });

    // openInterest: OpenInterest[]
    ```

    <Accordion title="输出：OpenInterest[]">
      <CodeGroup>
        ```ts OpenInterest Type theme={null}
        type OpenInterest = {
          market?: Hash64 | null;
          value?: DecimalString | null;
        };
        ```

        ```json OpenInterest Example theme={null}
        [
          {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "value": "7484304.679057"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_open_interests()`。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    open_interest = await client.get_open_interests(market=[condition_id])

    # open_interest: tuple[OpenInterest, ...]
    ```

    <Accordion title="输出：tuple[OpenInterest, ...]">
      <CodeGroup>
        ```python OpenInterest Type theme={null}
        class OpenInterest:
            market: CtfConditionId | Literal["GLOBAL"] | None
            value: Decimal | None
        ```

        ```json OpenInterest Example theme={null}
        [
          {
            "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "value": "7484304.679057"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    获取某个市场的未平仓量：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/oi?market=$CONDITION_ID"
    ```

    响应包含按市场划分的未平仓量：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "value": 7484304.679057
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

### 市场持仓者

查找市场中每种结果代币的最大公开持仓者。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listMarketHolders()`。

    ```ts theme={null}
    const holders = await client.listMarketHolders({
      market: [conditionId],
      limit: 1,
    });

    // holders: MetaHolder[]
    ```

    <Accordion title="输出：MetaHolder[]">
      <CodeGroup>
        ```ts MetaHolder Type theme={null}
        type Holder = {
          wallet?: Address | null;
          tokenId?: TokenId | null;
          amount?: DecimalString | null;
          outcomeIndex?: number | null;
          name?: string | null;
          pseudonym?: string | null;
          bio?: string | null;
          displayUsernamePublic?: boolean | null;
          profileImage?: string | null;
          profileImageOptimized?: string | null;
        };

        type MetaHolder = {
          token?: string | null;
          holders?: Holder[] | null;
        };
        ```

        ```json MetaHolder Example theme={null}
        [
          {
            "token": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "holders": [
              {
                "wallet": "0xaf23273e03A924A257eDD6bEae7133CF9D32377f",
                "amount": "894254.445452",
                "outcomeIndex": 0,
                "name": "SCssss"
              }
            ]
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_market_holders()`。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    holders = await client.get_market_holders(market=[condition_id], limit=1)

    # holders: tuple[MetaHolder, ...]
    ```

    <Accordion title="输出：tuple[MetaHolder, ...]">
      <CodeGroup>
        ```python MetaHolder Type theme={null}
        class Holder:
            wallet: EvmAddress | None
            token_id: TokenId | None
            amount: Decimal | None
            outcome_index: int | None
            name: str | None
            pseudonym: str | None
            bio: str | None
            display_username_public: bool | None
            profile_image: str | None
            profile_image_optimized: str | None

        class MetaHolder:
            token: str | None
            holders: tuple[Holder, ...] | None
        ```

        ```json MetaHolder Example theme={null}
        [
          {
            "token": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "holders": [
              {
                "wallet": "0xaf23273e03A924A257eDD6bEae7133CF9D32377f",
                "amount": "894254.445452",
                "outcome_index": 0,
                "name": "SCssss"
              }
            ]
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    列出某个市场的最大持仓者：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/holders?market=$CONDITION_ID&limit=1"
    ```

    响应按结果代币对持仓者分组：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "token": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "holders": [
            {
              "proxyWallet": "0xaf23273e03A924A257eDD6bEae7133CF9D32377f",
              "amount": 894254.445452,
              "outcomeIndex": 0,
              "name": "SCssss"
            }
          ]
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

### 事件实时交易量

汇总一个事件的整体活动，并按市场细分交易量。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchEventLiveVolume()`。

    ```ts theme={null}
    const liveVolume = await client.fetchEventLiveVolume({
      id: eventId,
    });

    // liveVolume: LiveVolume[]
    ```

    <Accordion title="输出：LiveVolume[]">
      <CodeGroup>
        ```ts LiveVolume Type theme={null}
        type MarketVolume = {
          market?: Hash64 | null;
          value?: DecimalString | null;
        };

        type LiveVolume = {
          total?: DecimalString | null;
          markets?: MarketVolume[] | null;
        };
        ```

        ```json LiveVolume Example theme={null}
        [
          {
            "total": "60773073.437817",
            "markets": [
              {
                "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
                "value": "34909514.911729"
              },
              {
                "market": "0xa7962b12241616d83dcb8c70fc33aa0f48b1ec46a3ad6a23db21d3885dedc4cb",
                "value": "11192047.412956"
              },
              {
                "market": "0xcddc048c672ee233890b99b18885dbd510e3db3d67c53afb408ddc93f9aadff4",
                "value": "8741732.803805"
              },
              "..."
            ]
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_event_live_volumes()`。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    live_volume = await client.get_event_live_volumes(id=event_id)

    # live_volume: tuple[LiveVolume, ...]
    ```

    <Accordion title="输出：tuple[LiveVolume, ...]">
      <CodeGroup>
        ```python LiveVolume Type theme={null}
        class MarketVolume:
            market: CtfConditionId | None
            value: Decimal | None

        class LiveVolume:
            total: Decimal | None
            markets: tuple[MarketVolume, ...] | None
        ```

        ```json LiveVolume Example theme={null}
        [
          {
            "total": "60773073.437817",
            "markets": [
              {
                "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
                "value": "34909514.911729"
              },
              {
                "market": "0xa7962b12241616d83dcb8c70fc33aa0f48b1ec46a3ad6a23db21d3885dedc4cb",
                "value": "11192047.412956"
              },
              {
                "market": "0xcddc048c672ee233890b99b18885dbd510e3db3d67c53afb408ddc93f9aadff4",
                "value": "8741732.803805"
              },
              "..."
            ]
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    获取某个事件的实时交易量：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/live-volume?id=$EVENT_ID"
    ```

    响应包含事件总交易量及其市场明细：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "total": 60773073.437817,
          "markets": [
            {
              "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
              "value": 34909514.911729
            },
            {
              "market": "0xa7962b12241616d83dcb8c70fc33aa0f48b1ec46a3ad6a23db21d3885dedc4cb",
              "value": 11192047.412956
            },
            {
              "market": "0xcddc048c672ee233890b99b18885dbd510e3db3d67c53afb408ddc93f9aadff4",
              "value": 8741732.803805
            },
            "..."
          ]
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

## 交易者排行榜

比较选定时间段内交易者的交易量和盈亏。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listTraderLeaderboard()`。

    ```ts theme={null}
    const pages = client.listTraderLeaderboard({ period: "1d", pageSize: 1 });

    for await (const page of pages) {
      // page.items: TraderLeaderboardEntry[]
    }
    ```

    <Accordion title="输出：TraderLeaderboardEntry[]">
      <CodeGroup>
        ```ts TraderLeaderboardEntry Type theme={null}
        type TraderLeaderboardEntry = {
          rank?: string | null;
          userName?: string | null;
          vol?: DecimalString | null;
          pnl?: DecimalString | null;
          wallet?: Address | null;
          profileImage?: string | null;
          xUsername?: string | null;
          verifiedBadge?: boolean | null;
        };
        ```

        ```json TraderLeaderboardEntry Example theme={null}
        [
          {
            "rank": "1",
            "userName": "NonceChaser",
            "vol": "89333.771517",
            "pnl": "193943.93458989085",
            "wallet": "0xb1Ca909E848CC24ec4E220CE1C453Bc290C51705"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_trader_leaderboard()`。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_trader_leaderboard(period="1d", page_size=1)

    async for page in pages:
        ...  # page.items: tuple[TraderLeaderboardEntry, ...]
    ```

    <Accordion title="输出：tuple[TraderLeaderboardEntry, ...]">
      <CodeGroup>
        ```python TraderLeaderboardEntry Type theme={null}
        class TraderLeaderboardEntry:
            rank: str | None
            user_name: str | None
            vol: Decimal | None
            pnl: Decimal | None
            wallet: EvmAddress | None
            profile_image: str | None
            x_username: str | None
            verified_badge: bool | None
        ```

        ```json TraderLeaderboardEntry Example theme={null}
        [
          {
            "rank": "1",
            "user_name": "NonceChaser",
            "vol": "89333.771517",
            "pnl": "193943.93458989085",
            "wallet": "0xb1Ca909E848CC24ec4E220CE1C453Bc290C51705"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    列出某个时间段的交易者排行榜：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/v1/leaderboard?period=1d&limit=1"
    ```

    响应包含按排名排列的交易者：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "rank": "1",
          "proxyWallet": "0xb1Ca909E848CC24ec4E220CE1C453Bc290C51705",
          "userName": "NonceChaser",
          "vol": 89333.771517,
          "pnl": 193943.93458989085
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

## Builder 分析

通过归因到 Builder 集成的交易活动，评估其覆盖范围。

### Builder 排行榜

根据归因交易量和活跃用户数比较各个 Builder。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listBuilderLeaderboard()`。

    ```ts theme={null}
    const pages = client.listBuilderLeaderboard({ period: "1d", pageSize: 1 });

    for await (const page of pages) {
      // page.items: LeaderboardEntry[]
    }
    ```

    <Accordion title="输出：LeaderboardEntry[]">
      <CodeGroup>
        ```ts LeaderboardEntry Type theme={null}
        type LeaderboardEntry = {
          rank?: string | null;
          builder?: string | null;
          volume?: DecimalString | null;
          activeUsers?: number | null;
          verified?: boolean | null;
          builderLogo?: string | null;
        };
        ```

        ```json LeaderboardEntry Example theme={null}
        [
          {
            "rank": "1",
            "builder": "Gate",
            "volume": "5628007.453362993",
            "activeUsers": 11,
            "verified": true
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_builder_leaderboard()`。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_builder_leaderboard(period="1d", page_size=1)

    async for page in pages:
        ...  # page.items: tuple[LeaderboardEntry, ...]
    ```

    <Accordion title="输出：tuple[LeaderboardEntry, ...]">
      <CodeGroup>
        ```python LeaderboardEntry Type theme={null}
        class LeaderboardEntry:
            rank: str | None
            builder: str | None
            volume: Decimal | None
            active_users: int | None
            verified: bool | None
            builder_logo: str | None
        ```

        ```json LeaderboardEntry Example theme={null}
        [
          {
            "rank": "1",
            "builder": "Gate",
            "volume": "5628007.453362993",
            "active_users": 11,
            "verified": true
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    列出某个时间段的 Builder 排行榜：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/v1/builders/leaderboard?period=1d&limit=1"
    ```

    响应包含按排名排列的 Builder：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "rank": "1",
          "builder": "Gate",
          "builderCode": "0x2c6624a23b16a2a69acd14c87b7fc03906870c851fa0a7b7f9d7be0fbbedea8a",
          "volume": 5628007.453362993,
          "activeUsers": 11,
          "verified": true
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

### Builder 交易量

跟踪归因到 Builder 的交易量和活跃用户数随时间的变化。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchBuilderVolume()`。

    ```ts theme={null}
    const builderVolume = await client.fetchBuilderVolume({
      builder: "0x0000000000000000000000000000000000000000",
    });

    // builderVolume: BuilderVolumeEntry[]
    ```

    <Accordion title="输出：BuilderVolumeEntry[]">
      <CodeGroup>
        ```ts BuilderVolumeEntry Type theme={null}
        type BuilderVolumeEntry = {
          bucketAt?: IsoDateTimeString | null;
          builder?: string | null;
          builderLogo?: string | null;
          verified?: boolean | null;
          volume?: DecimalString | null;
          activeUsers?: number | null;
          rank?: string | null;
        };
        ```

        ```json BuilderVolumeEntry Example theme={null}
        [
          {
            "bucketAt": "2026-06-29T00:00:00Z",
            "builder": "Gate",
            "volume": "5605762.113572997",
            "activeUsers": 11,
            "rank": "1"
          },
          {
            "bucketAt": "2026-06-29T00:00:00Z",
            "builder": "betmoar",
            "volume": "3888390.1247219997",
            "activeUsers": 168,
            "rank": "2"
          },
          {
            "bucketAt": "2026-06-29T00:00:00Z",
            "builder": "MagicMarkets",
            "volume": "1765982.109793",
            "activeUsers": 1,
            "rank": "3"
          },
          "..."
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_builder_volumes()`。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    builder_volume = await client.get_builder_volumes(
        builder="0x0000000000000000000000000000000000000000",
    )

    # builder_volume: tuple[BuilderVolumeEntry, ...]
    ```

    <Accordion title="输出：tuple[BuilderVolumeEntry, ...]">
      <CodeGroup>
        ```python BuilderVolumeEntry Type theme={null}
        class BuilderVolumeEntry:
            bucket_at: datetime | None
            builder: str | None
            builder_logo: str | None
            verified: bool | None
            volume: Decimal | None
            active_users: int | None
            rank: str | None
        ```

        ```json BuilderVolumeEntry Example theme={null}
        [
          {
            "bucket_at": "2026-06-29T00:00:00Z",
            "builder": "Gate",
            "volume": "5605762.113572997",
            "active_users": 11,
            "rank": "1"
          },
          {
            "bucket_at": "2026-06-29T00:00:00Z",
            "builder": "betmoar",
            "volume": "3888390.1247219997",
            "active_users": 168,
            "rank": "2"
          },
          {
            "bucket_at": "2026-06-29T00:00:00Z",
            "builder": "MagicMarkets",
            "volume": "1765982.109793",
            "active_users": 1,
            "rank": "3"
          },
          "..."
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    获取某个 Builder 的时间序列交易量：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/v1/builders/volume?builder=0x0000000000000000000000000000000000000000"
    ```

    响应包含按时间桶划分的 Builder 活动：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "dt": "2026-06-29T00:00:00Z",
          "builder": "Gate",
          "builderCode": "0x2c6624a23b16a2a69acd14c87b7fc03906870c851fa0a7b7f9d7be0fbbedea8a",
          "volume": 5605762.113572997,
          "activeUsers": 11,
          "rank": "1"
        },
        {
          "dt": "2026-06-29T00:00:00Z",
          "builder": "betmoar",
          "builderCode": "0xceebf77a833b30520287ddd9478ff51abbdffa30aa90a8d655dba0e8a79ce0c1",
          "volume": 3888390.1247219997,
          "activeUsers": 168,
          "rank": "2"
        },
        {
          "dt": "2026-06-29T00:00:00Z",
          "builder": "MagicMarkets",
          "builderCode": "0xd1d9dd6983c40006b0dc8eab84a41ac9a4f27643296178479ffbebbc01ab7bde",
          "volume": 1765982.109793,
          "activeUsers": 1,
          "rank": "3"
        },
        "..."
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>
