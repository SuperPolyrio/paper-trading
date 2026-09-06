> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 钱包活动

> 追踪与 Polymarket 钱包关联的持仓和活动。

使用钱包地址了解账户当前的风险敞口，并追踪其活动
随时间的变化。

## 未平仓持仓

查看钱包当前的结果敞口和未实现收益表现。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listPositions()`。

    ```ts theme={null}
    const address = "0x8ba1f109551bD432803012645Ac136ddd64DBA72";

    const pages = client.listPositions({ user: address, pageSize: 1 });

    for await (const page of pages) {
      // page.items: Position[]
    }
    ```

    <Accordion title="输出：Position[]">
      <CodeGroup>
        ```ts Position Type theme={null}
        type Position = {
          conditionId: CtfConditionId;
          wallet?: Address | null;
          tokenId?: TokenId | null;
          size?: DecimalString | null;
          avgPrice?: DecimalString | null;
          initialValue?: DecimalString | null;
          currentValue?: DecimalString | null;
          cashPnl?: DecimalString | null;
          percentPnl?: number | null;
          totalBought?: DecimalString | null;
          realizedPnl?: DecimalString | null;
          percentRealizedPnl?: number | null;
          curPrice?: DecimalString | null;
          redeemable?: boolean | null;
          mergeable?: boolean | null;
          title?: string | null;
          slug?: string | null;
          icon?: string | null;
          eventId?: string | null;
          eventSlug?: string | null;
          outcome?: string | null;
          outcomeIndex?: number | null;
          oppositeOutcome?: string | null;
          oppositeTokenId?: TokenId | null;
          endDate?: IsoCalendarDateString | null;
          negativeRisk?: boolean | null;
        };
        ```

        ```json Position Example theme={null}
        [
          {
            "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "wallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "size": "125.5",
            "avgPrice": "0.42",
            "initialValue": "52.71",
            "currentValue": "61.50",
            "cashPnl": "8.79",
            "percentPnl": 16.68,
            "curPrice": "0.49",
            "title": "Will the US confirm that aliens exist before 2027?",
            "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "eventSlug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "outcome": "Yes",
            "outcomeIndex": 0,
            "redeemable": false,
            "mergeable": false
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_positions()`。
    同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    address = "0x8ba1f109551bD432803012645Ac136ddd64DBA72"

    pages = client.list_positions(user=address, page_size=1)

    async for page in pages:
        ...  # page.items: tuple[Position, ...]
    ```

    <Accordion title="输出：tuple[Position, ...]">
      <CodeGroup>
        ```python Position Type theme={null}
        class Position:
            condition_id: CtfConditionId
            wallet: EvmAddress | None
            token_id: TokenId | None
            size: Decimal | None
            avg_price: Decimal | None
            initial_value: Decimal | None
            current_value: Decimal | None
            cash_pnl: Decimal | None
            percent_pnl: float | None
            total_bought: Decimal | None
            realized_pnl: Decimal | None
            percent_realized_pnl: float | None
            cur_price: Decimal | None
            redeemable: bool | None
            mergeable: bool | None
            title: str | None
            slug: str | None
            icon: str | None
            event_id: str | None
            event_slug: str | None
            outcome: str | None
            outcome_index: int | None
            opposite_outcome: str | None
            opposite_token_id: TokenId | None
            end_date: date | None
            negative_risk: bool | None
        ```

        ```json Position Example theme={null}
        [
          {
            "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "wallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "size": "125.5",
            "avg_price": "0.42",
            "initial_value": "52.71",
            "current_value": "61.50",
            "cash_pnl": "8.79",
            "percent_pnl": 16.68,
            "cur_price": "0.49",
            "title": "Will the US confirm that aliens exist before 2027?",
            "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "event_slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "outcome": "Yes",
            "outcome_index": 0,
            "redeemable": false,
            "mergeable": false
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    列出钱包的未平仓持仓：

    ```bash theme={null}
    ADDRESS="0x8ba1f109551bD432803012645Ac136ddd64DBA72"

    curl "https://data-api.polymarket.com/positions?user=$ADDRESS&limit=1"
    ```

    响应包含钱包的未平仓持仓：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "proxyWallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
          "asset": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "size": 125.5,
          "avgPrice": 0.42,
          "initialValue": 52.71,
          "currentValue": 61.5,
          "cashPnl": 8.79,
          "percentPnl": 16.68,
          "curPrice": 0.49,
          "title": "Will the US confirm that aliens exist before 2027?",
          "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
          "eventSlug": "will-the-us-confirm-that-aliens-exist-before-2027",
          "outcome": "Yes",
          "outcomeIndex": 0
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

## 已平仓持仓

查看钱包已退出或已结算的持仓，包括
其已实现收益表现。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listClosedPositions()`。

    ```ts theme={null}
    const pages = client.listClosedPositions({
      user: address,
      pageSize: 1,
    });

    for await (const page of pages) {
      // page.items: ClosedPosition[]
    }
    ```

    <Accordion title="输出：ClosedPosition[]">
      <CodeGroup>
        ```ts ClosedPosition Type theme={null}
        type ClosedPosition = {
          wallet?: Address | null;
          tokenId?: TokenId | null;
          conditionId?: CtfConditionId | null;
          avgPrice?: DecimalString | null;
          totalBought?: DecimalString | null;
          realizedPnl?: DecimalString | null;
          curPrice?: DecimalString | null;
          timestamp?: EpochMilliseconds | null;
          title?: string | null;
          slug?: string | null;
          icon?: string | null;
          eventSlug?: string | null;
          outcome?: string | null;
          outcomeIndex?: number | null;
          oppositeOutcome?: string | null;
          oppositeTokenId?: TokenId | null;
          endDate?: MixedDateTimeString | null;
        };
        ```

        ```json ClosedPosition Example theme={null}
        [
          {
            "wallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            "tokenId": "7305630249804085635496399869905769372294302716159034447326228509068694952392",
            "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "avgPrice": "0.58",
            "totalBought": "250.00",
            "realizedPnl": "34.25",
            "curPrice": "0.00",
            "timestamp": 1782752879000,
            "title": "Will the US confirm that aliens exist before 2027?",
            "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "eventSlug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "outcome": "No",
            "outcomeIndex": 1
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_closed_positions()`。
    同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_closed_positions(user=address, page_size=1)

    async for page in pages:
        ...  # page.items: tuple[ClosedPosition, ...]
    ```

    <Accordion title="输出：tuple[ClosedPosition, ...]">
      <CodeGroup>
        ```python ClosedPosition Type theme={null}
        class ClosedPosition:
            wallet: EvmAddress | None
            token_id: TokenId | None
            condition_id: CtfConditionId | None
            avg_price: Decimal | None
            total_bought: Decimal | None
            realized_pnl: Decimal | None
            cur_price: Decimal | None
            timestamp: datetime | None
            title: str | None
            slug: str | None
            icon: str | None
            event_slug: str | None
            outcome: str | None
            outcome_index: int | None
            opposite_outcome: str | None
            opposite_token_id: TokenId | None
            end_date: date | None
        ```

        ```json ClosedPosition Example theme={null}
        [
          {
            "wallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            "token_id": "7305630249804085635496399869905769372294302716159034447326228509068694952392",
            "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "avg_price": "0.58",
            "total_bought": "250.00",
            "realized_pnl": "34.25",
            "cur_price": "0.00",
            "timestamp": "2026-06-29T17:07:59Z",
            "title": "Will the US confirm that aliens exist before 2027?",
            "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "event_slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "outcome": "No",
            "outcome_index": 1
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    列出钱包的已平仓持仓：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/closed-positions?user=$ADDRESS&limit=1"
    ```

    响应包含钱包的已平仓持仓：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "proxyWallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
          "asset": "7305630249804085635496399869905769372294302716159034447326228509068694952392",
          "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "avgPrice": 0.58,
          "totalBought": 250,
          "realizedPnl": 34.25,
          "curPrice": 0,
          "timestamp": 1782752879,
          "title": "Will the US confirm that aliens exist before 2027?",
          "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
          "eventSlug": "will-the-us-confirm-that-aliens-exist-before-2027",
          "outcome": "No",
          "outcomeIndex": 1
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

## 活动历史

追踪钱包在交易及其他活动中的时间线。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listActivity()`。

    ```ts theme={null}
    const pages = client.listActivity({ user: address, pageSize: 1 });

    for await (const page of pages) {
      // page.items: Activity[]
    }
    ```

    `Activity` 是可辨识联合类型。`type` 字段用于标识
    每个变体：

    <Accordion title="输出：Activity[]">
      <CodeGroup>
        ```ts Activity Union theme={null}
        type Activity =
          | TradeActivity
          | SplitActivity
          | MergeActivity
          | RedeemActivity
          | ConversionActivity
          | RewardActivity
          | MakerRebateActivity
          | ReferralRewardActivity
          | YieldActivity;
        ```

        ```json Activity Example theme={null}
        [
          {
            "type": "TRADE",
            "isCombo": false,
            "wallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            "timestamp": 1782752879000,
            "transactionHash": "0x4f3d2c1b0a9876543210fedcba9876543210fedcba9876543210fedcba987654",
            "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "side": "BUY",
            "shares": "25",
            "amount": "12.25",
            "price": "0.49",
            "title": "Will the US confirm that aliens exist before 2027?",
            "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "eventSlug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "outcome": "Yes",
            "outcomeIndex": 0
          },
          {
            "type": "REWARD",
            "wallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            "timestamp": 1782752979000,
            "transactionHash": "0x7a6b5c4d3e2f109876543210fedcba9876543210fedcba9876543210fedcba98",
            "amount": "5"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_activity()`。
    同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_activity(user=address, page_size=1)

    async for page in pages:
        ...  # page.items: tuple[Activity, ...]
    ```

    `Activity` 是由受支持的活动变体组成的联合类型：

    <Accordion title="输出：tuple[Activity, ...]">
      <CodeGroup>
        ```python Activity Union theme={null}
        Activity = (
            TradeActivity
            | ComboTradeActivity
            | SplitActivity
            | MergeActivity
            | RedeemActivity
            | ConversionActivity
            | RewardActivity
            | MakerRebateActivity
            | ReferralRewardActivity
            | YieldActivity
            | UnknownActivity
        )
        ```

        ```json Activity Example theme={null}
        [
          {
            "type": "TRADE",
            "is_combo": false,
            "wallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            "timestamp": "2026-06-29T17:07:59Z",
            "transaction_hash": "0x4f3d2c1b0a9876543210fedcba9876543210fedcba9876543210fedcba987654",
            "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
            "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
            "side": "BUY",
            "shares": "25",
            "amount": "12.25",
            "price": "0.49",
            "title": "Will the US confirm that aliens exist before 2027?",
            "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "event_slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "outcome": "Yes",
            "outcome_index": 0
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    列出钱包活动：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/activity?user=$ADDRESS&limit=1"
    ```

    响应包含钱包活动：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "proxyWallet": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
          "timestamp": 1782752879,
          "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "type": "TRADE",
          "size": 25,
          "usdcSize": 12.25,
          "transactionHash": "0x4f3d2c1b0a9876543210fedcba9876543210fedcba9876543210fedcba987654",
          "price": 0.49,
          "asset": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
          "side": "BUY",
          "title": "Will the US confirm that aliens exist before 2027?",
          "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
          "eventSlug": "will-the-us-confirm-that-aliens-exist-before-2027",
          "outcome": "Yes",
          "outcomeIndex": 0
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

## 通知

通知为已连接账户提供短期保留的未读事件记录。
CLOB 会将这些记录保留 48 小时。

### 列出通知

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `fetchNotifications()`：

    ```ts theme={null}
    const notifications = await client.fetchNotifications();

    // notifications: Notification[]
    ```

    <Accordion title="输出：Notification[]">
      <CodeGroup>
        ```ts Notification Type theme={null}
        type Notification = {
          id: NotificationId;
          owner: string;
          payload: unknown;
          timestamp: EpochMilliseconds;
          type: number;
        };
        ```

        ```json Notification Example theme={null}
        [
          {
            "id": 1,
            "owner": "f4f247b7-4ac7-ff29-a152-04fda0a8755a",
            "type": 2,
            "payload": {
              "order_id": "0x72c66a1f70c00ac5e5eb9ce0452b7d118bc4869f8b822a1a8d8580c16e3ca83e"
            },
            "timestamp": 1675277676000
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `get_notifications()`。同步
    `SecureClient` 也提供相同方法：

    ```python theme={null}
    notifications = await client.get_notifications()

    # notifications: tuple[Notification, ...]
    ```

    <Accordion title="输出：tuple[Notification, ...]">
      <CodeGroup>
        ```python Notification Type theme={null}
        class Notification:
            id: int
            owner: str
            type: int
            payload: Any
            timestamp: datetime
        ```

        ```json Notification Example theme={null}
        [
          {
            "id": 1,
            "owner": "f4f247b7-4ac7-ff29-a152-04fda0a8755a",
            "type": 2,
            "payload": {
              "order_id": "0x72c66a1f70c00ac5e5eb9ce0452b7d118bc4869f8b822a1a8d8580c16e3ca83e"
            },
            "timestamp": "2023-02-01T18:54:36Z"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    列出账户的未读通知：

    ```bash theme={null}
    curl -G "https://clob.polymarket.com/notifications" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      --data-urlencode "signature_type=<signature_type>"
    ```

    使用 [API
    身份验证](/cn/getting-started/api#身份验证)中的签名者地址和 CLOB API 凭据，
    创建新的 `<clob_request_timestamp>`，并为
    `GET /notifications` 生成 `<clob_l2_signature>`。`signature_type`
    查询参数不属于签名路径：

    ```text theme={null}
    message = <clob_request_timestamp> + "GET" + "/notifications"

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    根据已连接账户的[钱包
    类型](/cn/trading/wallets-auth#钱包类型)选择 `signature_type`：

    | 钱包      | `signature_type` |
    | ------- | ---------------: |
    | 存款钱包    |              `3` |
    | 代理钱包    |              `1` |
    | Safe 钱包 |              `2` |
    | EOA     |              `0` |

    响应包含未读通知：

    ```json theme={null}
    [
      {
        "id": 1,
        "owner": "f4f247b7-4ac7-ff29-a152-04fda0a8755a",
        "type": 2,
        "payload": {
          "order_id": "0x72c66a1f70c00ac5e5eb9ce0452b7d118bc4869f8b822a1a8d8580c16e3ca83e"
        },
        "timestamp": 1675277676
      }
    ]
    ```
  </Tab>
</Tabs>

### 丢弃通知

处理通知后将其标记为已读。列出未读通知时，已丢弃的通知
将不再出现。

<Tabs>
  <Tab title="TypeScript">
    使用通知 ID 在 `SecureClient` 上调用 `dropNotifications()`：

    ```ts theme={null}
    await client.dropNotifications({
      ids: notifications.map(({ id }) => String(id)),
    });
    ```
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `drop_notifications()`。同步
    `SecureClient` 也提供相同方法：

    ```python theme={null}
    await client.drop_notifications(
        ids=[notification.id for notification in notifications],
    )
    ```
  </Tab>

  <Tab title="API">
    将通知 ID 作为逗号分隔的列表发送：

    ```bash theme={null}
    curl -X DELETE -G "https://clob.polymarket.com/notifications" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>" \
      --data-urlencode "ids=1,2"
    ```

    使用 [API
    身份验证](/cn/getting-started/api#身份验证)中的签名者地址和 CLOB API 凭据，
    创建新的 `<clob_request_timestamp>`，并为
    `DELETE /notifications` 生成 `<clob_l2_signature>`。`ids`
    查询参数不属于签名路径：

    ```text theme={null}
    message = <clob_request_timestamp> + "DELETE" + "/notifications"

    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    请求成功时返回 `OK`。
  </Tab>
</Tabs>

## 投资组合价值

读取钱包当前的投资组合价值。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchPortfolioValue()`。

    ```ts theme={null}
    const value = await client.fetchPortfolioValue({ user: address });

    // value: Value[]
    ```

    <Accordion title="输出：Value[]">
      <CodeGroup>
        ```ts Value Type theme={null}
        type Value = {
          user?: Address | null;
          value?: DecimalString | null;
        };
        ```

        ```json Value Example theme={null}
        [
          {
            "user": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            "value": "1842.37"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_portfolio_values()`。
    同步 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    value = await client.get_portfolio_values(user=address)

    # value: tuple[PortfolioValue, ...]
    ```

    <Accordion title="输出：tuple[PortfolioValue, ...]">
      <CodeGroup>
        ```python PortfolioValue Type theme={null}
        class PortfolioValue:
            user: EvmAddress | None
            value: Decimal | None
        ```

        ```json PortfolioValue Example theme={null}
        [
          {
            "user": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
            "value": "1842.37"
          }
        ]
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    获取钱包的投资组合价值：

    ```bash theme={null}
    curl "https://data-api.polymarket.com/value?user=$ADDRESS"
    ```

    响应包含钱包当前的投资组合价值：

    <Accordion title="响应">
      ```json theme={null}
      [
        {
          "user": "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
          "value": 1842.37
        }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>
