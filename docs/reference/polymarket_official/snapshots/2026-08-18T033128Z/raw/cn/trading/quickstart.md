> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 下第一笔订单

> 了解如何通过 CLOB 进行身份验证并提交第一笔市价单。

本指南介绍如何使用现有 Polymarket 账户下第一笔订单。建议开始前至少准备
10 pUSD。请先在 [polymarket.com](https://polymarket.com/) 创建账户或充值。

你可以在个人资料菜单中找到 Polymarket 钱包地址：

<Frame>
  <img className="hidden lg:block" src="https://mintcdn.com/polymarket-292d1b1b/1lJ_npwaE_MShiVL/images/deposit-wallet-desktop.png?fit=max&auto=format&n=1lJ_npwaE_MShiVL&q=85&s=6be3b87c53d6718f973db37e134ee944" alt="桌面端 Polymarket 个人资料菜单中显示的账户钱包地址" width="1280" height="274" data-path="images/deposit-wallet-desktop.png" />

  <img className="block lg:hidden" src="https://mintcdn.com/polymarket-292d1b1b/1lJ_npwaE_MShiVL/images/deposit-wallet-mobile.png?fit=max&auto=format&n=1lJ_npwaE_MShiVL&q=85&s=4d6f87ed5440c5297e60d6331c47dc73" alt="移动端 Polymarket 个人资料菜单中显示的账户钱包地址" width="529" height="274" data-path="images/deposit-wallet-mobile.png" />
</Frame>

<Steps>
  <Step title="进行身份验证">
    首先，通过 CLOB 进行身份验证。

    <Tabs>
      <Tab title="TypeScript">
        将签名者和钱包地址传给 `createSecureClient`。

        ```ts theme={null}
        import { createSecureClient, OrderSide } from "@polymarket/client";
        import { privateKey } from "@polymarket/client/viem";

        const client = await createSecureClient({
          wallet: process.env.POLYMARKET_WALLET_ADDRESS,
          signer: privateKey(process.env.POLYMARKET_PRIVATE_KEY),
        });
        ```

        <Note>
          此示例使用 Viem。请参阅[钱包
          集成](/cn/getting-started/typescript#钱包集成)，了解如何连接
          其他受支持钱包库中的签名者。
        </Note>
      </Tab>

      <Tab title="Python">
        将私钥和钱包地址传给 `AsyncSecureClient.create`。

        ```python theme={null}
        import os

        from polymarket import AsyncSecureClient

        client = await AsyncSecureClient.create(
            private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
            wallet=os.environ["POLYMARKET_WALLET_ADDRESS"],
        )
        ```
      </Tab>
    </Tabs>
  </Step>

  <Step title="选择结果">
    然后，获取市场并选择你要买入的结果。订单通过代币 ID 标识每个结果。
    如需查找其他市场，请参阅[市场数据](/cn/market-data/overview)。

    <Tabs>
      <Tab title="TypeScript">
        ```ts theme={null}
        const market = await client.fetchMarket({
          slug: "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
        });

        const tokenId = market.outcomes.yes.tokenId!;
        ```
      </Tab>

      <Tab title="Python">
        ```python theme={null}
        market = await client.get_market(
            slug="will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
        )

        token_id = market.outcomes.yes.token_id
        assert token_id is not None
        ```
      </Tab>
    </Tabs>
  </Step>

  <Step title="下市价单">
    接着，提交一笔小额市价买单。订单会与可用流动性成交，未成交部分会被取消，
    而不会继续挂在订单簿上。

    <Tabs>
      <Tab title="TypeScript">
        使用 `placeMarketOrder` 下市价单。

        ```ts theme={null}
        const response = await client.placeMarketOrder({
          tokenId,
          side: OrderSide.BUY,
          amount: "10", // Spend up to 10 pUSD
        });

        if (!response.ok) {
          throw new Error(response.message);
        }

        // response.orderId: string
        ```
      </Tab>

      <Tab title="Python">
        使用 `place_market_order` 下市价单。

        ```python theme={null}
        response = await client.place_market_order(
            token_id=token_id,
            side="BUY",
            amount="10",  # Spend up to 10 pUSD
        )

        if not response.ok:
            raise RuntimeError(response.message)

        # response.order_id: str
        ```
      </Tab>
    </Tabs>
  </Step>

  <Step title="检查仓位">
    最后，在交易结算后，列出所选市场的仓位，并找到你买入的结果。

    <Tabs>
      <Tab title="TypeScript">
        ```ts theme={null}
        const page = await client
          .listPositions({
            market: [market.conditionId!],
          })
          .firstPage();

        const position = page.items.find((item) => item.tokenId === tokenId);
        if (!position) {
          throw new Error("Position not found.");
        }

        const positionSize = position.size; // Outcome shares held
        ```
      </Tab>

      <Tab title="Python">
        ```python theme={null}
        condition_id = market.condition_id
        assert condition_id is not None

        page = await client.list_positions(market=[condition_id]).first_page()

        position = next(
            (item for item in page.items if item.token_id == token_id),
            None,
        )
        if position is None:
            raise RuntimeError("Position not found.")

        position_size = position.size  # Outcome shares held
        ```
      </Tab>
    </Tabs>

    完成了——你已经在 Polymarket 下了第一笔市价单。
  </Step>
</Steps>
