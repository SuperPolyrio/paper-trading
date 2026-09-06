> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 市场详情

> 了解 Polymarket 市场的结构和状态。

市场对象代表一个预测市场。除了问题和结果外，它还描述市场的当前状态，并提供集成读取价格、跟踪活动或交易所需的信息。

以下示例假设你已经有一个市场对象。如需查找或获取市场，请参阅[发现市场](/cn/market-data/discover-markets)。

<Tabs>
  <Tab title="TypeScript">
    假设你已在 `PublicClient` 或 `SecureClient` 上调用 `fetchMarket()` 获取市场：

    ```ts theme={null}
    const market = await client.fetchMarket({
      slug: "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
    });
    // market: Market
    ```
  </Tab>

  <Tab title="Python">
    假设你已在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_market()` 获取市场：

    ```python theme={null}
    market = await client.get_market(
        slug="will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
    )
    # market: Market
    ```
  </Tab>

  <Tab title="API">
    假设你已从 Gamma API 获取市场：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/markets/slug/will-the-us-confirm-that-aliens-exist-before-2027-789-924-249"
    ```
  </Tab>
</Tabs>

## 市场标识符

一个市场有三个标识符，各自在不同的场景中使用：

| 标识符     | 用途                        |
| ------- | ------------------------- |
| 市场 ID   | 通过 Gamma ID 获取已知市场。       |
| 市场 slug | 获取已知市场，或链接到其便于阅读的页面。      |
| 条件 ID   | 用于公共分析、持仓，以及部分交易或生命周期工作流。 |

<Tabs>
  <Tab title="TypeScript">
    从市场对象中读取标识符：

    ```ts theme={null}
    const marketId = market.id;
    const slug = market.slug;
    const conditionId = market.conditionId;
    ```
  </Tab>

  <Tab title="Python">
    从市场对象中读取标识符：

    ```python theme={null}
    market_id = market.id
    slug = market.slug
    condition_id = market.condition_id
    ```
  </Tab>

  <Tab title="API">
    从 Gamma 响应中读取标识符：

    ```json theme={null}
    {
      "id": "703257",
      "slug": "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
      "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75"
    }
    ```
  </Tab>
</Tabs>

## 市场结果

每个市场都有 YES 和 NO 两个结果。每个结果都包含标签、当前价格和 CLOB 代币 ID。代币 ID 用于将市场与订单簿及交易请求关联起来。

<Tabs>
  <Tab title="TypeScript">
    从 `market.outcomes` 中读取两个结果：

    ```ts theme={null}
    const yes = market.outcomes.yes;
    const no = market.outcomes.no;
    ```

    SDK 使用标签、价格和 CLOB 代币 ID 表示每个结果：

    ```ts theme={null}
    type MarketOutcome = {
      label: string;
      tokenId: TokenId | null;
      price: DecimalString | null;
    };

    type MarketOutcomes = {
      yes: MarketOutcome;
      no: MarketOutcome;
    };
    ```
  </Tab>

  <Tab title="Python">
    从 `market.outcomes` 中读取两个结果：

    ```python theme={null}
    yes = market.outcomes.yes
    no = market.outcomes.no
    ```

    SDK 使用标签、价格和 CLOB 代币 ID 表示每个结果：

    ```python theme={null}
    class MarketOutcome:
        label: str
        token_id: TokenId | None
        price: Decimal | None

    class MarketOutcomes:
        yes: MarketOutcome
        no: MarketOutcome
    ```
  </Tab>

  <Tab title="API">
    Gamma 以 JSON 编码数组的形式返回结果标签、价格和 CLOB 代币 ID：

    ```json theme={null}
    {
      "outcomes": "[\"Yes\", \"No\"]",
      "outcomePrices": "[\"0.085\", \"0.915\"]",
      "clobTokenIds": "[\"107505882767731489358349912513945399560393482969656700824895970500493757150417\", \"7305630249804085635496399869905769372294302716159034447326228509068694952392\"]"
    }
    ```

    解析各个数组，并按索引关联其中的值：

    ```js theme={null}
    const labels = JSON.parse(market.outcomes);
    const prices = JSON.parse(market.outcomePrices);
    const tokenIds = JSON.parse(market.clobTokenIds);

    const outcomes = labels.map((label, index) => ({
      label,
      price: prices[index],
      tokenId: tokenIds[index],
    }));
    ```

    索引 `0` 表示 YES 结果，索引 `1` 表示 NO 结果。
  </Tab>
</Tabs>

## 市场状态

市场状态表示市场当前是否可交易。在依赖实时价格或提交订单之前，请先检查市场状态，因为市场可能在订单簿开放前就已存在，并在关闭后仍可被发现。它还表示市场是否属于[负风险组](/cn/concepts/negative-risk)；在这种组中，多个互斥市场中只有一个可以结算为 YES。

<Tabs>
  <Tab title="TypeScript">
    从 `market.state` 中读取市场状态：

    ```ts theme={null}
    const state = market.state;
    // state: MarketState
    ```

    `MarketState` 汇总市场的运行状态和时间信息：

    <CodeGroup>
      ```ts MarketState Type theme={null}
      type MarketState = {
        active?: boolean | null;
        closed?: boolean | null;
        archived?: boolean | null;
        acceptingOrders?: boolean | null;
        enableOrderBook?: boolean | null;
        negRisk?: boolean | null;
        startDate?: IsoDateTimeString | null;
        endDate?: IsoDateTimeString | null;
        closedTime?: IsoDateTimeString | null;
      };
      ```

      ```json MarketState Example theme={null}
      {
        "active": true,
        "closed": false,
        "archived": false,
        "acceptingOrders": true,
        "enableOrderBook": true,
        "negRisk": false,
        "startDate": "2024-03-15T00:00:00Z",
        "endDate": "2027-01-01T00:00:00Z",
        "closedTime": null
      }
      ```
    </CodeGroup>

    使用其中的状态字段检查市场是否可以交易：

    ```ts theme={null}
    const isTradeReady = state.active && !state.closed && state.acceptingOrders;
    ```
  </Tab>

  <Tab title="Python">
    从 `market.state` 中读取市场状态：

    ```python theme={null}
    state = market.state
    # state: MarketState
    ```

    `MarketState` 汇总市场的运行状态和时间信息：

    <CodeGroup>
      ```python MarketState Type theme={null}
      class MarketState:
          active: bool | None
          closed: bool | None
          archived: bool | None
          accepting_orders: bool | None
          enable_order_book: bool | None
          neg_risk: bool | None
          start_date: datetime | None
          end_date: datetime | None
          closed_time: datetime | None
      ```

      ```json MarketState Example theme={null}
      {
        "active": true,
        "closed": false,
        "archived": false,
        "accepting_orders": true,
        "enable_order_book": true,
        "neg_risk": false,
        "start_date": "2024-03-15T00:00:00Z",
        "end_date": "2027-01-01T00:00:00Z",
        "closed_time": null
      }
      ```
    </CodeGroup>

    使用其中的状态字段检查市场是否可以交易：

    ```python theme={null}
    is_trade_ready = (
        state.active
        and not state.closed
        and state.accepting_orders
    )
    ```
  </Tab>

  <Tab title="API">
    从 Gamma 响应中读取相应字段：

    ```json theme={null}
    {
      "active": true,
      "closed": false,
      "acceptingOrders": true,
      "negRisk": false,
      "restricted": false,
      "archived": false
    }
    ```

    说明：

    | 字段                | 类型      | 说明                                      |
    | ----------------- | ------- | --------------------------------------- |
    | `active`          | boolean | 市场已部署且未归档。                              |
    | `closed`          | boolean | 市场已结算或关闭，因此无法继续交易。                      |
    | `acceptingOrders` | boolean | 订单簿已开放，可接受新的限价单和市价单。                    |
    | `negRisk`         | boolean | 市场属于[负风险组](/cn/concepts/negative-risk)。 |
    | `restricted`      | boolean | 市场在部分司法管辖区受到地域限制。                       |
    | `archived`        | boolean | 市场已归档且为只读：不能交易，也不会更新结算状态。               |
  </Tab>
</Tabs>

### 识别增强型负风险

负风险成员关系是市场级属性，但增强型负风险在事件上配置。确认市场的 `negRisk` 值为 `true` 后，获取其事件，并检查是否已启用增强型负风险：

<Tabs>
  <Tab title="TypeScript">
    获取市场引用的事件，然后检查其交易配置：

    ```ts theme={null}
    const event = await client.fetchEvent({
      id: market.events[0].id,
    });

    const isAugmentedNegativeRisk =
      event.trading.enableNegRisk === true &&
      event.trading.negRiskAugmented === true;
    ```
  </Tab>

  <Tab title="Python">
    获取市场引用的事件，然后检查其交易配置：

    ```python theme={null}
    event = await client.get_event(id=market.events[0].id)

    is_augmented_negative_risk = (
        event.trading.enable_neg_risk is True
        and event.trading.neg_risk_augmented is True
    )
    ```
  </Tab>

  <Tab title="API">
    从市场的 `events` 数组中读取事件 ID，然后获取该事件：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/events/$EVENT_ID"
    ```

    当两个字段均为 `true` 时，该事件使用增强型负风险：

    ```json theme={null}
    {
      "enableNegRisk": true,
      "negRiskAugmented": true
    }
    ```
  </Tab>
</Tabs>

有关命名结果、占位符和 Other 的行为，请参阅[增强型负风险](/cn/concepts/negative-risk#增强型负风险)。

## 交易约束

每个市场都会强制执行最小价格增量（也称为 **tick size**）和最小订单规模。最小价格增量定义了可提交价格的网格；订单价格必须是当前有效值的整数倍。小于最小订单规模的订单会被拒绝。

| 最小价格增量   | 步长    | 示例价格                         |
| -------- | ----- | ---------------------------- |
| `0.1`    | 10¢   | `0.1`, `0.5`, `0.9`          |
| `0.01`   | 1¢    | `0.01`, `0.50`, `0.99`       |
| `0.005`  | 0.5¢  | `0.005`, `0.500`, `0.995`    |
| `0.0025` | 0.25¢ | `0.0025`, `0.5000`, `0.9975` |
| `0.001`  | 0.1¢  | `0.001`, `0.500`, `0.999`    |
| `0.0001` | 0.01¢ | `0.0001`, `0.5000`, `0.9999` |

<Note>
  `0.0025`（0.25¢）的增量仅适用于世界杯的*晋级*、*独赢*、*让分*和*大小分*市场。请始终从市场中读取当前有效值，不要假设增量固定不变。
</Note>

在集成中读取市场的当前约束：

<Tabs>
  <Tab title="TypeScript">
    读取市场的交易约束：

    ```ts theme={null}
    const minimumTickSize = market.trading.minimumTickSize;
    const minimumOrderSize = market.trading.minimumOrderSize;

    // minimumTickSize: TickSizeValue | null
    // minimumOrderSize: DecimalString | null
    //   minimum USDC notional; orders smaller than this are rejected
    ```

    SDK 将这些约束汇总在 `MarketTrading` 中：

    <CodeGroup>
      ```ts MarketTrading Type theme={null}
      type MarketTrading = {
        minimumTickSize?: TickSizeValue | null;
        minimumOrderSize?: DecimalString | null;
      };
      ```

      ```json MarketTrading Example theme={null}
      {
        "minimumTickSize": 0.01,
        "minimumOrderSize": "5"
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    读取市场的交易约束：

    ```python theme={null}
    minimum_tick_size = market.trading.minimum_tick_size
    minimum_order_size = market.trading.minimum_order_size

    # minimum_tick_size: Decimal | None
    # minimum_order_size: Decimal | None
    #   minimum USDC notional per order
    ```

    SDK 将这些约束汇总在 `MarketTrading` 中：

    <CodeGroup>
      ```python MarketTrading Type theme={null}
      class MarketTrading:
          minimum_tick_size: Decimal | None
          minimum_order_size: Decimal | None
      ```

      ```json MarketTrading Example theme={null}
      {
        "minimum_tick_size": 0.01,
        "minimum_order_size": "5"
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    从 Gamma 响应中读取相应字段：

    ```json theme={null}
    {
      "orderPriceMinTickSize": 0.01,
      "orderMinSize": 5
    }
    ```

    说明：

    | 字段                      | 类型     | 说明                                |
    | ----------------------- | ------ | --------------------------------- |
    | `orderPriceMinTickSize` | number | 限价单的最小价格增量。价格必须是该值的整数倍。           |
    | `orderMinSize`          | number | 以 USDC 计的最小订单规模。CLOB 会拒绝低于此阈值的订单。 |
  </Tab>
</Tabs>

## 交易费用

部分市场会收取交易费用。费用计划决定费用如何随价格变化、由哪一方支付，以及返还给做市商的比例。

<Tabs>
  <Tab title="TypeScript">
    读取市场的费用配置：

    ```ts theme={null}
    const feesEnabled = market.trading.feesEnabled;
    const feeSchedule = market.trading.feeSchedule;

    // feesEnabled: boolean | null
    // feeSchedule: {
    //   rate: DecimalString;  base rate used in the fee calculation
    //   exponent: number;     exponent applied to the price component
    //   takerOnly: boolean;   fees charged to taker side only
    //   rebateRate: DecimalString; maker rebate as fraction of taker fee
    // } | null
    ```

    SDK 通过 `MarketTrading` 和 `FeeSchedule` 提供此配置：

    <CodeGroup>
      ```ts MarketTrading Type theme={null}
      type FeeSchedule = {
        rate: DecimalString;
        exponent: number;
        takerOnly: boolean;
        rebateRate: DecimalString;
      };

      type MarketTrading = {
        feesEnabled?: boolean | null;
        feeSchedule?: FeeSchedule | null;
      };
      ```

      ```json MarketTrading Example theme={null}
      {
        "feesEnabled": true,
        "feeSchedule": {
          "rate": "0.04",
          "exponent": 1,
          "takerOnly": true,
          "rebateRate": "0.25"
        }
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    读取市场的费用配置：

    ```python theme={null}
    fees_enabled = market.trading.fees_enabled
    fee_schedule = market.trading.fee_schedule

    # fees_enabled: bool | None
    # fee_schedule.rate: Decimal            base rate used in the fee calculation
    # fee_schedule.exponent: int | float    exponent applied to the price component
    # fee_schedule.taker_only: bool         taker-only flag
    # fee_schedule.rebate_rate: Decimal     maker rebate fraction
    ```

    SDK 通过 `MarketTrading` 和 `FeeSchedule` 提供此配置：

    <CodeGroup>
      ```python MarketTrading Type theme={null}
      class FeeSchedule:
          rate: Decimal
          exponent: int | float
          taker_only: bool
          rebate_rate: Decimal

      class MarketTrading:
          fees_enabled: bool | None
          fee_schedule: FeeSchedule | None
      ```

      ```json MarketTrading Example theme={null}
      {
        "fees_enabled": true,
        "fee_schedule": {
          "rate": 0.04,
          "exponent": 1,
          "taker_only": true,
          "rebate_rate": 0.25
        }
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    从 Gamma 响应中读取相应字段：

    ```json theme={null}
    {
      "feesEnabled": true,
      "feeSchedule": {
        "rate": 0.04,
        "exponent": 1,
        "takerOnly": true,
        "rebateRate": 0.25
      }
    }
    ```

    说明：

    | 字段                       | 类型      | 说明                                  |
    | ------------------------ | ------- | ----------------------------------- |
    | `feesEnabled`            | boolean | 此市场是否已启用费用。                         |
    | `feeSchedule.rate`       | number  | 费用计算使用的基础费率。                        |
    | `feeSchedule.exponent`   | number  | 应用于费用曲线价格部分的指数。                     |
    | `feeSchedule.takerOnly`  | boolean | 为 `true` 时，仅向吃单方收取费用，做市商无需付费。       |
    | `feeSchedule.rebateRate` | number  | 向挂单做市商返还的吃单费用比例。例如：`0.25` 表示返还 25%。 |
  </Tab>
</Tabs>

## 流动性奖励设置

流动性奖励设置决定哪些挂单有资格获得激励，以及可用的资金金额。订单必须达到市场规定的最低合格规模，并保持在最大合格价差范围内。评分方法请参阅[流动性奖励](/cn/programs/liquidity-rewards)。

<Tabs>
  <Tab title="TypeScript">
    从 `market.rewards` 中读取流动性奖励设置：

    ```ts theme={null}
    const minimumSize = market.rewards.rewardsMinSize;
    const maximumSpread = market.rewards.rewardsMaxSpread;
    const allocations = market.rewards.clobRewards;

    // minimumSize: DecimalString | null
    // maximumSpread: number | null
    // allocations: ClobRewards[] | null
    ```

    SDK 通过 `MarketRewards` 提供市场配置，并通过 `ClobRewards` 提供每个带日期的奖励分配：

    <CodeGroup>
      ```ts ClobRewards Type theme={null}
      type MarketRewards = {
        clobRewards?: ClobRewards[] | null;
        rewardsMinSize?: DecimalString | null;
        rewardsMaxSpread?: number | null;
        holdingRewardsEnabled?: boolean | null;
      };

      type ClobRewards = {
        id: ClobRewardId;
        conditionId: CtfConditionId;
        assetAddress: string;
        rewardsAmount: DecimalString;
        rewardsDailyRate: DecimalString;
        startDate: IsoCalendarDateString;
        endDate: IsoCalendarDateString | null;
      };
      ```

      ```json ClobRewards Example theme={null}
      {
        "rewardsMinSize": "100",
        "rewardsMaxSpread": 3,
        "clobRewards": [
          {
            "id": "<reward_id>",
            "conditionId": "<condition_id>",
            "assetAddress": "<asset_address>",
            "rewardsAmount": "10000",
            "rewardsDailyRate": "100",
            "startDate": "2026-07-01",
            "endDate": "2026-07-31"
          }
        ]
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    从 `market.rewards` 中读取流动性奖励设置：

    ```python theme={null}
    minimum_size = market.rewards.rewards_min_size
    maximum_spread = market.rewards.rewards_max_spread
    allocations = market.rewards.clob_rewards

    # minimum_size: Decimal | None
    # maximum_spread: float | None
    # allocations: tuple[ClobReward, ...] | None
    ```

    SDK 通过 `MarketRewards` 提供市场配置，并通过 `ClobReward` 提供每个带日期的奖励分配：

    <CodeGroup>
      ```python ClobReward Type theme={null}
      class MarketRewards:
          clob_rewards: tuple[ClobReward, ...] | None
          rewards_min_size: Decimal | None
          rewards_max_spread: float | None
          holding_rewards_enabled: bool | None

      class ClobReward:
          id: ClobRewardId
          condition_id: CtfConditionId
          asset_address: str
          rewards_amount: Decimal
          rewards_daily_rate: Decimal
          start_date: date
          end_date: date | None
      ```

      ```json ClobReward Example theme={null}
      {
        "rewards_min_size": 100,
        "rewards_max_spread": 3,
        "clob_rewards": [
          {
            "id": "<reward_id>",
            "condition_id": "<condition_id>",
            "asset_address": "<asset_address>",
            "rewards_amount": 10000,
            "rewards_daily_rate": 100,
            "start_date": "2026-07-01",
            "end_date": "2026-07-31"
          }
        ]
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    从 Gamma 响应中读取相应字段：

    ```json theme={null}
    {
      "rewardsMinSize": 100,
      "rewardsMaxSpread": 3,
      "clobRewards": [
        {
          "id": "<reward_id>",
          "conditionId": "<condition_id>",
          "assetAddress": "<asset_address>",
          "rewardsAmount": 10000,
          "rewardsDailyRate": 100,
          "startDate": "2026-07-01",
          "endDate": "2026-07-31"
        }
      ]
    }
    ```

    说明：

    | 字段                               | 类型     | 说明                     |
    | -------------------------------- | ------ | ---------------------- |
    | `rewardsMinSize`                 | number | 有资格获得奖励的最小订单规模，以份额计。   |
    | `rewardsMaxSpread`               | number | 距离中间价的最大合格距离，以美分计。     |
    | `clobRewards`                    | array  | 为市场配置的带日期奖励分配。         |
    | `clobRewards[].rewardsAmount`    | number | 为该分配配置的奖励总额。           |
    | `clobRewards[].rewardsDailyRate` | number | 分配期间每天可用的奖励金额。         |
    | `clobRewards[].startDate`        | string | 分配开始日期。                |
    | `clobRewards[].endDate`          | string | 分配结束日期；无结束日期时为 `null`。 |
  </Tab>
</Tabs>

## 市场时间

市场时间信息用于确定市场在预期日程中的位置。市场包含开始和结束日期，体育市场还可能包含比赛的预定开始时间。

<Tabs>
  <Tab title="TypeScript">
    从市场状态、交易配置和体育元数据中读取时间信息：

    ```ts theme={null}
    const startDate = market.state.startDate;
    const endDate = market.state.endDate;
    const secondsDelay = market.trading.secondsDelay;
    const gameStartTime = market.sports.gameStartTime;

    // startDate: IsoDateTimeString | null
    // endDate: IsoDateTimeString | null
    // secondsDelay: number | null
    // gameStartTime: IsoDateTimeString | null
    ```

    SDK 按用途对这些值进行分组：

    <CodeGroup>
      ```ts MarketSportsMetadata Type theme={null}
      type MarketState = {
        startDate?: IsoDateTimeString | null;
        endDate?: IsoDateTimeString | null;
      };

      type MarketTrading = {
        secondsDelay?: number | null;
      };

      type MarketSportsMetadata = {
        gameStartTime?: IsoDateTimeString | null;
      };
      ```

      ```json MarketSportsMetadata Example theme={null}
      {
        "state": {
          "startDate": "2024-03-15T00:00:00Z",
          "endDate": "2027-01-01T00:00:00Z"
        },
        "trading": {
          "secondsDelay": 0
        },
        "sports": {
          "gameStartTime": null
        }
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    从市场状态、交易配置和体育元数据中读取时间信息：

    ```python theme={null}
    start_date = market.state.start_date
    end_date = market.state.end_date
    seconds_delay = market.trading.seconds_delay
    game_start_time = market.sports.game_start_time

    # start_date: datetime | None
    # end_date: datetime | None
    # seconds_delay: int | None
    # game_start_time: datetime | None
    ```

    SDK 按用途对这些值进行分组：

    <CodeGroup>
      ```python MarketSportsMetadata Type theme={null}
      class MarketState:
          start_date: datetime | None
          end_date: datetime | None

      class MarketTrading:
          seconds_delay: int | None

      class MarketSportsMetadata:
          game_start_time: datetime | None
      ```

      ```json MarketSportsMetadata Example theme={null}
      {
        "state": {
          "start_date": "2024-03-15T00:00:00Z",
          "end_date": "2027-01-01T00:00:00Z"
        },
        "trading": {
          "seconds_delay": 0
        },
        "sports": {
          "game_start_time": null
        }
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    从 Gamma 响应中读取相应字段：

    ```json theme={null}
    {
      "startDateIso": "2024-03-15T00:00:00Z",
      "endDateIso": "2027-01-01T00:00:00Z",
      "gameStartTime": null,
      "secondsDelay": 0
    }
    ```

    说明：

    | 字段              | 类型      | 说明                  |
    | --------------- | ------- | ------------------- |
    | `startDateIso`  | string  | ISO 8601 格式的市场开始日期。 |
    | `endDateIso`    | string  | ISO 8601 格式的市场结束日期。 |
    | `gameStartTime` | string  | 体育市场对应比赛的预定开始时间。    |
    | `secondsDelay`  | integer | 新提交的可成交订单在撮合前的延迟秒数。 |
  </Tab>
</Tabs>

## 流动性与活动

流动性、成交量和近期价格活动反映市场的交易活跃程度。可使用这些指标比较市场，或监控随时间发生的变化。

<Tabs>
  <Tab title="TypeScript">
    读取市场的流动性、成交量和价格摘要：

    ```ts theme={null}
    const liquidity = market.metrics.liquidity;
    const volume24hr = market.metrics.volume24hr;
    const lastTradePrice = market.prices.lastTradePrice;
    const bestBid = market.prices.bestBid;
    const bestAsk = market.prices.bestAsk;
    const spread = market.prices.spread;
    const oneDayPriceChange = market.prices.oneDayPriceChange;

    // liquidity: DecimalString | null
    // volume24hr: DecimalString | null
    // lastTradePrice: DecimalString | null
    // bestBid: DecimalString | null
    // bestAsk: DecimalString | null
    // spread: DecimalString | null
    // oneDayPriceChange: DecimalString | null
    ```

    SDK 将流动性和成交量汇总在 `MarketMetrics` 中，并将价格活动汇总在 `MarketPrices` 中：

    <CodeGroup>
      ```ts MarketPrices Type theme={null}
      type MarketMetrics = {
        liquidity?: DecimalString | null;
        volume24hr?: DecimalString | null;
      };

      type MarketPrices = {
        bestBid?: DecimalString | null;
        bestAsk?: DecimalString | null;
        lastTradePrice?: DecimalString | null;
        spread?: DecimalString | null;
        oneDayPriceChange?: DecimalString | null;
      };
      ```

      ```json MarketPrices Example theme={null}
      {
        "metrics": {
          "liquidity": "150248.32",
          "volume24hr": "5842.3"
        },
        "prices": {
          "lastTradePrice": "0.085",
          "bestBid": "0.08",
          "bestAsk": "0.09",
          "spread": "0.01",
          "oneDayPriceChange": "-0.003"
        }
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="Python">
    读取市场的流动性、成交量和价格摘要：

    ```python theme={null}
    liquidity = market.metrics.liquidity
    volume_24hr = market.metrics.volume_24hr
    last_trade_price = market.prices.last_trade_price
    best_bid = market.prices.best_bid
    best_ask = market.prices.best_ask
    spread = market.prices.spread
    one_day_price_change = market.prices.one_day_price_change
    ```

    SDK 将流动性和成交量汇总在 `MarketMetrics` 中，并将价格活动汇总在 `MarketPrices` 中：

    <CodeGroup>
      ```python MarketPrices Type theme={null}
      class MarketMetrics:
          liquidity: Decimal | None
          volume_24hr: Decimal | None

      class MarketPrices:
          best_bid: Decimal | None
          best_ask: Decimal | None
          last_trade_price: Decimal | None
          spread: Decimal | None
          one_day_price_change: Decimal | None
      ```

      ```json MarketPrices Example theme={null}
      {
        "metrics": {
          "liquidity": "150248.32",
          "volume_24hr": "5842.3"
        },
        "prices": {
          "last_trade_price": "0.085",
          "best_bid": "0.08",
          "best_ask": "0.09",
          "spread": "0.01",
          "one_day_price_change": "-0.003"
        }
      }
      ```
    </CodeGroup>
  </Tab>

  <Tab title="API">
    从 Gamma 响应中读取相应字段：

    ```json theme={null}
    {
      "liquidity": "150248.32",
      "volume24hr": 5842.3,
      "lastTradePrice": 0.085,
      "bestBid": 0.08,
      "bestAsk": 0.09,
      "spread": 0.01,
      "oneDayPriceChange": -0.003
    }
    ```

    说明：

    | 字段                  | 类型     | 说明                    |
    | ------------------- | ------ | --------------------- |
    | `liquidity`         | string | 报告的市场流动性。             |
    | `volume24hr`        | number | 过去 24 小时的交易量。         |
    | `lastTradePrice`    | number | 报告的市场最近成交价。           |
    | `bestBid`           | number | 报告的最高挂单买价。            |
    | `bestAsk`           | number | 报告的最低挂单卖价。            |
    | `spread`            | number | 最优卖价与最优买价之差。          |
    | `oneDayPriceChange` | number | 报告的市场价格在过去 24 小时内的变化。 |
  </Tab>
</Tabs>
