> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# TypeScript SDK

> 开始使用统一的 Polymarket TypeScript SDK。

`@polymarket/client` NPM 软件包是构建 Polymarket 集成的官方 TypeScript SDK。
通过该 SDK 客户端，你可以获取市场数据、订阅实时数据、提交订单、赎回持仓，并执行许多其他操作。

<CardGroup cols={2}>
  <Card title="GitHub" icon="github" href="https://github.com/Polymarket/ts-sdk">
    查看 TypeScript SDK 源代码。
  </Card>

  <Card title="NPM 软件包" icon="npm" href="https://www.npmjs.com/package/@polymarket/client">
    安装 `@polymarket/client@latest`。
  </Card>
</CardGroup>

有关近期 TypeScript 版本，请参阅 [SDK 更新日志](/changelog/sdks#typescript)。

## 快速入门

<Steps>
  <Step title="安装 SDK">
    使用你偏好的软件包管理器安装 SDK。

    <CodeGroup>
      ```bash npm theme={null}
      npm install @polymarket/client@latest
      ```

      ```bash bun theme={null}
      bun add @polymarket/client@latest
      ```

      ```bash pnpm theme={null}
      pnpm add @polymarket/client@latest
      ```

      ```bash yarn theme={null}
      yarn add @polymarket/client@latest
      ```
    </CodeGroup>
  </Step>

  <Step title="创建公共客户端">
    创建 `PublicClient` 以访问公开的 Polymarket 数据。

    ```ts theme={null}
    import { createPublicClient } from "@polymarket/client";

    const client = createPublicClient();
    ```
  </Step>

  <Step title="获取活跃市场">
    获取一页活跃市场，发现交易机会。

    ```ts theme={null}
    const pages = client.listMarkets({
      closed: false,
      pageSize: 5,
    });

    const firstPage = await pages.firstPage();

    for (const market of firstPage.items) {
      // market: Market
    }
    ```
  </Step>
</Steps>

## 分页

SDK 的列表方法使用统一的分页器接口。使用 `for await` 获取所有页面；如果需要控制请求节奏，也可以逐页获取。

```ts theme={null}
const pages = client.listMarkets({
  closed: false,
  pageSize: 10,
});

for await (const page of pages) {
  for (const market of page.items) {
    // market: Market
  }
}
```

`page.nextCursor` 是不透明的 SDK 游标。如果希望稍后继续扫描，请原样保存；从第一页开始时则省略它。

```ts theme={null}
const pages = client.listMarkets({
  closed: false,
  pageSize: 10,
});

const page = await pages.firstPage();

if (page.nextCursor) {
  const secondPage = await pages.from(page.nextCursor).firstPage();
  // secondPage.items: Market[]
}
```

## 类型

SDK 方法返回带类型的响应，其公共类型由 `@polymarket/client` 导出。

```ts theme={null}
import type {
  Event,
  Market,
  OrderBook,
  PriceHistoryPoint,
} from "@polymarket/client";
```

SDK 还为标识符、十进制值、时间戳和 EVM 地址使用品牌类型，使 TypeScript 能够区分原本都表示为字符串的值。

```ts theme={null}
import type {
  CtfConditionId,
  DecimalString,
  EvmAddress,
  IsoDateTimeString,
  MarketId,
  TokenId,
} from "@polymarket/client";
```

## 错误处理

每项 SDK 操作都有对应的错误守卫。使用它处理该操作已记录的错误，并重新抛出任何意外错误。

```ts theme={null}
import { ListMarketsError } from "@polymarket/client";

try {
  const page = await client.listMarkets({ closed: false }).firstPage();
  // page.items: Market[]
} catch (error) {
  if (!ListMarketsError.isError(error)) {
    throw error;
  }

  switch (error.name) {
    case "RateLimitError":
      // Retry later.
      break;
    case "UserInputError":
      // Fix the request parameters.
      break;
  }
}
```

## 钱包集成

当集成需要交易或访问账户数据时，请创建 `SecureClient`。将所用钱包库的签名器适配器传给 `createSecureClient()`。

<Tabs>
  <Tab title="Viem">
    同时安装 Viem 适配器和 SDK。

    <CodeGroup>
      ```bash npm theme={null}
      npm install @polymarket/client@latest viem
      ```

      ```bash bun theme={null}
      bun add @polymarket/client@latest viem
      ```

      ```bash pnpm theme={null}
      pnpm add @polymarket/client@latest viem
      ```

      ```bash yarn theme={null}
      yarn add @polymarket/client@latest viem
      ```
    </CodeGroup>

    直接使用私钥创建签名器：

    ```ts theme={null}
    import { createSecureClient } from "@polymarket/client";
    import { privateKey } from "@polymarket/client/viem";

    const client = await createSecureClient({
      signer: privateKey(process.env.POLYMARKET_PRIVATE_KEY),
    });
    ```

    如果应用已有 Viem `WalletClient` 实例，请使用 `signerFrom()` 进行适配：

    ```ts theme={null}
    import { createSecureClient } from "@polymarket/client";
    import { signerFrom } from "@polymarket/client/viem";

    const client = await createSecureClient({
      signer: signerFrom(walletClient),
    });
    ```
  </Tab>

  <Tab title="Privy">
    同时安装 Privy 适配器、服务端 SDK 和 Polymarket SDK。

    <CodeGroup>
      ```bash npm theme={null}
      npm install @polymarket/client@latest @privy-io/node
      ```

      ```bash bun theme={null}
      bun add @polymarket/client@latest @privy-io/node
      ```

      ```bash pnpm theme={null}
      pnpm add @polymarket/client@latest @privy-io/node
      ```

      ```bash yarn theme={null}
      yarn add @polymarket/client@latest @privy-io/node
      ```
    </CodeGroup>

    使用 Privy 客户端和钱包 ID 创建签名器：

    ```ts theme={null}
    import { createSecureClient } from "@polymarket/client";
    import { signerFrom } from "@polymarket/client/privy";
    import { PrivyClient } from "@privy-io/node";

    const privy = new PrivyClient({
      appId: process.env.PRIVY_APP_ID!,
      appSecret: process.env.PRIVY_APP_SECRET!,
    });

    const client = await createSecureClient({
      signer: signerFrom({
        privy,
        walletId: process.env.PRIVY_WALLET_ID!,
      }),
    });
    ```
  </Tab>

  <Tab title="Ethers v5">
    同时安装 Ethers v5 适配器、其别名依赖项和 SDK。

    <CodeGroup>
      ```bash npm theme={null}
      npm install @polymarket/client@latest ethers-v5@npm:ethers@^5.8.0
      ```

      ```bash bun theme={null}
      bun add @polymarket/client@latest ethers-v5@npm:ethers@^5.8.0
      ```

      ```bash pnpm theme={null}
      pnpm add @polymarket/client@latest ethers-v5@npm:ethers@^5.8.0
      ```

      ```bash yarn theme={null}
      yarn add @polymarket/client@latest ethers-v5@npm:ethers@^5.8.0
      ```
    </CodeGroup>

    使用 Ethers v5 钱包创建签名器：

    ```ts theme={null}
    import { createSecureClient } from "@polymarket/client";
    import { signerFrom } from "@polymarket/client/ethers-v5";
    import { ethers } from "ethers-v5";

    const provider = new ethers.providers.JsonRpcProvider(
      process.env.POLYGON_RPC_URL,
    );
    const wallet = new ethers.Wallet(process.env.POLYMARKET_PRIVATE_KEY!, provider);

    const client = await createSecureClient({
      signer: signerFrom(wallet),
    });
    ```
  </Tab>
</Tabs>

接下来，请参阅[钱包与身份验证](/cn/trading/wallets-auth)，配置账户钱包和无 Gas 交易。

## 实时订阅

SDK 可以把多个实时数据源的更新合并为单一事件流。

使用一个或多个订阅规范调用 `subscribe()`。公共数据流可通过 `PublicClient` 和 `SecureClient` 使用；`SecureClient` 还提供已连接账户的私有数据流。

<CodeGroup>
  ```ts Public Client theme={null}
  const tokenId = "<token_id>";

  const stream = await client.subscribe([
    { topic: "market", tokenIds: [tokenId] },
    { topic: "sports" },
  ]);

  for await (const event of stream) {
    // event:
    //   | MarketBookEvent
    //   | MarketPriceChangeEvent
    //   | MarketLastTradePriceEvent
    //   | MarketTickSizeChangeEvent
    //   | SportsEvent

    if (shouldClose) {
      await stream.close();
      break;
    }
  }
  ```

  ```ts Secure Client theme={null}
  const tokenId = "<token_id>";

  const stream = await client.subscribe([
    { topic: "market", tokenIds: [tokenId] },
    { topic: "user" },
  ]);

  for await (const event of stream) {
    // event:
    //   | MarketBookEvent
    //   | MarketPriceChangeEvent
    //   | MarketLastTradePriceEvent
    //   | MarketTickSizeChangeEvent
    //   | UserEvent

    if (shouldClose) {
      await stream.close();
      break;
    }
  }
  ```
</CodeGroup>

在处理特定事件结构之前，请先通过 `event.topic` 缩小类型范围。示例仅展示了部分数据流；有关各数据源的选项，请参阅[实时数据](/cn/market-data/realtime-data)、[实时订单更新](/cn/trading/realtime-order-updates)和 [Perps 实时更新](/perps/realtime-updates)。

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
