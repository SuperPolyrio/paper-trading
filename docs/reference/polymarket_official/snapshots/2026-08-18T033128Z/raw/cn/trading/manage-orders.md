> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 管理订单

> 了解如何在提交订单后跟踪和管理订单。

提交订单后，使用经过身份验证的账户读取来检查订单当前状态、核对未成交订单及由此产生的交易，并取消不再希望挂在订单簿上的流动性。

## 获取单个订单

按 ID 查询单个订单。适合用于检查已知订单的当前状态，而不必扫描完整的未成交订单列表。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `fetchOrder()`，按 ID 获取订单：

    ```ts theme={null}
    const order = await client.fetchOrder({ orderId: "ORDER_ID" });

    // order: OpenOrder
    ```

    该方法返回一个 `OpenOrder`，其中包含经过规范化的代币、小数和日期值：

    <CodeGroup>
      ```ts OpenOrder Type theme={null}
      type OpenOrder = {
        id: string;
        conditionId: CtfConditionId;
        tokenId: TokenId;
        owner: string;
        makerAddress: string;
        side: string;
        price: DecimalString;
        originalSize: DecimalString;
        sizeMatched: DecimalString;
        outcome: string;
        orderType: string;
        status: string;
        associateTrades: string[];
        createdAt: IsoDateTimeString;
        expiresAt?: IsoDateTimeString;
      };
      ```

      ```json OpenOrder Example theme={null}
      {
        "id": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
        "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
        "makerAddress": "0x1234567890123456789012345678901234567890",
        "side": "BUY",
        "price": "0.52",
        "originalSize": "10",
        "sizeMatched": "0",
        "outcome": "Yes",
        "orderType": "GTD",
        "status": "LIVE",
        "associateTrades": [],
        "createdAt": "2026-06-01T12:00:00.000Z",
        "expiresAt": "2026-06-01T13:00:00.000Z"
      }
      ```
    </CodeGroup>

    响应包含到期时间时会提供 `expiresAt`；否则省略该字段。
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `get_order()`，按 ID 获取订单。同步 `SecureClient` 提供相同的方法。

    ```python theme={null}
    order = await client.get_order(order_id="ORDER_ID")

    # order: OpenOrder
    ```

    该方法返回一个 `OpenOrder`，其中包含经过规范化的代币、小数和日期值：

    <CodeGroup>
      ```python OpenOrder Type theme={null}
      class OpenOrder:
          id: str
          condition_id: CtfConditionId
          token_id: TokenId
          owner: str
          maker_address: str
          side: Literal["BUY", "SELL"]
          price: Decimal
          original_size: Decimal
          size_matched: Decimal
          outcome: str
          order_type: str
          status: str
          associate_trades: tuple[str, ...]
          created_at: datetime
          expires_at: datetime | None
      ```

      ```json OpenOrder Example theme={null}
      {
        "id": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
        "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
        "maker_address": "0x1234567890123456789012345678901234567890",
        "side": "BUY",
        "price": "0.52",
        "original_size": "10",
        "size_matched": "0",
        "outcome": "Yes",
        "order_type": "GTD",
        "status": "LIVE",
        "associate_trades": [],
        "created_at": "2026-06-01T12:00:00Z",
        "expires_at": "2026-06-01T13:00:00Z"
      }
      ```
    </CodeGroup>

    返回到期时间时，`expires_at` 包含规范化后的值；否则可以为 `None`。
  </Tab>

  <Tab title="API">
    通过经过身份验证的 CLOB 请求获取订单。完整签名流程请参阅 [API 身份验证](/cn/getting-started/api#身份验证)。

    ```bash theme={null}
    curl "https://clob.polymarket.com/data/order/$ORDER_ID" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>"
    ```

    其中，`<clob_request_timestamp>` 是 Unix 时间戳，`<clob_l2_signature>` 按以下方式生成：

    ```text theme={null}
    message = <clob_request_timestamp> + "GET" + "/data/order/$ORDER_ID"

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    CLOB 以传输响应格式返回订单：

    <CodeGroup>
      ```ts Order Type theme={null}
      type Order = {
        id: string;
        market: string;
        asset_id: string;
        owner: string;
        maker_address: string;
        side: string;
        price: string;
        original_size: string;
        size_matched: string;
        outcome: string;
        order_type: string;
        status: string;
        associate_trades?: string[];
        created_at: number;
        expiration: string;
      };
      ```

      ```json Order Example theme={null}
      {
        "id": "ORDER_ID",
        "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
        "owner": "0x1234...",
        "maker_address": "0x1234...",
        "side": "BUY",
        "price": "0.52",
        "original_size": "10",
        "size_matched": "0",
        "outcome": "Yes",
        "order_type": "GTC",
        "status": "LIVE",
        "associate_trades": [],
        "created_at": 1748779200,
        "expiration": "0"
      }
      ```
    </CodeGroup>

    `expiration` 为 `"0"` 表示该订单是 GTC 订单，不会按计时器到期。
  </Tab>
</Tabs>

请参阅[订单状态](/cn/concepts/order-lifecycle#订单状态)，了解返回的 `status`。

## 列出未成交订单

检索账户中的所有挂单，也可以缩小到某个代币、市场或特定订单 ID。用它来核对本地状态与订单簿上的实际有效订单。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `listOpenOrders()`，遍历账户的未成交订单：

    ```ts theme={null}
    const pages = client.listOpenOrders();

    for await (const page of pages) {
      // page.items: OpenOrder[]
    }
    ```

    传入筛选条件以缩小结果范围。虽然参数名为 `market`，但它接收市场的条件 ID：

    <CodeGroup>
      ```ts Token theme={null}
      const pages = client.listOpenOrders({
        tokenId: market.outcomes.yes.tokenId!,
      });
      ```

      ```ts Condition theme={null}
      const pages = client.listOpenOrders({
        market: market.conditionId!,
      });
      ```

      ```ts Order ID theme={null}
      const pages = client.listOpenOrders({
        id: order.id,
      });
      ```
    </CodeGroup>

    每一页都包含 `OpenOrder` 项：

    <CodeGroup>
      ```ts OpenOrder Type theme={null}
      type OpenOrder = {
        id: string;
        conditionId: CtfConditionId;
        tokenId: TokenId;
        owner: string;
        makerAddress: string;
        side: string;
        price: DecimalString;
        originalSize: DecimalString;
        sizeMatched: DecimalString;
        outcome: string;
        orderType: string;
        status: string;
        associateTrades: string[];
        createdAt: IsoDateTimeString;
        expiresAt?: IsoDateTimeString;
      };
      ```

      ```json OpenOrder Example theme={null}
      {
        "items": [
          {
            "id": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
            "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
            "makerAddress": "0x1234567890123456789012345678901234567890",
            "side": "BUY",
            "price": "0.52",
            "originalSize": "10",
            "sizeMatched": "0",
            "outcome": "Yes",
            "orderType": "GTD",
            "status": "LIVE",
            "associateTrades": [],
            "createdAt": "2026-06-01T12:00:00.000Z",
            "expiresAt": "2026-06-01T13:00:00.000Z"
          }
        ],
        "hasMore": false,
        "totalCount": 1
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `list_open_orders()`，遍历账户的未成交订单。同步 `SecureClient` 提供相同的方法。

    ```python theme={null}
    pages = client.list_open_orders()

    async for page in pages:
        # page.items: tuple[OpenOrder, ...]
    ```

    传入筛选条件以缩小结果范围。虽然参数名为 `market`，但它接收市场的条件 ID：

    <CodeGroup>
      ```python Token theme={null}
      pages = client.list_open_orders(
          token_id=market.outcomes.yes.token_id,
      )
      ```

      ```python Condition theme={null}
      pages = client.list_open_orders(
          market=market.condition_id,
      )
      ```

      ```python Order ID theme={null}
      pages = client.list_open_orders(
          id=order.id,
      )
      ```
    </CodeGroup>

    每一页都包含 `OpenOrder` 项：

    <CodeGroup>
      ```python OpenOrder Type theme={null}
      class OpenOrder:
          id: str
          condition_id: CtfConditionId
          token_id: TokenId
          owner: str
          maker_address: str
          side: Literal["BUY", "SELL"]
          price: Decimal
          original_size: Decimal
          size_matched: Decimal
          outcome: str
          order_type: str
          status: str
          associate_trades: tuple[str, ...]
          created_at: datetime
          expires_at: datetime | None
      ```

      ```json OpenOrder Example theme={null}
      {
        "items": [
          {
            "id": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
            "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
            "maker_address": "0x1234567890123456789012345678901234567890",
            "side": "BUY",
            "price": "0.52",
            "original_size": "10",
            "size_matched": "0",
            "outcome": "Yes",
            "order_type": "GTD",
            "status": "LIVE",
            "associate_trades": [],
            "created_at": "2026-06-01T12:00:00Z",
            "expires_at": "2026-06-01T13:00:00Z"
          }
        ],
        "has_more": false,
        "next_cursor": null,
        "total_count": 1
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    通过经过身份验证的 CLOB 请求列出未成交订单，并可按 `id`、`market` 或 `asset_id` 筛选。完整签名流程请参阅 [API 身份验证](/cn/getting-started/api#身份验证)。

    ```bash theme={null}
    curl "https://clob.polymarket.com/data/orders?asset_id=$YES_TOKEN_ID" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>"
    ```

    其中，`<clob_request_timestamp>` 是 Unix 时间戳，`<clob_l2_signature>` 按以下方式生成：

    ```text theme={null}
    message = <clob_request_timestamp> + "GET" + "/data/orders"

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    CLOB 以传输响应格式返回匹配的订单：

    <CodeGroup>
      ```ts Order Type theme={null}
      type Order = {
        id: string;
        market: string;
        asset_id: string;
        owner: string;
        maker_address: string;
        side: string;
        price: string;
        original_size: string;
        size_matched: string;
        outcome: string;
        order_type: string;
        status: string;
        associate_trades?: string[];
        created_at: number;
        expiration: string;
      };
      ```

      ```json Order Example theme={null}
      [
        {
          "id": "ORDER_ID",
          "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "owner": "0x1234...",
          "maker_address": "0x1234...",
          "side": "BUY",
          "price": "0.52",
          "original_size": "10",
          "size_matched": "0",
          "outcome": "Yes",
          "order_type": "GTC",
          "status": "LIVE",
          "associate_trades": [],
          "created_at": 1748779200,
          "expiration": "0"
        }
      ]
      ```
    </CodeGroup>
  </Tab>
</Tabs>

请参阅[订单状态](/cn/concepts/order-lifecycle#订单状态)，了解每个订单的 `status`。

## 列出账户交易

检索账户已执行的成交记录，而不是仍挂在订单簿上的订单。用它来重建成交历史、计算已实现的头寸规模，或审计实际撮合情况。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `listAccountTrades()`，遍历账户的交易：

    ```ts theme={null}
    const pages = client.listAccountTrades();

    for await (const page of pages) {
      // page.items: ClobTrade[]
    }
    ```

    传入筛选条件以缩小结果范围。虽然参数名为 `market`，但它接收市场的条件 ID：

    <CodeGroup>
      ```ts Token theme={null}
      const pages = client.listAccountTrades({
        tokenId: market.outcomes.yes.tokenId!,
      });
      ```

      ```ts Condition theme={null}
      const pages = client.listAccountTrades({
        market: market.conditionId!,
      });
      ```

      ```ts Maker theme={null}
      const pages = client.listAccountTrades({
        makerAddress: process.env.POLYMARKET_MAKER_ADDRESS!,
      });
      ```

      ```ts Time Range theme={null}
      const now = Math.floor(Date.now() / 1000);

      const pages = client.listAccountTrades({
        after: String(now - 24 * 60 * 60),
        before: String(now),
      });
      ```
    </CodeGroup>

    每一页都包含 `ClobTrade` 项：

    <CodeGroup>
      ```ts ClobTrade Type theme={null}
      type MakerOrder = {
        orderId: string;
        tokenId: TokenId;
        makerAddress: string;
        owner: string;
        side: string;
        price: DecimalString;
        matchedAmount: DecimalString;
        outcome: string;
        feeRateBps: DecimalString | null;
      };

      type ClobTrade = {
        id: string;
        conditionId: CtfConditionId;
        tokenId: TokenId;
        owner: string;
        makerAddress: string;
        takerOrderId: string;
        side: string;
        traderSide: "TAKER" | "MAKER";
        price: DecimalString;
        size: DecimalString;
        outcome: string;
        status: string;
        feeRateBps: DecimalString;
        bucketIndex: number;
        transactionHash: string;
        makerOrders: MakerOrder[];
        matchedAt: IsoDateTimeString;
        updatedAt: IsoDateTimeString;
      };
      ```

      ```json ClobTrade Example theme={null}
      {
        "items": [
          {
            "id": "TRADE_ID",
            "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
            "makerAddress": "0x1234567890123456789012345678901234567890",
            "takerOrderId": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
            "side": "BUY",
            "traderSide": "TAKER",
            "price": "0.52",
            "size": "10",
            "outcome": "Yes",
            "status": "TRADE_STATUS_MATCHED",
            "feeRateBps": "0",
            "bucketIndex": 0,
            "transactionHash": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "makerOrders": [],
            "matchedAt": "2026-06-01T12:00:05.000Z",
            "updatedAt": "2026-06-01T12:00:05.000Z"
          }
        ],
        "hasMore": false,
        "totalCount": 1
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `list_account_trades()`，遍历账户的交易。同步 `SecureClient` 提供相同的方法。

    ```python theme={null}
    pages = client.list_account_trades()

    async for page in pages:
        # page.items: tuple[ClobTrade, ...]
    ```

    传入筛选条件以缩小结果范围。虽然参数名为 `market`，但它接收市场的条件 ID：

    <CodeGroup>
      ```python Token theme={null}
      pages = client.list_account_trades(
          token_id=market.outcomes.yes.token_id,
      )
      ```

      ```python Condition theme={null}
      pages = client.list_account_trades(
          market=market.condition_id,
      )
      ```

      ```python Maker theme={null}
      pages = client.list_account_trades(
          maker_address=os.environ["POLYMARKET_MAKER_ADDRESS"],
      )
      ```

      ```python Time Range theme={null}
      import time

      now = int(time.time())

      pages = client.list_account_trades(
          after=str(now - 24 * 60 * 60),
          before=str(now),
      )
      ```
    </CodeGroup>

    每一页都包含 `ClobTrade` 项：

    <CodeGroup>
      ```python ClobTrade Type theme={null}
      class MakerOrder:
          order_id: str
          token_id: TokenId
          maker_address: str
          owner: str
          side: Literal["BUY", "SELL"]
          price: Decimal
          matched_amount: Decimal
          outcome: str
          fee_rate_bps: Decimal | None

      class ClobTrade:
          id: str
          condition_id: CtfConditionId
          token_id: TokenId
          owner: str
          maker_address: str
          taker_order_id: str
          side: Literal["BUY", "SELL"]
          trader_side: Literal["TAKER", "MAKER"]
          price: Decimal
          size: Decimal
          outcome: str
          status: str
          fee_rate_bps: Decimal
          bucket_index: int
          transaction_hash: str
          maker_orders: tuple[MakerOrder, ...]
          matched_at: datetime
          updated_at: datetime
      ```

      ```json ClobTrade Example theme={null}
      {
        "items": [
          {
            "id": "TRADE_ID",
            "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
            "maker_address": "0x1234567890123456789012345678901234567890",
            "taker_order_id": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
            "side": "BUY",
            "trader_side": "TAKER",
            "price": "0.52",
            "size": "10",
            "outcome": "Yes",
            "status": "TRADE_STATUS_MATCHED",
            "fee_rate_bps": "0",
            "bucket_index": 0,
            "transaction_hash": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "maker_orders": [],
            "matched_at": "2026-06-01T12:00:05Z",
            "updated_at": "2026-06-01T12:00:05Z"
          }
        ],
        "has_more": false,
        "next_cursor": null,
        "total_count": 1
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    通过经过身份验证的 CLOB 请求列出账户交易，并可按 `id`、`market`、`asset_id`、`maker_address`、`after` 或 `before` 筛选。完整签名流程请参阅 [API 身份验证](/cn/getting-started/api#身份验证)。

    ```bash theme={null}
    curl "https://clob.polymarket.com/data/trades?market=$CONDITION_ID" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>"
    ```

    其中，`<clob_request_timestamp>` 是 Unix 时间戳，`<clob_l2_signature>` 按以下方式生成：

    ```text theme={null}
    message = <clob_request_timestamp> + "GET" + "/data/trades"

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    CLOB 以传输响应格式返回匹配的交易：

    <CodeGroup>
      ```ts Trade Type theme={null}
      type MakerOrder = {
        order_id: string;
        asset_id: string;
        maker_address: string;
        owner: string;
        side: string;
        price: string;
        matched_amount: string;
        outcome: string;
        fee_rate_bps: string;
      };

      type Trade = {
        id: string;
        market: string;
        asset_id: string;
        owner: string;
        maker_address: string;
        taker_order_id: string;
        side: string;
        trader_side: string;
        price: string;
        size: string;
        outcome: string;
        status: string;
        fee_rate_bps?: string;
        bucket_index: number;
        transaction_hash?: string;
        maker_orders?: MakerOrder[];
        match_time: string;
        last_update: string;
      };
      ```

      ```json Trade Example theme={null}
      [
        {
          "id": "TRADE_ID",
          "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "owner": "0x1234...",
          "maker_address": "0x1234...",
          "taker_order_id": "ORDER_ID",
          "side": "BUY",
          "trader_side": "TAKER",
          "price": "0.52",
          "size": "10",
          "outcome": "Yes",
          "status": "MATCHED",
          "fee_rate_bps": "0",
          "bucket_index": 0,
          "transaction_hash": "0xabc...",
          "maker_orders": [],
          "match_time": "1748779205",
          "last_update": "1748779205"
        }
      ]
      ```
    </CodeGroup>
  </Tab>
</Tabs>

请参阅[交易状态](/cn/concepts/order-lifecycle#交易状态)，跟踪每笔交易的 `status` 直至结算。

## 列出 Builder 交易

检索归因于某个 Builder 代码、且来自应用所服务账户的已撮合交易。当你需要核对整个 Builder 集成的成交情况或衡量其活动时，请使用此读取，而不是账户交易读取。订单在撮合之前不会出现在这里。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listBuilderTrades()`，遍历归因于你的 Builder 代码的交易：

    ```ts theme={null}
    const pages = client.listBuilderTrades({
      builderCode: process.env.POLYMARKET_BUILDER_CODE!,
    });

    for await (const page of pages) {
      // page.items: BuilderTrade[]
    }
    ```

    传入筛选条件以缩小结果范围：

    <CodeGroup>
      ```ts Token theme={null}
      const pages = client.listBuilderTrades({
        builderCode: process.env.POLYMARKET_BUILDER_CODE!,
        tokenId: market.outcomes.yes.tokenId!,
      });
      ```

      ```ts Condition theme={null}
      const pages = client.listBuilderTrades({
        builderCode: process.env.POLYMARKET_BUILDER_CODE!,
        market: market.conditionId!,
      });
      ```

      ```ts Trade ID theme={null}
      const pages = client.listBuilderTrades({
        builderCode: process.env.POLYMARKET_BUILDER_CODE!,
        id: trade.id,
      });
      ```

      ```ts Time Range theme={null}
      const now = Math.floor(Date.now() / 1000);

      const pages = client.listBuilderTrades({
        builderCode: process.env.POLYMARKET_BUILDER_CODE!,
        after: String(now - 24 * 60 * 60),
        before: String(now),
      });
      ```
    </CodeGroup>

    每一页都包含 `BuilderTrade` 项：

    <CodeGroup>
      ```ts BuilderTrade Type theme={null}
      type BuilderTrade = {
        id: string;
        tradeType: string;
        takerOrderHash: string;
        builder: string;
        conditionId: CtfConditionId;
        tokenId: TokenId;
        side: "BUY" | "SELL";
        size: DecimalString;
        sizeUsdc: DecimalString;
        price: DecimalString;
        status: string;
        outcome: string;
        outcomeIndex: number;
        owner: string;
        maker: string;
        transactionHash: string;
        matchedAt: IsoDateTimeString;
        bucketIndex: number;
        fee: DecimalString;
        feeUsdc: DecimalString;
        errMsg?: string | null;
        createdAt?: IsoDateTimeString;
        updatedAt?: IsoDateTimeString;
      };
      ```

      ```json BuilderTrade Example theme={null}
      {
        "items": [
          {
            "id": "TRADE_ID",
            "tradeType": "TAKER",
            "takerOrderHash": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
            "builder": "0x1111111111111111111111111111111111111111111111111111111111111111",
            "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "side": "BUY",
            "size": "10",
            "sizeUsdc": "5.2",
            "price": "0.52",
            "status": "TRADE_STATUS_MATCHED",
            "outcome": "Yes",
            "outcomeIndex": 0,
            "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
            "maker": "0x1234567890123456789012345678901234567890",
            "transactionHash": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "matchedAt": "2026-06-01T12:00:05.000Z",
            "bucketIndex": 0,
            "fee": "0",
            "feeUsdc": "0",
            "errMsg": null,
            "createdAt": "2026-06-01T12:00:05.000Z",
            "updatedAt": "2026-06-01T12:00:05.000Z"
          }
        ],
        "hasMore": false,
        "totalCount": 1
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_builder_trades()`，遍历归因于你的 Builder 代码的交易。同步 `PublicClient` 和 `SecureClient` 提供相同的方法。

    ```python theme={null}
    pages = client.list_builder_trades(
        builder_code=os.environ["POLYMARKET_BUILDER_CODE"],
    )

    async for page in pages:
        # page.items: tuple[BuilderTrade, ...]
    ```

    传入筛选条件以缩小结果范围：

    <CodeGroup>
      ```python Token theme={null}
      pages = client.list_builder_trades(
          builder_code=os.environ["POLYMARKET_BUILDER_CODE"],
          token_id=market.outcomes.yes.token_id,
      )
      ```

      ```python Condition theme={null}
      pages = client.list_builder_trades(
          builder_code=os.environ["POLYMARKET_BUILDER_CODE"],
          market=market.condition_id,
      )
      ```

      ```python Trade ID theme={null}
      pages = client.list_builder_trades(
          builder_code=os.environ["POLYMARKET_BUILDER_CODE"],
          id=trade.id,
      )
      ```

      ```python Time Range theme={null}
      import time

      now = int(time.time())

      pages = client.list_builder_trades(
          builder_code=os.environ["POLYMARKET_BUILDER_CODE"],
          after=str(now - 24 * 60 * 60),
          before=str(now),
      )
      ```
    </CodeGroup>

    每一页都包含 `BuilderTrade` 项：

    <CodeGroup>
      ```python BuilderTrade Type theme={null}
      class BuilderTrade:
          id: str
          trade_type: str
          taker_order_hash: str
          builder: str
          condition_id: CtfConditionId
          token_id: TokenId
          side: Literal["BUY", "SELL"]
          size: Decimal
          size_usdc: Decimal
          price: Decimal
          status: str
          outcome: str
          outcome_index: int
          owner: str
          maker: str
          transaction_hash: str
          matched_at: datetime
          bucket_index: int
          fee: Decimal
          fee_usdc: Decimal
          error_msg: str | None
          created_at: datetime | None
          updated_at: datetime | None
      ```

      ```json BuilderTrade Example theme={null}
      {
        "items": [
          {
            "id": "TRADE_ID",
            "trade_type": "TAKER",
            "taker_order_hash": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
            "builder": "0x1111111111111111111111111111111111111111111111111111111111111111",
            "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "side": "BUY",
            "size": "10",
            "size_usdc": "5.2",
            "price": "0.52",
            "status": "TRADE_STATUS_MATCHED",
            "outcome": "Yes",
            "outcome_index": 0,
            "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
            "maker": "0x1234567890123456789012345678901234567890",
            "transaction_hash": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "matched_at": "2026-06-01T12:00:05Z",
            "bucket_index": 0,
            "fee": "0",
            "fee_usdc": "0",
            "error_msg": null,
            "created_at": "2026-06-01T12:00:05Z",
            "updated_at": "2026-06-01T12:00:05Z"
          }
        ],
        "has_more": false,
        "next_cursor": null,
        "total_count": 1
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    通过公开 CLOB 请求列出归因于某个 Builder 代码的交易。需要时，可使用 `id`、`market`、`asset_id`、`after` 或 `before` 筛选结果：

    ```bash theme={null}
    curl "https://clob.polymarket.com/builder/trades?builder_code=$POLYMARKET_BUILDER_CODE"
    ```

    响应包含匹配的交易以及下一页的游标：

    ```json theme={null}
    {
      "limit": 300,
      "next_cursor": "LTE=",
      "count": 1,
      "data": [
        {
          "id": "TRADE_ID",
          "tradeType": "TAKER",
          "takerOrderHash": "0xff354cd7ca7539dfa9c28d90943ab5779a4eac34b9b37a757d7b32bdfb11790b",
          "builder": "0x1111111111111111111111111111111111111111111111111111111111111111",
          "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "assetId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "side": "BUY",
          "size": "10",
          "sizeUsdc": "5.2",
          "price": "0.52",
          "status": "TRADE_STATUS_MATCHED",
          "outcome": "Yes",
          "outcomeIndex": 0,
          "owner": "9180014b-33c8-9240-a14b-bdca11c0a465",
          "maker": "0x1234567890123456789012345678901234567890",
          "transactionHash": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd",
          "matchTime": "1748779205",
          "bucketIndex": 0,
          "fee": "0",
          "feeUsdc": "0",
          "err_msg": null,
          "createdAt": "2026-06-01T12:00:05Z",
          "updatedAt": "2026-06-01T12:00:05Z"
        }
      ]
    }
    ```

    将 `next_cursor` 作为 `next_cursor` 查询参数传回以请求下一页。终止游标为 `LTE=`。
  </Tab>
</Tabs>

请参阅 [Builder 计划](/cn/programs/builders/overview)，了解归因的工作原理。

## 取消订单

仅按所需范围取消订单。优先使用已知订单 ID；撤回一组报价时使用市场或代币筛选条件；只有在特殊情况下才取消整个账户的订单。当交易所在仅允许取消模式下拒绝新订单时，取消操作仍然可用。对于部分成交的订单，取消操作只会移除其未成交部分。

### 取消订单 by ID

知道订单 ID 时，可取消单个订单。若要通过一次请求取消一组已知订单，请提交最多 3,000 个 ID 的批次。批次中的重复 ID 会被忽略。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `cancelOrder()`，取消单个订单：

    ```ts theme={null}
    const result = await client.cancelOrder({ orderId: order.id });

    // result: CancelOrdersResponse
    ```

    调用 `cancelOrders()`，在一次请求中取消多个已知订单：

    ```ts theme={null}
    const result = await client.cancelOrders({
      orderIds: orders.map((order) => order.id),
    });

    // result: CancelOrdersResponse
    ```
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `cancel_order()`，取消单个订单。同步 `SecureClient` 提供相同的方法，但无需使用 `await`。

    ```python theme={null}
    result = await client.cancel_order(order_id=order.id)

    # result: CancelOrdersResponse
    ```

    调用 `cancel_orders()`，在一次请求中取消多个已知订单：

    ```python theme={null}
    result = await client.cancel_orders(
        order_ids=[order.id for order in orders],
    )

    # result: CancelOrdersResponse
    ```
  </Tab>

  <Tab title="API">
    通过经过身份验证的 CLOB 请求取消单个订单。完整签名流程请参阅 [API 身份验证](/cn/getting-started/api#身份验证)。

    ```bash theme={null}
    curl -X DELETE "https://clob.polymarket.com/order" \
      -H "Content-Type: application/json" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>" \
      --data '{"orderID":"'"$ORDER_ID"'"}'
    ```

    其中，`<clob_request_timestamp>` 是 Unix 时间戳，`<clob_l2_signature>` 根据精确序列化后的请求体生成：

    ```text theme={null}
    body = '{"orderID":"' + order_id + '"}'
    message = <clob_request_timestamp> + "DELETE" + "/order" + body

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    若要取消多个订单，请发送包含 1 至 3,000 个订单 ID 的数组：

    ```bash theme={null}
    curl -X DELETE "https://clob.polymarket.com/orders" \
      -H "Content-Type: application/json" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>" \
      --data '["'"$ORDER_ID_1"'","'"$ORDER_ID_2"'"]'
    ```

    使用批量请求的路径和精确序列化后的数组进行签名：

    ```text theme={null}
    body = '["' + order_id_1 + '","' + order_id_2 + '"]'
    message = <clob_request_timestamp> + "DELETE" + "/orders" + body

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    两个请求都会返回 `200 OK`，并附带包含 `canceled` 和 `not_canceled` 的[取消结果](#取消结果)。
  </Tab>
</Tabs>

### 取消市场订单

按代币取消可撤回某个结果的订单；按市场取消可撤回该条件下所有结果的订单。必须提供至少一个筛选条件。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `cancelMarketOrders()`，并传入代币 ID 或条件 ID：

    <CodeGroup>
      ```ts Token theme={null}
      const result = await client.cancelMarketOrders({
        tokenId: market.outcomes.yes.tokenId!,
      });
      ```

      ```ts Condition theme={null}
      const result = await client.cancelMarketOrders({
        market: market.conditionId!,
      });
      ```
    </CodeGroup>

    `market` 字段接收市场的条件 ID。
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `cancel_market_orders()`，并传入代币 ID 或条件 ID。同步 `SecureClient` 提供相同的方法，但无需使用 `await`。

    <CodeGroup>
      ```python Token theme={null}
      result = await client.cancel_market_orders(
          token_id=market.outcomes.yes.token_id,
      )
      ```

      ```python Condition theme={null}
      result = await client.cancel_market_orders(
          market=market.condition_id,
      )
      ```
    </CodeGroup>

    `market` 参数接收市场的条件 ID。
  </Tab>

  <Tab title="API">
    通过经过身份验证的 CLOB 请求取消某个结果的所有未成交订单。完整签名流程请参阅 [API 身份验证](/cn/getting-started/api#身份验证)。

    ```bash theme={null}
    curl -X DELETE "https://clob.polymarket.com/cancel-market-orders" \
      -H "Content-Type: application/json" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>" \
      --data '{"asset_id":"'"$TOKEN_ID"'"}'
    ```

    其中，`<clob_request_timestamp>` 是 Unix 时间戳，`<clob_l2_signature>` 根据精确序列化后的请求体生成：

    ```text theme={null}
    body = '{"asset_id":"' + token_id + '"}'
    message = <clob_request_timestamp> + "DELETE" + "/cancel-market-orders" + body

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    若要取消某个条件的所有未成交订单，请将其条件 ID 作为 `market` 发送，并改为对该精确请求体签名：

    ```bash theme={null}
    curl -X DELETE "https://clob.polymarket.com/cancel-market-orders" \
      -H "Content-Type: application/json" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>" \
      --data '{"market":"'"$CONDITION_ID"'"}'
    ```

    使用相同路径和精确序列化后的请求体，对限定到该条件的请求进行签名：

    ```text theme={null}
    body = '{"market":"' + condition_id + '"}'
    message = <clob_request_timestamp> + "DELETE" + "/cancel-market-orders" + body

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    两个请求都会返回 `200 OK`，并附带包含 `canceled` 和 `not_canceled` 的[取消结果](#取消结果)。
  </Tab>
</Tabs>

### 取消所有订单

需要移除所有挂单时（例如紧急停机期间），请使用账户级取消。

<Warning>
  此操作会取消与经过身份验证的 CLOB API 凭据关联的所有未成交订单。
</Warning>

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `cancelAll()`：

    ```ts theme={null}
    const result = await client.cancelAll();

    // result: CancelOrdersResponse
    ```
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `cancel_all()`。同步 `SecureClient` 提供相同的方法，但无需使用 `await`。

    ```python theme={null}
    result = await client.cancel_all()

    # result: CancelOrdersResponse
    ```
  </Tab>

  <Tab title="API">
    通过经过身份验证的 CLOB 请求取消所有未成交订单。完整签名流程请参阅 [API 身份验证](/cn/getting-started/api#身份验证)。

    ```bash theme={null}
    curl -X DELETE "https://clob.polymarket.com/cancel-all" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>"
    ```

    其中，`<clob_request_timestamp>` 是 Unix 时间戳，`<clob_l2_signature>` 在没有请求体的情况下生成：

    ```text theme={null}
    message = <clob_request_timestamp> + "DELETE" + "/cancel-all"

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    该请求返回 `200 OK`，并附带包含 `canceled` 和 `not_canceled` 的[取消结果](#取消结果)。
  </Tab>
</Tabs>

### 取消结果

取消操作可能只对部分请求的订单成功。重试前请核对结果：其中会标识已取消的订单，并给出每个无法取消的订单的原因。

<Tabs>
  <Tab title="TypeScript">
    TypeScript SDK 返回 `CancelOrdersResponse`：

    ```ts Type theme={null}
    type CancelOrdersResponse = {
      canceled: OrderId[];
      notCanceled: Record<OrderId, string>;
    };
    ```

    在此示例中，一个订单已取消，另一个订单因已撮合而无法取消：

    ```json Example theme={null}
    {
      "canceled": ["ORDER_ID_1"],
      "notCanceled": {
        "ORDER_ID_2": "order already matched"
      }
    }
    ```
  </Tab>

  <Tab title="Python">
    Python SDK 返回 `CancelOrdersResponse`：

    ```python Type theme={null}
    class CancelOrdersResponse:
        canceled: tuple[OrderId, ...]
        not_canceled: dict[OrderId, str]
    ```

    在此示例中，一个订单已取消，另一个订单因已撮合而无法取消：

    ```json Example theme={null}
    {
      "canceled": ["ORDER_ID_1"],
      "not_canceled": {
        "ORDER_ID_2": "order already matched"
      }
    }
    ```
  </Tab>

  <Tab title="API">
    API 以 JSON 返回取消结果。此响应显示一个已取消订单和一个已撮合订单：

    ```json Response theme={null}
    {
      "canceled": ["ORDER_ID_1"],
      "not_canceled": {
        "ORDER_ID_2": "order already matched"
      }
    }
    ```
  </Tab>
</Tabs>

## 订单心跳

使用订单心跳，在交易进程停止响应时自动取消挂单。首次心跳被接受后，CLOB 会要求账户持续发送心跳；如果 10 秒内未收到有效心跳，则会取消这些 CLOB API 凭据所拥有的全部未成交订单。取消检查每五秒运行一次，因此最晚可能在超时后五秒才执行取消。

<Tabs>
  <Tab title="API">
    每 **5 秒**发送一次心跳。

    <Steps>
      <Step title="启动心跳">
        向 `POST /v1/heartbeats` 发送空的 `heartbeat_id`。使用拥有待保护订单的同一组 CLOB API 凭据对请求进行身份验证：

        ```bash theme={null}
        curl -X POST "https://clob.polymarket.com/v1/heartbeats" \
          -H "Content-Type: application/json" \
          -H "POLY_ADDRESS: <signer_address>" \
          -H "POLY_API_KEY: <clob_api_key>" \
          -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
          -H "POLY_SIGNATURE: <clob_l2_signature>" \
          -H "POLY_TIMESTAMP: <clob_request_timestamp>" \
          --data '{"heartbeat_id":""}'
        ```

        使用 [API 身份验证](/cn/getting-started/api#身份验证)中的签名者地址和 CLOB API 凭据，创建新的 `<clob_request_timestamp>`，并根据通过网络发送的精确序列化请求体生成 `<clob_l2_signature>`：

        ```text theme={null}
        body = '{"heartbeat_id":""}'
        message = <clob_request_timestamp> + "POST" + "/v1/heartbeats" + body

        clob_l2_signature = urlsafeBase64WithPadding(
          HMAC-SHA256(base64Decode(<clob_api_secret>), message)
        )
        ```

        保存响应中返回的 ID：

        ```json theme={null}
        {
          "heartbeat_id": "<heartbeat_id>"
        }
        ```
      </Step>

      <Step title="继续发送心跳">
        五秒后，将最新的 `heartbeat_id` 发回同一路由。每次成功响应都会返回一个新 ID，供下一次心跳使用：

        ```bash theme={null}
        curl -X POST "https://clob.polymarket.com/v1/heartbeats" \
          -H "Content-Type: application/json" \
          -H "POLY_ADDRESS: <signer_address>" \
          -H "POLY_API_KEY: <clob_api_key>" \
          -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
          -H "POLY_SIGNATURE: <clob_l2_signature>" \
          -H "POLY_TIMESTAMP: <clob_request_timestamp>" \
          --data '{"heartbeat_id":"<heartbeat_id>"}'
        ```

        为此请求创建新的 `<clob_request_timestamp>` 和 `<clob_l2_signature>`，并在上述签名计算中用包含最新 ID 的精确序列化请求体替换 `body`。

        如果 ID 无效或已过期，CLOB 会返回 `400 Bad Request` 并提供预期的 ID。使用该 ID 对新请求签名，然后重试：

        ```json theme={null}
        {
          "error_msg": "Invalid Heartbeat ID",
          "heartbeat_id": "<expected_heartbeat_id>"
        }
        ```
      </Step>
    </Steps>
  </Tab>

  <Tab title="TypeScript">内容即将推出。</Tab>
  <Tab title="Python">内容即将推出。</Tab>
</Tabs>

## 检查仅允许平仓模式

仅允许平仓模式是针对市场的账户级熔断机制。启用后，账户只能下达减少现有头寸的订单，不能下达新的开仓订单。如果希望快速失败而不是等待订单被拒绝，请在下单前检查此状态。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `fetchClosedOnlyMode()`：

    ```ts theme={null}
    const closedOnly = await client.fetchClosedOnlyMode();

    // closedOnly: boolean
    ```

    该方法以布尔值返回账户的仅允许平仓状态：

    <CodeGroup>
      ```json ClosedOnlyMode Example theme={null}
      false
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `get_closed_only_mode()`。同步 `SecureClient` 提供相同的方法。

    ```python theme={null}
    closed_only = await client.get_closed_only_mode()

    # closed_only: bool
    ```

    该方法以 `bool` 返回账户的仅允许平仓状态：

    <CodeGroup>
      ```json ClosedOnlyMode Example theme={null}
      false
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    读取账户的仅允许平仓状态：

    ```bash theme={null}
    curl "https://clob.polymarket.com/auth/ban-status/closed-only" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>"
    ```

    使用 [API 身份验证](/cn/getting-started/api#身份验证)中的签名者地址和 CLOB API 凭据，创建新的 `<clob_request_timestamp>`，并在没有请求体的情况下生成 `<clob_l2_signature>`：

    ```text theme={null}
    message = <clob_request_timestamp> + "GET" + "/auth/ban-status/closed-only"

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    <CodeGroup>
      ```ts ClosedOnlyMode Type theme={null}
      type ClosedOnlyMode = {
        closed_only: boolean;
      };
      ```

      ```json ClosedOnlyMode Example theme={null}
      {
        "closed_only": false
      }
      ```
    </CodeGroup>

    `closed_only: true` 表示账户只能减少或平掉现有头寸；新的开仓订单会被拒绝。
  </Tab>
</Tabs>

## 检查订单计分状态

当订单在提供[流动性奖励](/cn/programs/liquidity-rewards)的市场中处于有效状态，并满足该市场的最低合格规模、最大合格价差和所需有效时长时，订单会获得计分。由于资格会随订单簿变化，请将结果视为某个时间点的检查。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `fetchOrderScoring()`，并传入订单 ID：

    ```ts theme={null}
    const scoring = await client.fetchOrderScoring({ orderId: order.id });

    // scoring: boolean
    ```

    若要在一次请求中检查多个订单的计分状态，请调用 `fetchOrdersScoring()` 并传入这些订单的 ID：

    ```ts theme={null}
    const scoringByOrder = await client.fetchOrdersScoring({
      orderIds: ["ORDER_ID_1", "ORDER_ID_2"],
    });

    // scoringByOrder: OrdersScoringResponse
    ```

    响应将每个订单 ID 映射到其计分状态：

    <CodeGroup>
      ```ts OrdersScoringResponse Type theme={null}
      type OrdersScoringResponse = Record<string, boolean>;
      ```

      ```json OrdersScoringResponse Example theme={null}
      {
        "ORDER_ID_1": true,
        "ORDER_ID_2": false
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `get_order_scoring()`，并传入订单 ID。同步 `SecureClient` 提供相同的方法。

    ```python theme={null}
    scoring = await client.get_order_scoring(order_id=order.id)

    # scoring: bool
    ```

    若要在一次请求中检查多个订单的计分状态，请调用 `get_orders_scoring()` 并传入这些订单的 ID：

    ```python theme={null}
    scoring_by_order = await client.get_orders_scoring(
        order_ids=["ORDER_ID_1", "ORDER_ID_2"],
    )

    # scoring_by_order: dict[str, bool]
    ```

    响应将每个订单 ID 映射到其计分状态：

    <CodeGroup>
      ```python OrdersScoringResponse Type theme={null}
      dict[str, bool]
      ```

      ```json OrdersScoringResponse Example theme={null}
      {
        "ORDER_ID_1": true,
        "ORDER_ID_2": false
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    检查订单当前是否符合奖励资格：

    ```bash theme={null}
    curl "https://clob.polymarket.com/order-scoring?order_id=<order_id>" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>"
    ```

    使用 [API 身份验证](/cn/getting-started/api#身份验证)中的签名者地址和 CLOB API 凭据，创建新的 `<clob_request_timestamp>`，并在没有请求体的情况下生成 `<clob_l2_signature>`。`order_id` 查询参数不属于签名路径的一部分：

    ```text theme={null}
    message = <clob_request_timestamp> + "GET" + "/order-scoring"

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    响应包含订单当前的计分状态：

    ```json Example theme={null}
    {
      "scoring": true
    }
    ```

    完整的请求和响应架构请参阅[获取订单计分状态](/api-reference/trade/get-order-scoring-status)。
  </Tab>
</Tabs>
