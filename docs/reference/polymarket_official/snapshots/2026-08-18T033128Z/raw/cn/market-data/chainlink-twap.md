> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Chainlink TWAP 价格

> 直接通过 Chainlink 或 Polymarket RTDS 获取 Chainlink 计算的 TWAP 价格。

时间加权平均价格（TWAP）表示资产在一段回看窗口内的价格。本页介绍 Chainlink 计算的 30 秒和 60 秒 TWAP。

<Info>
  Chainlink Data Streams 主网 TWAP Feed 现已上线。你可以使用现有的标准或赞助
  Data Streams 凭证。Polymarket RTDS 计划于 2026 年 8 月 4 日上线。
</Info>

<Warning>
  Chainlink 仍在进行开发和稳定性测试，预计持续至 2026 年 8 月 4 日。Feed
  目前可用于集成，但在此之前仍可能更新。
</Warning>

## 使用 Chainlink Data Streams

如需直接访问主网数据、获取订阅前的最新报告或原始签名报告，请使用 Chainlink Data Streams。在 [Chainlink Data Streams 目录](https://data.chain.link/streams)中查找资产的 `TWAP: 30s` 或 `TWAP: 60s` 产品，然后复制其 Feed ID。请仅在可信后端运行，切勿向浏览器或移动应用暴露 Chainlink 凭证。

<Steps>
  <Step title="安装并连接">
    使用 Node.js 20+ 和 TypeScript 5.3+。安装 Chainlink SDK：

    ```bash theme={null}
    npm install --save-exact @chainlink/data-streams-sdk@1.2.1
    ```

    创建主网客户端：

    ```ts theme={null}
    import {
      createClient,
      decodeReport,
      type Report,
    } from "@chainlink/data-streams-sdk";

    type TwapFeed = {
      symbol: string;
      windowSeconds: 30 | 60;
    };

    function requireEnv(name: string): string {
      const value = process.env[name];

      if (!value) {
        throw new Error(`Missing required environment variable: ${name}`);
      }

      return value;
    }

    const client = createClient({
      apiKey: requireEnv("CHAINLINK_CLIENT_ID"),
      userSecret: requireEnv("CHAINLINK_CLIENT_SECRET"),
      endpoint: "https://api.dataengine.chain.link",
      wsEndpoint: "wss://ws.dataengine.chain.link",
      haMode: false,
    });
    ```

    SDK 会为请求签名。请确保服务器时间与 Chainlink 服务器时间相差不超过五秒。
  </Step>

  <Step title="映射并解码报告">
    将目录中的 Feed ID 存入服务端环境变量，然后把每个 ID
    映射到对应的资产和窗口。保留有符号 E18 价格，不要转换为 JavaScript
    `number`：

    ```ts theme={null}
    const feeds = new Map<string, TwapFeed>([
      [
        requireEnv("CHAINLINK_TWAP_30S_FEED_ID").toLowerCase(),
        { symbol: "btc/usd", windowSeconds: 30 },
      ],
      [
        requireEnv("CHAINLINK_TWAP_60S_FEED_ID").toLowerCase(),
        { symbol: "btc/usd", windowSeconds: 60 },
      ],
    ]);

    const E18 = 10n ** 18n;

    function formatE18(value: bigint): string {
      const sign = value < 0n ? "-" : "";
      const absolute = value < 0n ? -value : value;
      const whole = absolute / E18;
      const fraction = (absolute % E18)
        .toString()
        .padStart(18, "0")
        .replace(/0+$/, "");

      return `${sign}${whole}${fraction ? `.${fraction}` : ""}`;
    }

    function readTwap(report: Report) {
      const feed = feeds.get(report.feedID.toLowerCase());

      if (!feed) {
        throw new Error(`Unexpected Chainlink feed ID: ${report.feedID}`);
      }

      const decoded = decodeReport(report.fullReport, report.feedID);

      if (decoded.version !== "V2") {
        throw new Error(`Expected a V2 report, received ${decoded.version}`);
      }

      return {
        feedID: report.feedID,
        symbol: feed.symbol,
        windowSeconds: feed.windowSeconds,
        value: formatE18(decoded.price),
        observedAt: new Date(report.observationsTimestamp * 1_000).toISOString(),
      };
    }
    ```
  </Step>

  <Step title="获取最新 TWAP">
    在订阅前获取每个 Feed 的最新报告：

    ```ts theme={null}
    const latestReports = await Promise.all(
      [...feeds.keys()].map((feedID) => client.getLatestReport(feedID)),
    );

    for (const report of latestReports) {
      console.log(readTwap(report));
    }
    ```

    <Accordion title="输出：Chainlink TWAP">
      ```json theme={null}
      {
        "feedID": "0x<mainnet-feed-id>",
        "symbol": "btc/usd",
        "windowSeconds": 30,
        "value": "65000.5",
        "observedAt": "2026-07-27T19:00:00.000Z"
      }
      ```
    </Accordion>
  </Step>

  <Step title="订阅更新">
    订阅所选 Feed ID：

    ```ts theme={null}
    const stream = client.createStream([...feeds.keys()]);

    stream.on("report", (report) => {
      try {
        console.log(readTwap(report));
      } catch (error) {
        console.error("Failed to decode Chainlink report:", error);
      }
    });

    stream.on("disconnected", () => {
      console.warn("Chainlink stream disconnected");
    });

    stream.on("reconnecting", ({ attempt, delayMs }) => {
      console.warn(`Reconnecting: attempt ${attempt} in ${delayMs}ms`);
    });

    stream.on("error", (error) => {
      console.error("Chainlink stream error:", error);
    });

    await stream.connect();

    process.once("SIGINT", async () => {
      await stream.close();
      process.exit(0);
    });
    ```

    SDK 会自动验证身份并重连。请监控 `error`、`disconnected` 和
    `reconnecting`；如果重试耗尽，请重新启动数据流。
  </Step>
</Steps>

### 读取 Chainlink 报告

TWAP Feed 使用 Chainlink V2 报告架构。`decodeReport()` 会将编码后的
`benchmarkPrice` 暴露为 `decoded.price`。

| 值                              | 用法                                |
| ------------------------------ | --------------------------------- |
| `report.feedID`                | 将报告映射到对应资产以及 30 秒或 60 秒窗口         |
| `decoded.price`                | 将精确的有符号 E18 值保留为 `bigint` 或十进制字符串 |
| `report.validFromTimestamp`    | 读取报告开始生效的 Unix 秒时间戳               |
| `report.observationsTimestamp` | 读取 Chainlink 观测时间的 Unix 秒时间戳      |
| `decoded.expiresAt`            | 读取报告过期时间的 Unix 秒时间戳               |
| `report.fullReport`            | 访问 Chainlink 的原始签名报告              |

报告不包含交易对或窗口标签。请自行维护映射，切勿根据更新频率推断窗口，并使用 `observationsTimestamp` 检查新鲜度。

<Note>
  `decodeReport()` 只解析报告字段，不验证 DON
  签名。在将报告用于结算或其他对信任要求较高的操作前，请遵循 Chainlink
  的验证要求。
</Note>

### 查找 Feed ID

打开 [Chainlink Data Streams 目录](https://data.chain.link/streams)，搜索所需资产和窗口，例如 `BTC / USD - TWAP: 30s` 或 `BTC / USD - TWAP: 60s`。从产品页面复制 Feed ID，并配合现有的标准或赞助 Data Streams 凭证使用。

| REST URL                            | WebSocket URL                    |
| ----------------------------------- | -------------------------------- |
| `https://api.dataengine.chain.link` | `wss://ws.dataengine.chain.link` |

30 秒和 60 秒表示回看窗口，而不是发布频率。Chainlink 负责计算并签名 TWAP。请定义新鲜度阈值和报告中断时的回退行为，不要尝试根据传入更新推断计算方式。Chainlink 目前未公开此自定义数据流的采样边界、权重、舍入或输入缺失处理方式，因此请勿在没有 Chainlink 规范的情况下自行复现该值。

有关生产环境要求，请参阅 Chainlink 的 [TypeScript SDK
参考](https://docs.chain.link/data-streams/reference/data-streams-api/ts-sdk)、[身份验证参考](https://docs.chain.link/data-streams/reference/data-streams-api/authentication)和[开发者责任](https://docs.chain.link/data-streams/developer-responsibilities)。

## 使用 Polymarket RTDS

RTDS 是推荐的生产集成。从 2026 年 8 月 4 日起，它将无需凭证即可转发 Chainlink 计算的主网 TWAP 更新。

<Warning>
  RTDS TWAP 计划于 2026 年 8 月 4 日上线。启用前，订阅可能返回 `topic not found`
  且不会产生事件。现在即可安装和部署，但请在上线后创建或重新创建订阅；被拒绝的上线前订阅可能不会在现有连接上重试。使用下方
  SDK 版本时，RTDS 启用无需再次升级。
</Warning>

请使用小写、斜杠分隔的交易对，例如 `btc/usd`。省略符号筛选条件即可接收所有可用交易对。

<Tabs>
  <Tab title="TypeScript">
    需要 Node.js 24+ 和 `@polymarket/client` 0.3.0 或更高版本：

    ```bash theme={null}
    npm install @polymarket/client
    ```

    匹配的 Polymarket bindings 会自动安装。常规设置请参阅 [TypeScript SDK
    指南](/cn/getting-started/typescript)。

    使用明确的窗口和所需交易对进行订阅：

    ```ts theme={null}
    import { createPublicClient } from "@polymarket/client";

    const client = createPublicClient();

    const stream = await client.subscribe([
      {
        topic: "prices.crypto.chainlink.twap",
        windowSeconds: 30,
        symbols: ["btc/usd"],
      },
    ]);

    try {
      for await (const event of stream) {
        console.log({
          symbol: event.payload.symbol,
          value: event.payload.value,
          windowSeconds: event.payload.windowSeconds,
          observedAt: new Date(event.payload.timestamp).toISOString(),
        });
      }
    } finally {
      await stream.close();
    }
    ```

    将 `windowSeconds` 设置为 `30` 或 `60`。省略 `symbols`
    即可接收所有可用交易对。该订阅也适用于 `SecureClient`。

    <Accordion title="RTDS 启用后的示例输出">
      <CodeGroup>
        ```ts CryptoPricesChainlinkTwapEvent Union theme={null}
        type CryptoPricesChainlinkTwapThirtyEvent = {
          topic: "prices.crypto.chainlink.twap";
          type: "update";
          timestamp: EpochMilliseconds;
          payload: {
            symbol: string;
            timestamp: EpochMilliseconds;
            value: DecimalString;
            windowSeconds: 30;
          };
        };

        type CryptoPricesChainlinkTwapSixtyEvent = {
          topic: "prices.crypto.chainlink.twap";
          type: "update";
          timestamp: EpochMilliseconds;
          payload: {
            symbol: string;
            timestamp: EpochMilliseconds;
            value: DecimalString;
            windowSeconds: 60;
          };
        };

        type CryptoPricesChainlinkTwapEvent =
          | CryptoPricesChainlinkTwapThirtyEvent
          | CryptoPricesChainlinkTwapSixtyEvent;
        ```

        ```json CryptoPricesChainlinkTwapEvent Example theme={null}
        {
          "topic": "prices.crypto.chainlink.twap",
          "type": "update",
          "timestamp": 1785178800123,
          "payload": {
            "symbol": "btc/usd",
            "timestamp": 1785178800000,
            "value": "65000.5",
            "windowSeconds": 30
          }
        }
        ```
      </CodeGroup>
    </Accordion>

    `payload.value` 是根据 Chainlink 定点值转换得到的精确十进制字符串。请将其保留为十进制字符串，不要转换为 JavaScript `number`。使用 `payload.timestamp` 作为 Chainlink 观测时间；外层 `timestamp` 是发布方将更新提交到 RTDS 的时间。

    RTDS 接受订阅后，SDK 会在断开时恢复订阅，但不会在连接保持打开时重试上线前被拒绝的订阅。
  </Tab>

  <Tab title="Python">
    需要 Python 3.11+ 和 `polymarket-client` 0.3.0 或更高版本：

    ```bash theme={null}
    python -m pip install --upgrade polymarket-client
    ```

    常规设置请参阅 [Python SDK 指南](/cn/getting-started/python)。

    使用 `CryptoPricesChainlinkTwapSpec` 订阅：

    ```python theme={null}
    import asyncio

    from polymarket import AsyncPublicClient
    from polymarket.streams import CryptoPricesChainlinkTwapSpec


    async def main() -> None:
        async with AsyncPublicClient() as client:
            async with await client.subscribe(
                CryptoPricesChainlinkTwapSpec(
                    window_seconds=30,
                    symbols=["btc/usd"],
                )
            ) as stream:
                async for event in stream:
                    print(
                        event.payload.symbol,
                        event.payload.value,
                        event.payload.window_seconds,
                        event.payload.timestamp,
                    )


    asyncio.run(main())
    ```

    将 `window_seconds` 设置为 `30` 或 `60`。省略 `symbols`
    即可接收所有可用交易对。该订阅也适用于 `AsyncSecureClient`。同步客户端不支持实时订阅。

    <Accordion title="RTDS 启用后的示例输出">
      <CodeGroup>
        ```python CryptoPricesChainlinkTwapEvent Type theme={null}
        class CryptoPricesChainlinkTwapPayload:
            symbol: str
            timestamp: int
            value: Decimal
            window_seconds: Literal[30, 60]

        class CryptoPricesChainlinkTwapEvent:
            topic: Literal["prices.crypto.chainlink.twap"]
            type: Literal["update"]
            timestamp: datetime | None
            payload: CryptoPricesChainlinkTwapPayload
        ```

        ```json CryptoPricesChainlinkTwapEvent Example theme={null}
        {
          "topic": "prices.crypto.chainlink.twap",
          "type": "update",
          "timestamp": "2026-07-27T19:00:00.123000Z",
          "payload": {
            "symbol": "btc/usd",
            "timestamp": 1785178800000,
            "value": "65000.5",
            "window_seconds": 30
          }
        }
        ```
      </CodeGroup>
    </Accordion>

    `payload.value` 是根据 Chainlink 定点值转换得到的精确 `Decimal`。使用 `payload.timestamp` 作为 Chainlink 观测时间；外层 `timestamp` 是发布方将更新提交到 RTDS 的时间。

    RTDS 接受订阅后，SDK 会在断开时恢复订阅，但不会在连接保持打开时重试上线前被拒绝的订阅。
  </Tab>

  <Tab title="API">
    如需更底层的控制，可直接连接 RTDS：

    ```text theme={null}
    wss://ws-live-data.polymarket.com
    ```

    <Note>RTDS 使用应用层心跳。每 5 秒发送一次文本帧 `PING` 以维持连接。</Note>

    为所需的回看窗口发送订阅帧：

    ```json theme={null}
    {
      "action": "subscribe",
      "subscriptions": [
        {
          "topic": "crypto_prices_twap_thirty",
          "type": "update",
          "filters": "{\"symbol\":\"btc/usd\"}"
        },
        {
          "topic": "crypto_prices_twap_sixty",
          "type": "update",
          "filters": "{\"symbol\":\"btc/usd\"}"
        }
      ]
    }
    ```

    | 回看窗口 | RTDS 主题                     |
    | ---- | --------------------------- |
    | 30 秒 | `crypto_prices_twap_thirty` |
    | 60 秒 | `crypto_prices_twap_sixty`  |

    `filters` 必须使用上例所示的精确紧凑 JSON 格式，包含一个小写符号且不能有空格，例如 `{"symbol":"btc/usd"}`。省略该字段即可接收所有可用符号。如果同一窗口需要多个符号，请省略 `filters`，并在应用中按 `payload.symbol` 筛选更新。

    <Accordion title="RTDS 启用后的示例输出">
      ```json theme={null}
      {
        "topic": "crypto_prices_twap_thirty",
        "type": "update",
        "timestamp": 1785178800123,
        "payload": {
          "symbol": "btc/usd",
          "value": 65000.5,
          "full_accuracy_value": "65000500000000000000000",
          "timestamp": 1785178800000,
          "window_s": 30
        }
      }
      ```
    </Accordion>

    `full_accuracy_value` 是精确的有符号 E18 定点值。请使用整数或十进制运算将其除以 10<sup>18</sup>。数值 `value` 仅用于便捷显示。

    使用 `payload.timestamp` 作为 Chainlink 观测时间；外层 `timestamp` 是发布方将更新提交到 RTDS 的时间。直接连接的客户端必须在断开连接或收到上线前的 `topic not found` 响应后重新连接并重新订阅。
  </Tab>
</Tabs>

### 选择窗口

每次订阅选择 30 秒或 60 秒窗口。如需两个值，请订阅两次。

### 数据流行为

订阅从下一次更新开始，不提供快照、历史数据或断开后的重放。
