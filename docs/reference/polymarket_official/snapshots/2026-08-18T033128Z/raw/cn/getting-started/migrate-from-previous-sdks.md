> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# SDK 迁移

> 将现有的 Polymarket 集成迁移到统一的 SDK。

从旧版 CLOB、Relayer 和 Builder 签名客户端迁移到统一 SDK。

<Tabs>
  <Tab title="TypeScript">
    下面的示例将之前的客户端功能映射到 `@polymarket/client`。该 SDK 还涵盖了 Gamma API 和 Data API。直接迁移 Gamma 和 Data 调用可以简化应用程序代码，并提供与 SDK 的交易和工作流无缝配合的标准化、类型化的模型。

    ## 安装统一 SDK

    移除集成中使用的旧版 CLOB、Relayer 和 Builder 签名包。然后安装统一 SDK 和你的钱包库。

    <CodeGroup>
      ```bash npm theme={null}
      npm uninstall @polymarket/clob-client-v2 @polymarket/builder-relayer-client @polymarket/builder-signing-sdk
      npm install @polymarket/client@latest viem
      ```

      ```bash pnpm theme={null}
      pnpm remove @polymarket/clob-client-v2 @polymarket/builder-relayer-client @polymarket/builder-signing-sdk
      pnpm add @polymarket/client@latest viem
      ```

      ```bash Yarn theme={null}
      yarn remove @polymarket/clob-client-v2 @polymarket/builder-relayer-client @polymarket/builder-signing-sdk
      yarn add @polymarket/client@latest viem
      ```

      ```bash Bun theme={null}
      bun remove @polymarket/clob-client-v2 @polymarket/builder-relayer-client @polymarket/builder-signing-sdk
      bun add @polymarket/client@latest viem
      ```
    </CodeGroup>

    <Info>
      统一 SDK 包含了针对 Viem、Privy 和 Ethers v5
      的签名器适配器，因此你可以连接自己的钱包库而无需实现 CLOB
      特定的签名逻辑。请参阅 [钱包集成](/cn/getting-started/typescript#钱包集成)
      获取设置示例。
    </Info>

    ## 客户端和钱包设置

    ### 创建公共客户端

    `PublicClient` 提供无需身份验证的市场发现和数据读取功能。
    `createPublicClient` 默认使用生产环境，且不需要主机或链 ID。

    ```ts Before theme={null}
    import { ClobClient } from "@polymarket/clob-client-v2";

    const client = new ClobClient({
      host: "https://clob.polymarket.com",
      chain: 137,
    });
    ```

    ```ts After theme={null}
    import { createPublicClient } from "@polymarket/client";

    const client = createPublicClient();
    ```

    ### 创建经过身份验证的客户端

    `SecureClient` 增加了经过身份验证的交易、账户和钱包操作。
    `createSecureClient` 派生或创建 CLOB 凭据，解析账户钱包，并配置正确的签名流程。

    ```ts Before theme={null}
    import { ClobClient, SignatureTypeV2 } from "@polymarket/clob-client-v2";

    const tempClient = new ClobClient({
      host: "https://clob.polymarket.com",
      chain: 137,
      signer,
    });
    const credentials = await tempClient.createOrDeriveApiKey();

    const client = new ClobClient({
      host: "https://clob.polymarket.com",
      chain: 137,
      signer,
      creds: credentials,
      signatureType: SignatureTypeV2.POLY_1271,
      funderAddress: process.env.POLYMARKET_WALLET_ADDRESS,
    });
    ```

    ```ts After theme={null}
    import { type ApiKeyCreds, createSecureClient } from "@polymarket/client";
    import { privateKey } from "@polymarket/client/viem";

    const client = await createSecureClient({
      wallet: process.env.POLYMARKET_WALLET_ADDRESS,
      signer: privateKey(process.env.POLYMARKET_PRIVATE_KEY),
    });
    ```

    省略 `wallet` 以使用默认的 Deposit Wallet 流程。

    ### 使用现有凭据恢复

    从已认证的客户端读取凭据，安全存储它们，并将其传递给新客户端以使用相同的 API 密钥恢复。签名者和钱包必须标识拥有这些凭据的账户。

    ```ts Before theme={null}
    import { ClobClient, SignatureTypeV2 } from "@polymarket/clob-client-v2";

    const credentials = client.creds;
    if (!credentials) throw new Error("Client has no credentials");

    // Store the credentials securely.

    const resumedClient = new ClobClient({
      host: "https://clob.polymarket.com",
      chain: 137,
      signer,
      creds: credentials,
      signatureType: SignatureTypeV2.POLY_1271,
      funderAddress: process.env.POLYMARKET_WALLET_ADDRESS,
    });
    ```

    ```ts After theme={null}
    import { createSecureClient } from "@polymarket/client";
    import { privateKey } from "@polymarket/client/viem";

    const credentials = client.credentials;

    // Store the credentials securely.

    const resumedClient = await createSecureClient({
      wallet: process.env.POLYMARKET_WALLET_ADDRESS,
      signer: privateKey(process.env.POLYMARKET_PRIVATE_KEY),
      // Restore the type if loading from storage erased it.
      credentials: credentials as ApiKeyCreds,
    });
    ```

    ### 撤销当前 API 密钥

    结束认证将撤销当前的 API 密钥，使 `SecureClient` 失效，并返回一个 `PublicClient`。

    ```ts Before theme={null}
    await client.deleteApiKey();
    ```

    ```ts After theme={null}
    const publicClient = await client.endAuthentication();
    ```

    ### 配置 Builder API 密钥

    `SecureClient` 接受用于无 Gas 钱包操作的 Builder 授权。
    将本地或远程的 Builder 签名迁移到相应的辅助函数。

    **本地 Builder API 密钥**

    在服务器上保留本地 Builder 凭据。Node.js 入口点会生成所需的 HMAC 请求头。

    ```ts Before theme={null}
    import { RelayClient } from "@polymarket/builder-relayer-client";
    import { BuilderConfig } from "@polymarket/builder-signing-sdk";

    const builderConfig = new BuilderConfig({
      localBuilderCreds: {
        key: process.env.POLYMARKET_BUILDER_API_KEY!,
        secret: process.env.POLYMARKET_BUILDER_SECRET!,
        passphrase: process.env.POLYMARKET_BUILDER_PASSPHRASE!,
      },
    });

    const relayer = new RelayClient(
      "https://relayer-v2.polymarket.com",
      137,
      walletClient,
      builderConfig,
    );
    ```

    ```ts After theme={null}
    import { createSecureClient } from "@polymarket/client";
    import { builderApiKey } from "@polymarket/client/node";
    import { privateKey } from "@polymarket/client/viem";

    const client = await createSecureClient({
      signer: privateKey(process.env.POLYMARKET_PRIVATE_KEY),
      apiKey: builderApiKey({
        key: process.env.POLYMARKET_BUILDER_API_KEY!,
        secret: process.env.POLYMARKET_BUILDER_SECRET!,
        passphrase: process.env.POLYMARKET_BUILDER_PASSPHRASE!,
      }),
    });
    ```

    **远程 Builder 签名**

    将 Builder 凭据保留在签名端点之后。客户端发送相同的 `{ method, path, body }` 请求并应用相同的 Builder 请求头，因此端点契约无需更改。

    ```ts Before theme={null}
    import { RelayClient } from "@polymarket/builder-relayer-client";
    import { BuilderConfig } from "@polymarket/builder-signing-sdk";

    const builderConfig = new BuilderConfig({
      remoteBuilderConfig: {
        url: "https://example.com/api/builder/sign",
        token: process.env.BUILDER_SIGNING_TOKEN,
      },
    });

    const relayer = new RelayClient(
      "https://relayer-v2.polymarket.com",
      137,
      walletClient,
      builderConfig,
    );
    ```

    ```ts After theme={null}
    import { createSecureClient, remoteBuilderSigning } from "@polymarket/client";

    const client = await createSecureClient({
      signer,
      apiKey: remoteBuilderSigning({
        url: "https://example.com/api/builder/sign",
        headers: {
          Authorization: `Bearer ${process.env.BUILDER_SIGNING_TOKEN}`,
        },
      }),
    });
    ```

    在签名服务器上，将 `BuilderConfig.generateBuilderHeaders` 替换为
    `buildHmacSignature`。保留你现有的调用者认证和
    针对此处理程序的授权。

    ```ts Before theme={null}
    import { BuilderConfig } from "@polymarket/builder-signing-sdk";

    const builderConfig = new BuilderConfig({
      localBuilderCreds: {
        key: process.env.POLYMARKET_BUILDER_API_KEY!,
        secret: process.env.POLYMARKET_BUILDER_SECRET!,
        passphrase: process.env.POLYMARKET_BUILDER_PASSPHRASE!,
      },
    });

    export async function POST(request: Request): Promise<Response> {
      const { body, method, path } = await request.json();
      const headers = await builderConfig.generateBuilderHeaders(
        method,
        path,
        body,
      );

      return Response.json(headers);
    }
    ```

    ```ts After theme={null}
    import { buildHmacSignature } from "@polymarket/client";

    export async function POST(request: Request): Promise<Response> {
      const { body, method, path } = await request.json();
      const timestamp = Math.floor(Date.now() / 1000);

      return Response.json({
        POLY_BUILDER_API_KEY: process.env.POLYMARKET_BUILDER_API_KEY!,
        POLY_BUILDER_PASSPHRASE: process.env.POLYMARKET_BUILDER_PASSPHRASE!,
        POLY_BUILDER_SIGNATURE: await buildHmacSignature(
          process.env.POLYMARKET_BUILDER_SECRET!,
          timestamp,
          method,
          path,
          body,
        ),
        POLY_BUILDER_TIMESTAMP: `${timestamp}`,
      });
    }
    ```

    ### 配置 Relayer API 密钥

    `SecureClient` 直接配置 Relayer API 密钥。在重新连接现有账户时，将密钥及其关联地址传递给 `relayerApiKey`。

    ```ts theme={null}
    import { createSecureClient, relayerApiKey } from "@polymarket/client";
    import { privateKey } from "@polymarket/client/viem";

    const client = await createSecureClient({
      wallet: process.env.POLYMARKET_WALLET_ADDRESS,
      signer: privateKey(process.env.POLYMARKET_PRIVATE_KEY),
      apiKey: relayerApiKey({
        key: process.env.POLYMARKET_RELAYER_API_KEY!,
        address: process.env.POLYMARKET_RELAYER_API_KEY_ADDRESS!,
      }),
    });
    ```

    ### 部署 Deposit Wallet

    在不提供 `wallet` 的情况下创建 `SecureClient` 会派生签名者的 Deposit Wallet 地址，并在需要时进行部署。使用上述 Builder 授权策略之一来授权部署。

    ```ts Before theme={null}
    const deployment = await relayer.deployDepositWallet();
    await deployment.wait();
    ```

    ```ts After theme={null}
    import { createSecureClient } from "@polymarket/client";
    import { builderApiKey } from "@polymarket/client/node";
    import { privateKey } from "@polymarket/client/viem";

    const client = await createSecureClient({
      signer: privateKey(process.env.POLYMARKET_PRIVATE_KEY),
      apiKey: builderApiKey({
        key: process.env.POLYMARKET_BUILDER_API_KEY!,
        secret: process.env.POLYMARKET_BUILDER_SECRET!,
        passphrase: process.env.POLYMARKET_BUILDER_PASSPHRASE!,
      }),
    });

    const depositWalletAddress = client.account.wallet;
    ```

    ## 市场发现

    ### 获取市场

    `PublicClient` 和 `SecureClient` 均提供 `fetchMarket`，用于通过 Gamma 市场 ID、slug 或 Polymarket URL 进行读取。

    ```ts Before theme={null}
    const market = await client.getMarket("CONDITION_ID");
    ```

    ```ts After theme={null}
    const market = await client.fetchMarket({ slug: "MARKET_SLUG" });
    ```

    如果你只有条件 ID，请筛选 `listMarkets` 并获取其第一页。

    ```ts theme={null}
    const page = await client
      .listMarkets({ conditionIds: ["CONDITION_ID"] })
      .firstPage();

    const market = page.items[0];
    ```

    ### 列出市场

    `PublicClient` 和 `SecureClient` 均提供 `listMarkets`，它返回一个分页器并直接接受筛选条件。

    ```ts Before theme={null}
    const page = await client.getMarkets();
    const markets = page.data;
    ```

    ```ts After theme={null}
    const pages = client.listMarkets({ closed: false, pageSize: 20 });
    const page = await pages.firstPage();
    const markets = page.items;
    ```

    `getSimplifiedMarkets` 不需要单独的替代方案。统一的市场模型已针对 SDK 使用进行了规范化。采样市场读取应迁移至 `listCurrentRewards`，后者列出当前的奖励配置。

    ```ts Before theme={null}
    const markets = await client.getSamplingMarkets();
    const page = await client.getSamplingSimplifiedMarkets();
    ```

    ```ts After theme={null}
    const pages = client.listCurrentRewards();
    const page = await pages.firstPage();
    ```

    ## 市场数据

    ### 读取交易参数

    `PublicClient` 和 `SecureClient` 均返回统一市场模型中的 tick size 和负风险状态。下单方法也会自动解析这些值，因此你在下单时无需再传递这两个值。

    ```ts Before theme={null}
    const tickSize = await client.getTickSize("TOKEN_ID");
    const negRisk = await client.getNegRisk("TOKEN_ID");
    ```

    ```ts After theme={null}
    const page = await client
      .listMarkets({ clobTokenIds: ["TOKEN_ID"] })
      .firstPage();

    const market = page.items[0];
    const tickSize = market?.trading.minimumTickSize;
    const negRisk = market?.state.negRisk;
    ```

    ### 读取 CLOB 市场详情

    `PublicClient` 和 `SecureClient` 均返回用于公开产品数据的标准化市场模型。其交易字段取代了紧凑的 CLOB 市场响应和独立的费用读取。

    ```ts Before theme={null}
    const info = await client.getClobMarketInfo("CONDITION_ID");
    const feeRateBps = await client.getFeeRateBps("TOKEN_ID");
    const feeExponent = await client.getFeeExponent("TOKEN_ID");
    ```

    ```ts After theme={null}
    const page = await client
      .listMarkets({ conditionIds: ["CONDITION_ID"] })
      .firstPage();

    const market = page.items[0];
    const feeSchedule = market?.trading.feeSchedule;
    ```

    ### 获取订单簿

    `PublicClient` 和 `SecureClient` 提供带有 camelCase 请求字段的单个和批量订单簿读取功能。

    ```ts Before theme={null}
    const book = await client.getOrderBook("TOKEN_ID");
    const books = await client.getOrderBooks([
      { token_id: "TOKEN_ID_1" },
      { token_id: "TOKEN_ID_2" },
    ]);
    ```

    ```ts After theme={null}
    const book = await client.fetchOrderBook({ tokenId: "TOKEN_ID" });
    const books = await client.fetchOrderBooks([
      { tokenId: "TOKEN_ID_1" },
      { tokenId: "TOKEN_ID_2" },
    ]);
    ```

    ### 获取价格

    `PublicClient` 和 `SecureClient` 提供带有 `OrderSide` 和 camelCase 请求字段的单个和批量价格读取功能。

    ```ts Before theme={null}
    import { Side } from "@polymarket/clob-client-v2";

    const price = await client.getPrice("TOKEN_ID", Side.BUY);
    const prices = await client.getPrices([
      { token_id: "TOKEN_ID_1", side: Side.BUY },
      { token_id: "TOKEN_ID_2", side: Side.SELL },
    ]);
    ```

    ```ts After theme={null}
    import { OrderSide } from "@polymarket/client";

    const price = await client.fetchPrice({
      tokenId: "TOKEN_ID",
      side: OrderSide.BUY,
    });
    const prices = await client.fetchPrices([
      { tokenId: "TOKEN_ID_1", side: OrderSide.BUY },
      { tokenId: "TOKEN_ID_2", side: OrderSide.SELL },
    ]);
    ```

    ### 获取中间价

    `PublicClient` 和 `SecureClient` 提供用于单个和批量读取的 `fetchMidpoint` 和 `fetchMidpoints`。

    ```ts Before theme={null}
    const midpoint = await client.getMidpoint("TOKEN_ID");
    const midpoints = await client.getMidpoints([
      { token_id: "TOKEN_ID_1" },
      { token_id: "TOKEN_ID_2" },
    ]);
    ```

    ```ts After theme={null}
    const midpoint = await client.fetchMidpoint({ tokenId: "TOKEN_ID" });
    const midpoints = await client.fetchMidpoints([
      { tokenId: "TOKEN_ID_1" },
      { tokenId: "TOKEN_ID_2" },
    ]);
    ```

    ### 获取价差

    `PublicClient` 和 `SecureClient` 提供具有相同请求模式的单个和批量价差读取功能。

    ```ts Before theme={null}
    const spread = await client.getSpread("TOKEN_ID");
    const spreads = await client.getSpreads([
      { token_id: "TOKEN_ID_1" },
      { token_id: "TOKEN_ID_2" },
    ]);
    ```

    ```ts After theme={null}
    const spread = await client.fetchSpread({ tokenId: "TOKEN_ID" });
    const spreads = await client.fetchSpreads([
      { tokenId: "TOKEN_ID_1" },
      { tokenId: "TOKEN_ID_2" },
    ]);
    ```

    ### 获取最新成交价

    `PublicClient` 和 `SecureClient` 均提供用于获取最新成交价的单条和批量方法。

    ```ts Before theme={null}
    const lastTrade = await client.getLastTradePrice("TOKEN_ID");
    const lastTrades = await client.getLastTradesPrices([
      { token_id: "TOKEN_ID_1" },
      { token_id: "TOKEN_ID_2" },
    ]);
    ```

    ```ts After theme={null}
    const lastTrade = await client.fetchLastTradePrice({ tokenId: "TOKEN_ID" });
    const lastTrades = await client.fetchLastTradePrices([
      { tokenId: "TOKEN_ID_1" },
      { tokenId: "TOKEN_ID_2" },
    ]);
    ```

    ### 获取价格历史

    `PublicClient` 和 `SecureClient` 均提供价格历史功能。将所有参数传入一个请求对象；响应使用 camelCase 字段和类型化的时间戳。

    ```ts Before theme={null}
    const history = await client.getPricesHistory({
      market: "TOKEN_ID",
      interval: "1d",
      fidelity: 60,
    });
    ```

    ```ts After theme={null}
    import { PriceHistoryInterval } from "@polymarket/client";

    const history = await client.fetchPriceHistory({
      tokenId: "TOKEN_ID",
      interval: PriceHistoryInterval.ONE_DAY,
      fidelity: 60,
    });
    ```

    ### 估算市价单价格

    `PublicClient` 和 `SecureClient` 均提供市价单价格估算。请求中需区分用于买入的抵押品支出与用于卖出的份额出售。

    ```ts Before theme={null}
    const price = await client.calculateMarketPrice(
      "TOKEN_ID",
      Side.BUY,
      100,
      OrderType.FAK,
    );
    ```

    ```ts After theme={null}
    const price = await client.estimateMarketPrice({
      tokenId: "TOKEN_ID",
      side: OrderSide.BUY,
      amount: 100,
    });
    ```

    ### 列出近期市场交易

    `PublicClient` 和 `SecureClient` 均提供公开交易分页器，用于替代之前的市场事件响应。

    ```ts Before theme={null}
    const trades = await client.getMarketTradesEvents("CONDITION_ID");
    ```

    ```ts After theme={null}
    const pages = client.listTrades({ market: ["CONDITION_ID"] });
    const page = await pages.firstPage();
    const trades = page.items;
    ```

    ## 订单

    ### 提交限价单

    `SecureClient` 在提交限价单前会解析最小变动价位、负风险状态、费用及签名详情。

    ```ts Before theme={null}
    const response = await client.createAndPostOrder(
      {
        tokenID: "TOKEN_ID",
        price: 0.52,
        size: 10,
        side: Side.BUY,
      },
      { tickSize: "0.01", negRisk: false },
      OrderType.GTC,
    );
    ```

    ```ts After theme={null}
    const response = await client.placeLimitOrder({
      tokenId: "TOKEN_ID",
      price: 0.52,
      size: 10,
      side: OrderSide.BUY,
    });
    ```

    为 GTD 订单设置 `expiration`，为仅挂单订单设置 `postOnly: true`。

    ### 下市价单

    `SecureClient` 用于下达市价单。买入时指定要花费的抵押品，卖出时指定要卖出的份额。

    ```ts Before theme={null}
    const response = await client.createAndPostMarketOrder(
      {
        tokenID: "TOKEN_ID",
        amount: 10,
        userUSDCBalance: 10,
        side: Side.BUY,
      },
      { tickSize: "0.01", negRisk: false },
      OrderType.FAK,
    );
    ```

    ```ts After theme={null}
    const response = await client.placeMarketOrder({
      tokenId: "TOKEN_ID",
      amount: 10,
      maxSpend: 10,
      side: OrderSide.BUY,
      orderType: OrderType.FAK,
    });
    ```

    `userUSDCBalance` 变为 `maxSpend`。之前的字段提供用于调整手续费的账户余额；而统一 SDK 改为接受最大总花费金额。当金额应包含手续费时，将 `maxSpend` 设置为等于 `amount`；否则省略它以在额外支付适用手续费。

    ### 仅签名不发布

    当签名和提交需要分开进行时，`SecureClient` 提供特定于订单类型的创建方法。

    ```ts Before theme={null}
    const signedOrder = await client.createOrder(
      {
        tokenID: "TOKEN_ID",
        price: 0.52,
        size: 10,
        side: Side.BUY,
      },
      { tickSize: "0.01", negRisk: false },
    );

    const response = await client.postOrder(signedOrder, OrderType.GTC);
    ```

    ```ts After theme={null}
    const signedOrder = await client.createLimitOrder({
      tokenId: "TOKEN_ID",
      price: 0.52,
      size: 10,
      side: OrderSide.BUY,
    });

    const response = await client.postOrder(signedOrder);
    ```

    准备 FAK 或 FOK 市价单时，请使用 `createMarketOrder`。

    ### 发布多个订单

    `SecureClient` 提供 `postOrders`。订单类型和仅挂单行为是每个已签名订单的一部分，因此该方法直接接受已签名的订单。

    ```ts Before theme={null}
    const response = await client.postOrders([
      { order: firstOrder, orderType: OrderType.GTC },
      { order: secondOrder, orderType: OrderType.GTC },
    ]);
    ```

    ```ts After theme={null}
    const response = await client.postOrders([firstOrder, secondOrder]);
    ```

    ### 将订单归属给 Builder

    `SecureClient` 为每个订单附加 Builder 代码。

    ```ts Before theme={null}
    const response = await client.createAndPostOrder(
      {
        tokenID: "TOKEN_ID",
        price: 0.52,
        size: 10,
        side: Side.BUY,
        builderCode: process.env.POLYMARKET_BUILDER_CODE,
      },
      { tickSize: "0.01", negRisk: false },
    );
    ```

    ```ts After theme={null}
    const response = await client.placeLimitOrder({
      tokenId: "TOKEN_ID",
      price: 0.52,
      size: 10,
      side: OrderSide.BUY,
      builderCode: process.env.POLYMARKET_BUILDER_CODE,
    });
    ```

    ### 取消订单

    `SecureClient` 提供取消方法。它们现在接受 camelCase 请求对象，而 `cancelAll` 仍无需参数。

    ```ts Before theme={null}
    await client.cancelOrder("ORDER_ID");
    await client.cancelOrders(["ORDER_ID_1", "ORDER_ID_2"]);
    await client.cancelAll();
    await client.cancelMarketOrders({
      market: "CONDITION_ID",
      asset_id: "TOKEN_ID",
    });
    ```

    ```ts After theme={null}
    await client.cancelOrder({ orderId: "ORDER_ID" });
    await client.cancelOrders({ orderIds: ["ORDER_ID_1", "ORDER_ID_2"] });
    await client.cancelAll();
    await client.cancelMarketOrders({
      market: "CONDITION_ID",
      tokenId: "TOKEN_ID",
    });
    ```

    ### 获取订单

    `SecureClient` 使用请求对象通过 ID 获取订单。

    ```ts Before theme={null}
    const order = await client.getOrder("ORDER_ID");
    ```

    ```ts After theme={null}
    const order = await client.fetchOrder({ orderId: "ORDER_ID" });
    ```

    ### 列出未成交订单

    `SecureClient` 提供 `listOpenOrders`，返回一个分页器。过滤条件和
    响应字段使用 camelCase（小驼峰命名法）。

    ```ts Before theme={null}
    const orders = await client.getOpenOrders({
      market: "CONDITION_ID",
      asset_id: "TOKEN_ID",
    });
    ```

    ```ts After theme={null}
    const pages = client.listOpenOrders({
      market: "CONDITION_ID",
      tokenId: "TOKEN_ID",
    });
    const orders = [];
    for await (const page of pages) {
      orders.push(...page.items);
    }
    ```

    ### 检查订单评分

    `SecureClient` 提供 `fetchOrderScoring` 用于单个订单，
    以及 `fetchOrdersScoring` 用于多个订单。

    ```ts Before theme={null}
    const scoring = await client.isOrderScoring({ orderId: "ORDER_ID" });
    const batch = await client.areOrdersScoring({
      orderIds: ["ORDER_ID_1", "ORDER_ID_2"],
    });
    ```

    ```ts After theme={null}
    const scoring = await client.fetchOrderScoring({ orderId: "ORDER_ID" });
    const batch = await client.fetchOrdersScoring({
      orderIds: ["ORDER_ID_1", "ORDER_ID_2"],
    });
    ```

    ## 账户活动与余额

    ### 列出账户交易

    `SecureClient` 将之前的两种账户交易历史方法合并为一个
    分页器。

    ```ts Before theme={null}
    const trades = await client.getTrades({ market: "CONDITION_ID" });
    const page = await client.getTradesPaginated({ market: "CONDITION_ID" });
    ```

    ```ts After theme={null}
    const pages = client.listAccountTrades({ market: "CONDITION_ID" });
    const firstPage = await pages.firstPage();

    const trades = [];
    for await (const page of pages) {
      trades.push(...page.items);
    }
    ```

    ### 列出 Builder 交易

    `PublicClient` 和 `SecureClient` 均提供公开的 Builder 交易历史，
    可按 Builder 代码进行过滤。

    ```ts Before theme={null}
    const trades = await client.getBuilderTrades();
    ```

    ```ts After theme={null}
    const pages = client.listBuilderTrades({
      builderCode: process.env.POLYMARKET_BUILDER_CODE,
    });
    const page = await pages.firstPage();
    const trades = page.items;
    ```

    ### 读取并清除通知

    `SecureClient` 提供具有动作导向名称的通知方法以及 camelCase 格式的请求字段。

    ```ts Before theme={null}
    const notifications = await client.getNotifications();
    await client.dropNotifications({ ids: [1, 2] });
    ```

    ```ts After theme={null}
    const notifications = await client.fetchNotifications();
    await client.dropNotifications({ ids: ["1", "2"] });
    ```

    ### 刷新余额和授权额度

    `SecureClient` 在下单时管理余额和授权额度。它会检测缺失的授权额度，完成所需的批准操作，刷新余额和授权额度，并重试下单。

    ```ts Before theme={null}
    await client.updateBalanceAllowance({
      asset_type: AssetType.COLLATERAL,
    });

    const balance = await client.getBalanceAllowance({
      asset_type: AssetType.COLLATERAL,
    });
    ```

    ```ts After theme={null}
    const response = await client.placeLimitOrder({
      tokenId: "TOKEN_ID",
      price: 0.52,
      size: 10,
      side: OrderSide.BUY,
    });
    ```

    ## 持仓

    统一方法负责构建并提交钱包交易。你不再需要编码合约调用或将交易数组传递给 `execute`。

    ### 拆分持仓

    `SecureClient` 将抵押品拆分为完整的 YES 和 NO 结果代币集合。

    ```ts Before theme={null}
    const splitTx = {
      to: CTF_COLLATERAL_ADAPTER_ADDRESS,
      data: collateralAdapterInterface.encodeFunctionData("splitPosition", [
        PUSD_ADDRESS,
        ethers.constants.HashZero,
        conditionId,
        [1, 2],
        ethers.utils.parseUnits("1", 6),
      ]),
      value: "0",
    };

    const split = await relayer.execute([splitTx], "Split position");
    await split.wait();
    ```

    ```ts After theme={null}
    const split = await client.splitPosition({
      conditionId,
      amount: 1_000_000n,
    });

    await split.wait();
    ```

    ### 合并持仓

    `SecureClient` 将平衡的 YES 和 NO 代币重新合并为抵押品。

    ```ts Before theme={null}
    const mergeTx = {
      to: CTF_COLLATERAL_ADAPTER_ADDRESS,
      data: collateralAdapterInterface.encodeFunctionData("mergePositions", [
        PUSD_ADDRESS,
        ethers.constants.HashZero,
        conditionId,
        [1, 2],
        ethers.utils.parseUnits("1", 6),
      ]),
      value: "0",
    };

    const merge = await relayer.execute([mergeTx], "Merge positions");
    await merge.wait();
    ```

    ```ts After theme={null}
    const merge = await client.mergePositions({
      conditionId,
      amount: "max",
    });

    await merge.wait();
    ```

    ### 赎回已结算持仓

    `SecureClient` 在结果确定后，将获胜的结果代币兑换为抵押品。

    ```ts Before theme={null}
    const redeemTx = {
      to: CTF_COLLATERAL_ADAPTER_ADDRESS,
      data: collateralAdapterInterface.encodeFunctionData("redeemPositions", [
        PUSD_ADDRESS,
        ethers.constants.HashZero,
        conditionId,
        [1, 2],
      ]),
      value: "0",
    };

    const redeem = await relayer.execute([redeemTx], "Redeem positions");
    await redeem.wait();
    ```

    ```ts After theme={null}
    const redeem = await client.redeemPositions({ conditionId });
    await redeem.wait();
    ```

    ## 服务状态

    无需调用 `PublicClient` 或 `SecureClient`。统一客户端在内部管理请求时序。

    ```ts Before theme={null}
    await client.getOk();
    const timestamp = await client.getServerTime();
    ```

    ```ts After theme={null}
    // No client call is required for request timestamp synchronization.
    ```
  </Tab>

  <Tab title="Python">
    以下示例将之前的客户端功能映射到 `polymarket-client`。
    SDK 还涵盖了 Gamma API 和 Data API。迁移直接的 Gamma 和
    Data 调用可以简化应用程序代码，并提供与 SDK 的交易和账户工作流无缝配合的标准化、类型化模型。

    公共客户端提供无需身份验证的市场发现和数据读取。
    安全客户端增加了经过身份验证的交易、账户和钱包操作。
    Python 提供了每个客户端的异步和同步版本，因此请选择与应用程序执行模型匹配的接口。

    | 接口 | 客户端                                      | 适用场景               |
    | -- | ---------------------------------------- | ------------------ |
    | 异步 | `AsyncPublicClient`, `AsyncSecureClient` | 使用事件循环的服务、机器人和应用程序 |
    | 同步 | `PublicClient`, `SecureClient`           | 脚本、笔记本和同步应用程序      |

    以下示例使用异步客户端。

    <Note>由于流式传输特性，某些功能仅通过异步客户端提供。</Note>

    ## 安装统一 SDK

    移除旧版 CLOB、Relayer 和 Builder 签名包。然后安装
    统一 SDK。

    <CodeGroup>
      ```bash uv theme={null}
      uv remove py-clob-client-v2 py-builder-relayer-client py-builder-signing-sdk
      uv add polymarket-client
      ```

      ```bash pip theme={null}
      pip uninstall -y py-clob-client-v2 py-builder-relayer-client py-builder-signing-sdk
      pip install polymarket-client
      ```

      ```bash Poetry theme={null}
      poetry remove py-clob-client-v2 py-builder-relayer-client py-builder-signing-sdk
      poetry add polymarket-client
      ```
    </CodeGroup>

    ## 客户端和钱包设置

    ### 创建公共客户端

    `AsyncPublicClient` 提供无需身份验证的市场发现和数据读取。
    生产环境是默认环境。

    ```python Before theme={null}
    from py_clob_client_v2.client import ClobClient

    client = ClobClient("https://clob.polymarket.com", chain_id=137)
    ```

    ```python After theme={null}
    from polymarket import AsyncPublicClient

    client = AsyncPublicClient()
    ```

    ### 创建经过身份验证的客户端

    `AsyncSecureClient` 提供经过身份验证的交易、账户和钱包操作。
    `create` 派生或创建 CLOB 凭据，解析账户钱包，并
    配置签名。

    ```python Before theme={null}
    import os

    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.order_utils.model import SignatureTypeV2

    temporary_client = ClobClient(
        "https://clob.polymarket.com",
        chain_id=137,
        key=os.environ["POLYMARKET_PRIVATE_KEY"],
    )
    credentials = temporary_client.create_or_derive_api_key()

    client = ClobClient(
        "https://clob.polymarket.com",
        chain_id=137,
        key=os.environ["POLYMARKET_PRIVATE_KEY"],
        creds=credentials,
        signature_type=SignatureTypeV2.POLY_1271,
        funder=os.environ["POLYMARKET_WALLET_ADDRESS"],
    )
    ```

    ```python After theme={null}
    import os

    from polymarket import AsyncSecureClient

    client = await AsyncSecureClient.create(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        wallet=os.environ["POLYMARKET_WALLET_ADDRESS"],
    )
    ```

    省略 `wallet` 以使用默认的 Deposit Wallet 流程。

    ### 使用现有凭据恢复

    从经过身份验证的客户端读取凭据，安全存储它们，并将其传递给新客户端以使用相同的 API 密钥恢复。私钥和钱包必须标识拥有该凭据的账户。

    ```python Before theme={null}
    import os

    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.order_utils.model import SignatureTypeV2

    credentials = client.creds
    if credentials is None:
        raise RuntimeError("Client has no credentials")

    # Store the credentials securely.

    resumed_client = ClobClient(
        "https://clob.polymarket.com",
        chain_id=137,
        key=os.environ["POLYMARKET_PRIVATE_KEY"],
        creds=credentials,
        signature_type=SignatureTypeV2.POLY_1271,
        funder=os.environ["POLYMARKET_WALLET_ADDRESS"],
    )
    ```

    ```python After theme={null}
    import os

    from polymarket import AsyncSecureClient

    credentials = client.credentials

    # Store the credentials securely.

    resumed_client = await AsyncSecureClient.create(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        wallet=os.environ["POLYMARKET_WALLET_ADDRESS"],
        credentials=credentials,
    )
    ```

    ### 撤销当前 API 密钥

    结束身份验证将撤销当前 API 密钥，使
    `AsyncSecureClient` 失效，并返回 `AsyncPublicClient`。

    ```python Before theme={null}
    client.delete_api_key()
    ```

    ```python After theme={null}
    public_client = await client.end_authentication()
    ```

    ### 配置 Builder API 密钥

    `AsyncSecureClient` 接受用于无 Gas 钱包操作的 `BuilderApiKey`。请将
    key、secret 和 passphrase 保存在服务器端。

    ```python Before theme={null}
    import os

    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=os.environ["POLYMARKET_BUILDER_API_KEY"],
            secret=os.environ["POLYMARKET_BUILDER_SECRET"],
            passphrase=os.environ["POLYMARKET_BUILDER_PASSPHRASE"],
        )
    )

    relayer = RelayClient(
        "https://relayer-v2.polymarket.com",
        137,
        os.environ["POLYMARKET_PRIVATE_KEY"],
        builder_config,
    )
    ```

    ```python After theme={null}
    import os

    from polymarket import AsyncSecureClient, BuilderApiKey

    client = await AsyncSecureClient.create(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        api_key=BuilderApiKey(
            key=os.environ["POLYMARKET_BUILDER_API_KEY"],
            secret=os.environ["POLYMARKET_BUILDER_SECRET"],
            passphrase=os.environ["POLYMARKET_BUILDER_PASSPHRASE"],
        ),
    )
    ```

    ### 配置 Relayer API 密钥

    `AsyncSecureClient` 直接配置 Relayer API 密钥。在重新连接现有账户时，传递密钥及其
    关联的地址。

    ```python theme={null}
    import os

    from polymarket import AsyncSecureClient, RelayerApiKey

    client = await AsyncSecureClient.create(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        wallet=os.environ["POLYMARKET_WALLET_ADDRESS"],
        api_key=RelayerApiKey(
            key=os.environ["POLYMARKET_RELAYER_API_KEY"],
            address=os.environ["POLYMARKET_RELAYER_API_KEY_ADDRESS"],
        ),
    )
    ```

    ### 部署 Deposit Wallet

    在不提供 `wallet` 的情况下创建 `AsyncSecureClient` 会派生签名者的 Deposit
    Wallet 地址，并在需要时进行部署。使用 Builder 授权来批准该部署。

    ```python Before theme={null}
    deployment = relayer.deploy_deposit_wallet()
    deployment.wait()
    ```

    ```python After theme={null}
    import os

    from polymarket import AsyncSecureClient, BuilderApiKey

    client = await AsyncSecureClient.create(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        api_key=BuilderApiKey(
            key=os.environ["POLYMARKET_BUILDER_API_KEY"],
            secret=os.environ["POLYMARKET_BUILDER_SECRET"],
            passphrase=os.environ["POLYMARKET_BUILDER_PASSPHRASE"],
        ),
    )

    deposit_wallet_address = client.wallet
    ```

    ## 市场发现

    ### 获取市场

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供 `get_market`，用于通过 Gamma 市场 ID、slug 或 Polymarket URL 读取数据。

    ```python Before theme={null}
    market = client.get_market("CONDITION_ID")
    ```

    ```python After theme={null}
    market = await client.get_market(slug="MARKET_SLUG")
    ```

    如果你只有条件 ID，请筛选 `list_markets` 并获取其第一页。

    ```python theme={null}
    pages = client.list_markets(condition_ids=["CONDITION_ID"])
    page = await pages.first_page()
    market = page.items[0]
    ```

    ### 列出市场

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供 `list_markets`，它返回一个异步分页器并支持直接传入筛选条件。

    ```python Before theme={null}
    page = client.get_markets()
    markets = page["data"]
    ```

    ```python After theme={null}
    pages = client.list_markets(closed=False, page_size=20)
    page = await pages.first_page()
    markets = page.items
    ```

    简化版市场读取无需单独的替换方案。采样市场读取迁移至 `list_current_rewards`，用于列出当前的奖励配置。

    ```python Before theme={null}
    markets = client.get_sampling_markets()
    page = client.get_sampling_simplified_markets()
    ```

    ```python After theme={null}
    pages = client.list_current_rewards()
    page = await pages.first_page()
    ```

    ## 市场数据

    ### 读取交易参数

    `AsyncPublicClient` 和 `AsyncSecureClient` 返回市场模型中的 tick size 和负风险状态。订单方法也会自动解析这些值。

    ```python Before theme={null}
    tick_size = client.get_tick_size("TOKEN_ID")
    neg_risk = client.get_neg_risk("TOKEN_ID")
    fee_rate_bps = client.get_fee_rate_bps("TOKEN_ID")
    ```

    ```python After theme={null}
    market = await client.get_market(slug="MARKET_SLUG")
    tick_size = market.trading.minimum_tick_size
    neg_risk = market.state.neg_risk
    fee_schedule = market.trading.fee_schedule
    ```

    ### 读取 CLOB 市场详情

    `AsyncPublicClient` 和 `AsyncSecureClient` 返回类型化的市场模型。其交易字段取代了紧凑的 CLOB 市场响应。

    ```python Before theme={null}
    details = client.get_clob_market_info("CONDITION_ID")
    tick_size = details["mts"]
    neg_risk = details["nr"]
    fee_details = details["fd"]
    ```

    ```python After theme={null}
    market = await client.get_market(slug="MARKET_SLUG")
    tick_size = market.trading.minimum_tick_size
    neg_risk = market.state.neg_risk
    fee_schedule = market.trading.fee_schedule
    ```

    ### 获取订单簿

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供单次和批量的订单簿读取功能。

    ```python Before theme={null}
    from py_clob_client_v2 import BookParams

    book = client.get_order_book("TOKEN_ID")
    books = client.get_order_books(
        [BookParams(token_id="TOKEN_ID_1"), BookParams(token_id="TOKEN_ID_2")]
    )
    ```

    ```python After theme={null}
    book = await client.get_order_book(token_id="TOKEN_ID")
    books = await client.get_order_books(token_ids=["TOKEN_ID_1", "TOKEN_ID_2"])
    ```

    ### 获取价格

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供带有类型化请求的单次和批量价格读取功能。

    ```python Before theme={null}
    from py_clob_client_v2 import BookParams, Side

    price = client.get_price("TOKEN_ID", Side.BUY)
    prices = client.get_prices(
        [
            BookParams(token_id="TOKEN_ID_1", side=Side.BUY),
            BookParams(token_id="TOKEN_ID_2", side=Side.SELL),
        ]
    )
    ```

    ```python After theme={null}
    from polymarket import PriceRequest

    price = await client.get_price(token_id="TOKEN_ID", side="BUY")
    prices = await client.get_prices(
        requests=[
            PriceRequest(token_id="TOKEN_ID_1", side="BUY"),
            PriceRequest(token_id="TOKEN_ID_2", side="SELL"),
        ]
    )
    ```

    ### 获取中间价

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供单次和批量的中间价读取功能。

    ```python Before theme={null}
    from py_clob_client_v2 import BookParams

    midpoint = client.get_midpoint("TOKEN_ID")
    midpoints = client.get_midpoints(
        [BookParams(token_id="TOKEN_ID_1"), BookParams(token_id="TOKEN_ID_2")]
    )
    ```

    ```python After theme={null}
    midpoint = await client.get_midpoint(token_id="TOKEN_ID")
    midpoints = await client.get_midpoints(token_ids=["TOKEN_ID_1", "TOKEN_ID_2"])
    ```

    ### 获取价差

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供单次和批量的价差读取功能。

    ```python Before theme={null}
    from py_clob_client_v2 import BookParams

    spread = client.get_spread("TOKEN_ID")
    spreads = client.get_spreads(
        [BookParams(token_id="TOKEN_ID_1"), BookParams(token_id="TOKEN_ID_2")]
    )
    ```

    ```python After theme={null}
    spread = await client.get_spread(token_id="TOKEN_ID")
    spreads = await client.get_spreads(token_ids=["TOKEN_ID_1", "TOKEN_ID_2"])
    ```

    ### 获取最新成交价

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供用于获取最新成交价的单次和批量方法。

    ```python Before theme={null}
    from py_clob_client_v2 import BookParams

    last_trade = client.get_last_trade_price("TOKEN_ID")
    last_trades = client.get_last_trades_prices(
        [BookParams(token_id="TOKEN_ID_1"), BookParams(token_id="TOKEN_ID_2")]
    )
    ```

    ```python After theme={null}
    last_trade = await client.get_last_trade_price(token_id="TOKEN_ID")
    last_trades = await client.get_last_trade_prices(
        token_ids=["TOKEN_ID_1", "TOKEN_ID_2"]
    )
    ```

    ### 获取价格历史

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供价格历史数据。将参数作为关键字参数传递，响应中包含类型化的数据点。

    ```python Before theme={null}
    from py_clob_client_v2 import PricesHistoryParams

    history = client.get_prices_history(
        PricesHistoryParams(market="TOKEN_ID", interval="1d", fidelity=60)
    )
    ```

    ```python After theme={null}
    history = await client.get_price_history(
        token_id="TOKEN_ID",
        interval="1d",
        fidelity=60,
    )
    ```

    ### 估算市价单价格

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供市价单价格估算。买入时指定花费的抵押品，卖出时指定出售的份额数量。

    ```python Before theme={null}
    from py_clob_client_v2 import OrderType, Side

    buy_price = client.calculate_market_price(
        "TOKEN_ID", Side.BUY, 10, OrderType.FOK
    )
    sell_price = client.calculate_market_price(
        "TOKEN_ID", Side.SELL, 5, OrderType.FOK
    )
    ```

    ```python After theme={null}
    buy_price = await client.estimate_market_price(
        token_id="TOKEN_ID", side="BUY", amount=10, order_type="FOK"
    )
    sell_price = await client.estimate_market_price(
        token_id="TOKEN_ID", side="SELL", shares=5, order_type="FOK"
    )
    ```

    ### 列出近期市场交易

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供公共交易分页器，用于替代之前的市场事件响应。

    ```python Before theme={null}
    trades = client.get_market_trades_events("CONDITION_ID")
    ```

    ```python After theme={null}
    pages = client.list_trades(market=["CONDITION_ID"])
    page = await pages.first_page()
    trades = page.items
    ```

    ## 订单

    ### 提交限价单

    `AsyncSecureClient` 在提交限价单前会解析最小变动价位、负风险状态、费用以及签名详情。

    ```python Before theme={null}
    from py_clob_client_v2 import OrderArgs, Side

    response = client.create_and_post_order(
        OrderArgs(
            token_id="TOKEN_ID",
            price=0.52,
            size=10,
            side=Side.BUY,
        )
    )
    ```

    ```python After theme={null}
    response = await client.place_limit_order(
        token_id="TOKEN_ID",
        price=0.52,
        size=10,
        side="BUY",
    )
    ```

    ### 提交市价单

    `AsyncSecureClient` 用于提交市价单。买入时指定要花费的金额，卖出时指定要出售的份额数量。

    ```python Before theme={null}
    from py_clob_client_v2 import MarketOrderArgs, OrderType, Side

    response = client.create_and_post_market_order(
        MarketOrderArgs(
            token_id="TOKEN_ID",
            amount=10,
            user_usdc_balance=10,
            side=Side.BUY,
        ),
        order_type=OrderType.FOK,
    )
    ```

    ```python After theme={null}
    response = await client.place_market_order(
        token_id="TOKEN_ID",
        side="BUY",
        amount=10,
        max_spend=10,
        order_type="FOK",
    )
    ```

    `user_usdc_balance` 变为 `max_spend`。之前的字段提供用于调整订单费用的账户余额；而统一的 SDK 改为接受最大总支出金额。当金额应包含费用时，将 `max_spend` 设置为与 `amount` 相等；若希望额外支付适用费用，则省略该字段。

    ### 仅签名不发布

    当签名和提交需要分开进行时，`AsyncSecureClient` 提供特定于订单类型的创建方法。

    ```python Before theme={null}
    from py_clob_client_v2 import MarketOrderArgs, OrderArgs, Side

    limit_order = client.create_order(
        OrderArgs(token_id="TOKEN_ID", price=0.52, size=10, side=Side.BUY)
    )
    market_order = client.create_market_order(
        MarketOrderArgs(token_id="TOKEN_ID", amount=10, side=Side.BUY)
    )
    ```

    ```python After theme={null}
    limit_order = await client.create_limit_order(
        token_id="TOKEN_ID",
        price=0.52,
        size=10,
        side="BUY",
    )
    market_order = await client.create_market_order(
        token_id="TOKEN_ID",
        side="BUY",
        amount=10,
    )
    ```

    ### 发布多笔订单

    `AsyncSecureClient` 提供 `post_orders`。订单类型和仅挂单行为是每个签名订单的一部分，因此该方法直接接受签名订单。

    ```python Before theme={null}
    from py_clob_client_v2 import OrderType, PostOrdersV2Args

    responses = client.post_orders(
        [
            PostOrdersV2Args(order=first_order, orderType=OrderType.GTC),
            PostOrdersV2Args(order=second_order, orderType=OrderType.GTC),
        ]
    )
    ```

    ```python After theme={null}
    responses = await client.post_orders([first_order, second_order])
    ```

    ### 将订单归属给 Builder

    `AsyncSecureClient` 将 Builder 代码附加到每个订单。

    ```python Before theme={null}
    from py_clob_client_v2 import OrderArgs, Side

    response = client.create_and_post_order(
        OrderArgs(
            token_id="TOKEN_ID",
            price=0.52,
            size=10,
            side=Side.BUY,
            builder_code="BUILDER_CODE",
        )
    )
    ```

    ```python After theme={null}
    response = await client.place_limit_order(
        token_id="TOKEN_ID",
        price=0.52,
        size=10,
        side="BUY",
        builder_code="BUILDER_CODE",
    )
    ```

    ### 取消订单

    `AsyncSecureClient` 提供用于取消单笔订单、多笔订单、所有订单或特定市场订单的显式方法。

    ```python Before theme={null}
    from py_clob_client_v2 import OrderMarketCancelParams, OrderPayload

    client.cancel_order(OrderPayload(orderID="ORDER_ID"))
    client.cancel_orders(["ORDER_ID_1", "ORDER_ID_2"])
    client.cancel_market_orders(OrderMarketCancelParams(market="CONDITION_ID"))
    client.cancel_all()
    ```

    ```python After theme={null}
    await client.cancel_order(order_id="ORDER_ID")
    await client.cancel_orders(order_ids=["ORDER_ID_1", "ORDER_ID_2"])
    await client.cancel_market_orders(market="CONDITION_ID")
    await client.cancel_all()
    ```

    ### 获取订单

    `AsyncSecureClient` 使用关键字参数通过 ID 获取订单。

    ```python Before theme={null}
    order = client.get_order("ORDER_ID")
    ```

    ```python After theme={null}
    order = await client.get_order(order_id="ORDER_ID")
    ```

    ### 列出未成交订单

    `AsyncSecureClient` 提供 `list_open_orders`，返回一个异步分页器。

    ```python Before theme={null}
    from py_clob_client_v2 import OpenOrderParams

    orders = client.get_open_orders(
        OpenOrderParams(market="CONDITION_ID", asset_id="TOKEN_ID")
    )
    ```

    ```python After theme={null}
    pages = client.list_open_orders(market="CONDITION_ID", token_id="TOKEN_ID")
    orders = [order async for order in pages.iter_items()]
    ```

    ### 检查订单评分

    `AsyncSecureClient` 提供 `get_order_scoring`（用于单笔订单）和 `get_orders_scoring`（用于多笔订单）。

    ```python Before theme={null}
    from py_clob_client_v2 import OrderScoringParams, OrdersScoringParams

    scoring = client.is_order_scoring(OrderScoringParams(orderId="ORDER_ID"))
    batch = client.are_orders_scoring(
        OrdersScoringParams(orderIds=["ORDER_ID_1", "ORDER_ID_2"])
    )
    ```

    ```python After theme={null}
    scoring = await client.get_order_scoring(order_id="ORDER_ID")
    batch = await client.get_orders_scoring(
        order_ids=["ORDER_ID_1", "ORDER_ID_2"]
    )
    ```

    ## 账户活动与余额

    ### 列出账户交易

    `AsyncSecureClient` 将之前的两种账户交易历史方法合并为一个异步分页器。

    ```python Before theme={null}
    from py_clob_client_v2 import TradeParams

    trades = client.get_trades(TradeParams(market="CONDITION_ID"))
    page = client.get_trades_paginated(TradeParams(market="CONDITION_ID"))
    ```

    ```python After theme={null}
    pages = client.list_account_trades(market="CONDITION_ID")
    first_page = await pages.first_page()

    trades = [trade async for trade in pages.iter_items()]
    ```

    ### 列出 Builder 交易

    `AsyncPublicClient` 和 `AsyncSecureClient` 均提供按 Builder 代码过滤的公开 Builder 交易历史。

    ```python Before theme={null}
    from py_clob_client_v2 import BuilderTradeParams

    page = client.get_builder_trades(
        BuilderTradeParams(builder_code="BUILDER_CODE")
    )
    trades = page["trades"]
    ```

    ```python After theme={null}
    pages = client.list_builder_trades(builder_code="BUILDER_CODE")
    page = await pages.first_page()
    trades = page.items
    ```

    ### 读取并清除通知

    `AsyncSecureClient` 提供带有关键字参数和类型化响应的通知方法。

    ```python Before theme={null}
    from py_clob_client_v2 import DropNotificationParams

    notifications = client.get_notifications()
    client.drop_notifications(DropNotificationParams(ids=[1, 2]))
    ```

    ```python After theme={null}
    notifications = await client.get_notifications()
    await client.drop_notifications(ids=["1", "2"])
    ```

    ### 刷新余额与授权额度

    `AsyncSecureClient` 在下单时读取余额并管理缺失的授权额度。它会完成所需的批准、刷新授权额度并重试订单。

    ```python Before theme={null}
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    client.update_balance_allowance(params)
    balance = client.get_balance_allowance(params)
    ```

    ```python After theme={null}
    balance = await client.get_balance_allowance(asset_type="COLLATERAL")
    response = await client.place_limit_order(
        token_id="TOKEN_ID",
        price=0.52,
        size=10,
        side="BUY",
    )
    ```

    ## 持仓

    统一的方法会构建并提交钱包交易。你不再需要编码合约调用或将交易数组传递给 `execute`。

    ### 拆分持仓

    `AsyncSecureClient` 将抵押品拆分为完整的 YES 和 NO 结果代币。

    ```python Before theme={null}
    from py_builder_relayer_client.models import Transaction

    split_tx = Transaction(
        to=CTF_COLLATERAL_ADAPTER_ADDRESS,
        data=collateral_adapter.encode_abi(
            "splitPosition",
            args=[PUSD_ADDRESS, bytes(32), CONDITION_ID, [1, 2], 1_000_000],
        ),
        value="0",
    )

    split = relayer.execute([split_tx], "Split position")
    split.wait()
    ```

    ```python After theme={null}
    split = await client.split_position(
        condition_id="CONDITION_ID",
        amount=1_000_000,
    )
    await split.wait()
    ```

    ### 合并持仓

    `AsyncSecureClient` 将平衡的 YES 和 NO 代币重新合并为抵押品。

    ```python Before theme={null}
    from py_builder_relayer_client.models import Transaction

    merge_tx = Transaction(
        to=CTF_COLLATERAL_ADAPTER_ADDRESS,
        data=collateral_adapter.encode_abi(
            "mergePositions",
            args=[PUSD_ADDRESS, bytes(32), CONDITION_ID, [1, 2], 1_000_000],
        ),
        value="0",
    )

    merge = relayer.execute([merge_tx], "Merge positions")
    merge.wait()
    ```

    ```python After theme={null}
    merge = await client.merge_positions(
        condition_id="CONDITION_ID",
        amount="max",
    )
    await merge.wait()
    ```

    ### 赎回已结算持仓

    `AsyncSecureClient` 在结果确定后，将获胜的结果代币兑换为抵押品。

    ```python Before theme={null}
    from py_builder_relayer_client.models import Transaction

    redeem_tx = Transaction(
        to=CTF_COLLATERAL_ADAPTER_ADDRESS,
        data=collateral_adapter.encode_abi(
            "redeemPositions",
            args=[PUSD_ADDRESS, bytes(32), CONDITION_ID, [1, 2]],
        ),
        value="0",
    )

    redeem = relayer.execute([redeem_tx], "Redeem positions")
    redeem.wait()
    ```

    ```python After theme={null}
    redeem = await client.redeem_positions(condition_id="CONDITION_ID")
    await redeem.wait()
    ```

    ## 服务状态

    `AsyncPublicClient` 和 `AsyncSecureClient` 均不需要这些调用。统一客户端在内部管理请求时序。

    ```python Before theme={null}
    client.get_ok()
    timestamp = client.get_server_time()
    ```

    ```python After theme={null}
    # No client call is required for request timestamp synchronization.
    ```
  </Tab>

  <Tab title="Rust">
    统一的 Rust SDK 正在开发中。在它可用之前，请使用 `polymarket_client_sdk_v2` `0.7.0`（当前的 Rust SDK）。

    有关完整的 API 和示例，请参阅 [GitHub 仓库](https://github.com/Polymarket/rs-clob-client-v2) 以及 [crates.io 上的 `polymarket_client_sdk_v2` 包](https://crates.io/crates/polymarket_client_sdk_v2)。

    ## 安装 Rust SDK

    启用 `clob` 功能以进行身份验证和交易：

    ```bash theme={null}
    cargo add polymarket_client_sdk_v2@0.7.0 --features clob
    ```

    ## 使用 Deposit Wallet 交易

    Rust SDK 支持现有 Deposit Wallet 的 CLOB 订单路径。它不包含用于部署 Deposit Wallet 或提交钱包批次的客户端；请针对这些操作使用 Direct API 工作流。

    将已部署的 Deposit Wallet 作为资金提供方，并在认证时使用 `SignatureType::Poly1271`。此示例会刷新抵押品额度缓存，然后下达 GTC 限价买单：

    ```rust theme={null}
    use std::str::FromStr as _;

    use alloy::signers::Signer as _;
    use alloy::signers::local::LocalSigner;
    use polymarket_client_sdk_v2::clob::types::request::UpdateBalanceAllowanceRequest;
    use polymarket_client_sdk_v2::clob::types::{
        AssetType, OrderType, Side, SignatureType,
    };
    use polymarket_client_sdk_v2::clob::{Client, Config};
    use polymarket_client_sdk_v2::types::{Address, Decimal, U256};
    use polymarket_client_sdk_v2::{POLYGON, PRIVATE_KEY_VAR};

    #[tokio::main]
    async fn main() -> anyhow::Result<()> {
        let host = "https://clob-v2.polymarket.com";
        let signer = LocalSigner::from_str(&std::env::var(PRIVATE_KEY_VAR)?)?
            .with_chain_id(Some(POLYGON));
        let deposit_wallet = Address::from_str(&std::env::var("DEPOSIT_WALLET")?)?;

        let client = Client::new(host, Config::default())?
            .authentication_builder(&signer)
            .funder(deposit_wallet)
            .signature_type(SignatureType::Poly1271)
            .authenticate()
            .await?;

        client
            .update_balance_allowance(
                UpdateBalanceAllowanceRequest::builder()
                    .asset_type(AssetType::Collateral)
                    .build(),
            )
            .await?;

        let response = client
            .limit_order()
            .token_id(U256::from_str(&std::env::var("TOKEN_ID")?)?)
            .side(Side::Buy)
            .price(Decimal::from_str("0.40")?)
            .size(Decimal::from_str("100")?)
            .order_type(OrderType::GTC)
            .build_sign_and_post(&signer)
            .await?;

        println!("order_id={} status={}", response.order_id, response.status);
        Ok(())
    }
    ```

    ## 处理匹配引擎重启

    在每次重试前重新构建并签署订单，因为 `SignedOrder` 在提交后会被消耗。将 HTTP `425` 视为临时状态，并使用指数退避策略进行重试：

    ```rust theme={null}
    use polymarket_client_sdk_v2::error::{Kind, StatusCode};

    let mut delay = std::time::Duration::from_secs(1);

    for _ in 0..10 {
        let order = client.limit_order()
            .token_id(token_id).price(price).size(size).side(side)
            .build().await?;
        let signed = client.sign(&signer, order).await?;

        match client.post_order(signed).await {
            Ok(response) => return Ok(response),
            Err(err) if err.kind() == Kind::Status => {
                if let Some(status) = err.downcast_ref::<polymarket_client_sdk_v2::error::Status>() {
                    if status.status_code == StatusCode::from_u16(425).unwrap() {
                        eprintln!("Engine restarting, retrying in {delay:?}...");
                        tokio::time::sleep(delay).await;
                        delay = (delay * 2).min(std::time::Duration::from_secs(30));
                        continue;
                    }
                }
                return Err(err);
            }
            Err(err) => return Err(err),
        }
    }
    ```

    有关重启窗口、仅挂单时段（post-only period）和受限模式响应，请参阅 [匹配引擎重启](/cn/trading/matching-engine)。
  </Tab>
</Tabs>
