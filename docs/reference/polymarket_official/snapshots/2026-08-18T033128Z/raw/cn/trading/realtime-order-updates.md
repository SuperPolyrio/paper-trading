> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 实时订单更新

> 实时响应经过身份验证的订单和交易活动。

使用实时订单更新，无需轮询即可保持交易账户视图最新。用户流会报告账户订单
的变化，以及这些订单撮合后产生的交易。有关公开订单簿和市场更新，请参阅
[实时数据](/cn/market-data/realtime-data)。

<Note>
  订阅前，请对要监控其活动的账户进行身份验证。请参阅
  [钱包与身份验证](/cn/trading/wallets-auth)。
</Note>

## 用户流

用户流会为经过身份验证的账户传送订单变更和交易更新。不提供市场筛选条件即可
关注整个账户，也可以提供条件 ID，仅关注选定市场。

<Tabs>
  <Tab title="TypeScript">
    给定一个 `SecureClient`，订阅 `user` 主题：

    ```ts theme={null}
    const stream = await client.subscribe([{ topic: "user" }]);

    for await (const event of stream) {
      switch (event.type) {
        case "order":
          // event: UserOrderEvent
          break;
        case "trade":
          // event: UserTradeEvent
          break;
      }
    }
    ```

    <Accordion title="用户事件">
      #### 订单更新

      <CodeGroup>
        ```ts UserOrderEvent Type theme={null}
        type UserOrderEvent = {
          topic: "user";
          type: "order";
          payload: {
            id: string;
            owner: string;
            market: string;
            tokenId: TokenId;
            side: OrderSide;
            orderOwner?: string | null;
            originalSize: DecimalString;
            sizeMatched: DecimalString;
            price: DecimalString;
            associateTrades?: string[] | null;
            outcome?: string | null;
            orderEventType: "PLACEMENT" | "UPDATE" | "CANCELLATION";
            createdAt?: IsoDateTimeString | null;
            expiresAt?: IsoDateTimeString | null;
            orderType?: "GTC" | "FOK" | "GTD" | "FAK" | null;
            status?: "LIVE" | "MATCHED" | "DELAYED" | "UNMATCHED" | "CANCELED" | null;
            makerAddress?: string | null;
            timestamp: EpochMilliseconds;
          };
        };
        ```

        ```json UserOrderEvent Example theme={null}
        {
          "topic": "user",
          "type": "order",
          "payload": {
            "id": "<order_id>",
            "owner": "<clob_api_key>",
            "market": "<condition_id>",
            "tokenId": "<token_id>",
            "side": "BUY",
            "originalSize": "10",
            "sizeMatched": "0",
            "price": "0.52",
            "outcome": "Yes",
            "orderEventType": "PLACEMENT",
            "status": "LIVE",
            "timestamp": 1782753357257
          }
        }
        ```
      </CodeGroup>

      #### 交易更新

      <CodeGroup>
        ```ts UserTradeEvent Type theme={null}
        type TradeMakerOrder = {
          orderId: string;
          owner: string;
          makerAddress?: string | null;
          matchedAmount: DecimalString;
          price: DecimalString;
          feeRateBps?: DecimalString | null;
          tokenId: TokenId;
          outcome?: string | null;
          outcomeIndex?: number | null;
          side: OrderSide;
        };

        type UserTradeEvent = {
          topic: "user";
          type: "trade";
          payload: {
            id: string;
            takerOrderId: string;
            market: string;
            tokenId: TokenId;
            side: OrderSide;
            size: DecimalString;
            feeRateBps?: DecimalString | null;
            price: DecimalString;
            status:
              | "TRADE_STATUS_MATCHED"
              | "TRADE_STATUS_MATCHED_NOT_BROADCASTED"
              | "TRADE_STATUS_MINED"
              | "TRADE_STATUS_CONFIRMED"
              | "TRADE_STATUS_RETRYING"
              | "TRADE_STATUS_FAILED";
            matchedAt?: IsoDateTimeString | null;
            updatedAt?: IsoDateTimeString | null;
            outcome?: string | null;
            owner: string;
            tradeOwner?: string | null;
            makerAddress?: string | null;
            transactionHash?: string | null;
            bucketIndex?: number | null;
            makerOrders?: TradeMakerOrder[] | null;
            traderSide?: "TAKER" | "MAKER" | null;
            timestamp: EpochMilliseconds;
          };
        };
        ```

        ```json UserTradeEvent Example theme={null}
        {
          "topic": "user",
          "type": "trade",
          "payload": {
            "id": "<trade_id>",
            "takerOrderId": "<order_id>",
            "market": "<condition_id>",
            "tokenId": "<token_id>",
            "side": "BUY",
            "size": "10",
            "price": "0.52",
            "status": "TRADE_STATUS_MATCHED",
            "owner": "<clob_api_key>",
            "traderSide": "TAKER",
            "timestamp": 1782753357257
          }
        }
        ```
      </CodeGroup>
    </Accordion>

    如需仅接收选定市场的更新，请在 `markets` 中传入其条件 ID：

    ```ts theme={null}
    const stream = await client.subscribe([
      {
        topic: "user",
        markets: ["<condition_id>"],
      },
    ]);
    ```
  </Tab>

  <Tab title="Python">
    给定一个 `AsyncSecureClient`，使用 `UserSpec` 进行订阅。同步
    `SecureClient` 不支持实时订阅。

    ```python theme={null}
    from polymarket.streams import UserSpec


    async with await client.subscribe(UserSpec()) as stream:
        async for event in stream:
            if event.type == "order":
                ...  # event: UserOrderEvent
            elif event.type == "trade":
                ...  # event: UserTradeEvent
    ```

    <Accordion title="用户事件">
      #### 订单更新

      <CodeGroup>
        ```python UserOrderEvent Type theme={null}
        class UserOrderPayload:
            id: str
            owner: str
            market: str
            token_id: TokenId
            side: Literal["BUY", "SELL"]
            order_owner: str | None
            original_size: Decimal
            size_matched: Decimal
            price: Decimal
            associate_trades: tuple[str, ...] | None
            outcome: str | None
            order_event_type: Literal["PLACEMENT", "UPDATE", "CANCELLATION"]
            created_at: datetime | None
            expires_at: datetime | None
            order_type: Literal["GTC", "FOK", "IOC", "GTD", "FAK"] | None
            status: Literal["LIVE", "MATCHED", "DELAYED", "UNMATCHED", "CANCELED"] | None
            maker_address: str | None
            timestamp: datetime | None

        class UserOrderEvent:
            topic: Literal["user"]
            type: Literal["order"]
            payload: UserOrderPayload
        ```

        ```json UserOrderEvent Example theme={null}
        {
          "topic": "user",
          "type": "order",
          "payload": {
            "id": "<order_id>",
            "owner": "<clob_api_key>",
            "market": "<condition_id>",
            "token_id": "<token_id>",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "0",
            "price": "0.52",
            "outcome": "Yes",
            "order_event_type": "PLACEMENT",
            "status": "LIVE",
            "timestamp": "2026-06-29T17:15:57.257000Z"
          }
        }
        ```
      </CodeGroup>

      #### 交易更新

      <CodeGroup>
        ```python UserTradeEvent Type theme={null}
        class UserTradeMakerOrder:
            order_id: str
            owner: str
            maker_address: str | None
            matched_amount: Decimal
            price: Decimal
            fee_rate_bps: Decimal | None
            token_id: TokenId
            outcome: str | None
            outcome_index: int | None
            side: Literal["BUY", "SELL"]

        class UserTradePayload:
            id: str
            taker_order_id: str
            market: str
            token_id: TokenId
            side: Literal["BUY", "SELL"]
            size: Decimal
            fee_rate_bps: Decimal | None
            price: Decimal
            status: Literal[
                "MATCHED",
                "MATCHED_NOT_BROADCASTED",
                "MINED",
                "CONFIRMED",
                "RETRYING",
                "FAILED",
            ]
            matched_at: datetime | None
            updated_at: datetime | None
            outcome: str | None
            owner: str
            trade_owner: str | None
            maker_address: str | None
            transaction_hash: str | None
            bucket_index: int | None
            maker_orders: tuple[UserTradeMakerOrder, ...] | None
            trader_side: Literal["TAKER", "MAKER"] | None
            timestamp: datetime | None

        class UserTradeEvent:
            topic: Literal["user"]
            type: Literal["trade"]
            payload: UserTradePayload
        ```

        ```json UserTradeEvent Example theme={null}
        {
          "topic": "user",
          "type": "trade",
          "payload": {
            "id": "<trade_id>",
            "taker_order_id": "<order_id>",
            "market": "<condition_id>",
            "token_id": "<token_id>",
            "side": "BUY",
            "size": "10",
            "price": "0.52",
            "status": "MATCHED",
            "owner": "<clob_api_key>",
            "trader_side": "TAKER",
            "timestamp": "2026-06-29T17:15:57.257000Z"
          }
        }
        ```
      </CodeGroup>
    </Accordion>

    如需仅接收选定市场的更新，请将其条件 ID 传给 `UserSpec`：

    ```python theme={null}
    async with await client.subscribe(
        UserSpec(markets=["<condition_id>"]),
    ) as stream:
        async for event in stream:
            ...
    ```
  </Tab>

  <Tab title="API">
    连接经过身份验证的用户 WebSocket：

    ```text theme={null}
    wss://ws-subscriptions-clob.polymarket.com/ws/user
    ```

    <Note>
      用户 WebSocket 使用应用层心跳。每 10 秒发送文本帧 `PING`；服务器会回复
      `PONG`。
    </Note>

    连接后，发送包含通过 [API 身份验证](/cn/getting-started/api#身份验证)
    创建的 CLOB API 凭据的 `user` 订阅帧：

    ```json theme={null}
    {
      "auth": {
        "apiKey": "<clob_api_key>",
        "secret": "<clob_api_secret>",
        "passphrase": "<clob_api_passphrase>"
      },
      "type": "user"
    }
    ```

    <Warning>连接后立即发送订阅帧。服务器可能会关闭一直未订阅的连接。</Warning>

    <Warning>
      将 CLOB API 凭据保存在服务器环境中。切勿在客户端代码中暴露它们。
    </Warning>

    在线上传输时，`event_type` 用于区分订单事件和交易事件。对于订单事件，
    `type` 表示订单是已下达、已更新还是已取消。

    <Accordion title="用户事件">
      #### 订单更新

      ```json theme={null}
      {
        "event_type": "order",
        "id": "<order_id>",
        "owner": "<clob_api_key>",
        "market": "<condition_id>",
        "asset_id": "<token_id>",
        "side": "BUY",
        "order_owner": "<clob_api_key>",
        "original_size": "10",
        "size_matched": "0",
        "price": "0.52",
        "associate_trades": null,
        "outcome": "Yes",
        "type": "PLACEMENT",
        "created_at": "1782753357",
        "expiration": "0",
        "order_type": "GTC",
        "status": "LIVE",
        "maker_address": "<maker_address>",
        "timestamp": "1782753357257"
      }
      ```

      #### 交易更新

      ```json theme={null}
      {
        "event_type": "trade",
        "type": "TRADE",
        "id": "<trade_id>",
        "taker_order_id": "<order_id>",
        "market": "<condition_id>",
        "asset_id": "<token_id>",
        "side": "BUY",
        "size": "10",
        "fee_rate_bps": "0",
        "price": "0.52",
        "status": "MATCHED",
        "match_time": "1782753357",
        "last_update": "1782753357",
        "outcome": "Yes",
        "owner": "<clob_api_key>",
        "trade_owner": "<clob_api_key>",
        "maker_address": "<maker_address>",
        "transaction_hash": null,
        "bucket_index": 0,
        "maker_orders": [
          {
            "order_id": "<maker_order_id>",
            "owner": "<maker_clob_api_key>",
            "maker_address": "<maker_address>",
            "matched_amount": "10",
            "price": "0.52",
            "fee_rate_bps": "0",
            "asset_id": "<token_id>",
            "outcome": "Yes",
            "outcome_index": 0,
            "side": "SELL"
          }
        ],
        "trader_side": "TAKER",
        "timestamp": "1782753357257"
      }
      ```
    </Accordion>

    如需仅接收选定市场的更新，请在初始订阅帧中包含其条件 ID：

    ```json theme={null}
    {
      "auth": {
        "apiKey": "<clob_api_key>",
        "secret": "<clob_api_secret>",
        "passphrase": "<clob_api_passphrase>"
      },
      "markets": ["<condition_id>"],
      "type": "user"
    }
    ```

    你可以在不重新连接的情况下添加或移除市场筛选条件：

    <CodeGroup>
      ```json Subscribe theme={null}
      {
        "operation": "subscribe",
        "markets": ["<condition_id>"]
      }
      ```

      ```json Unsubscribe theme={null}
      {
        "operation": "unsubscribe",
        "markets": ["<condition_id>"]
      }
      ```
    </CodeGroup>
  </Tab>
</Tabs>

## 理解订单更新

订单更新会说明账户的未成交订单状态为何发生变化：

| 更新 | 含义          |
| -- | ----------- |
| 下单 | 新订单已被接受。    |
| 更新 | 订单部分或全部成交。  |
| 取消 | 剩余未成交数量已取消。 |

## 理解交易更新

交易更新会跟踪一笔撮合直到链上结算。确认和永久失败属于终态；正在重试的
交易之后仍可能被打包并确认。

| 状态       | 终态 | 含义                  |
| -------- | -- | ------------------- |
| 已撮合，尚未广播 | 否  | 订单已撮合，但链上交易尚未广播。    |
| 已撮合      | 否  | 订单已撮合，交易已提交执行。      |
| 已打包      | 否  | 已在链上观察到交易，但尚未达到最终性。 |
| 已确认      | 是  | 交易已成功达到最终性。         |
| 正在重试     | 否  | 结算暂时失败，正在重试。        |
| 失败       | 是  | 结算永久失败。             |

## 重新连接后恢复

实时更新不能取代权威账户读取，也不会重放断开连接期间错过的每项变更。
重新连接后，从[管理订单](/cn/trading/manage-orders)获取账户的未成交订单和
近期交易，然后从刷新后的状态继续应用新的流事件。
