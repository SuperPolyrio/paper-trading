> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 受支持资产

> 支持存入 Polymarket 的链和代币

Bridge API 支持从多条链存入多种代币。所有存款都会自动转换为
**Polygon 上的 pUSD**，作为在 Polymarket 上交易的抵押品。

## 获取受支持资产

获取受支持链和代币的完整列表，以及各自的最低存款金额。

```bash theme={null}
curl https://bridge.polymarket.com/supported-assets
```

```json theme={null}
{
  "supportedAssets": [
    {
      "chainId": "137",
      "chainName": "Polygon",
      "token": {
        "name": "USD Coin",
        "symbol": "USDC",
        "address": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
        "decimals": 6
      },
      "minCheckoutUsd": 2
    }
  ]
}
```

## 受支持的链

跨链桥支持从以下区块链网络存款：

| 链               | 地址类型 | 最低存款 | 代币示例                                        |
| --------------- | ---- | ---- | ------------------------------------------- |
| Ethereum        | EVM  | \$7  | ETH, USDC, USDT, WBTC, DAI, LINK, UNI, AAVE |
| Polygon         | EVM  | \$2  | POL, USDC, USDT, DAI, WETH, SAND            |
| Arbitrum        | EVM  | \$2  | ETH, ARB, USDC, USDT, DAI, WBTC, USDe       |
| Base            | EVM  | \$2  | ETH, USDC, USDT, DAI, cbBTC, AERO, USDS     |
| Optimism        | EVM  | \$2  | ETH, OP, USDC, USDT, DAI, USDe              |
| BNB Smart Chain | EVM  | \$2  | BNB, USDC, USDT, DAI, ETH, BTCB, BUSD       |
| Solana          | SVM  | \$2  | SOL, USDC, USDT, USDe, TRUMP                |
| Bitcoin         | BTC  | \$9  | BTC                                         |
| Tron            | Tron | \$9  | USDT                                        |
| HyperEVM        | EVM  | \$2  | HYPE, USDC, USDe, stHYPE, UBTC, UETH        |
| Abstract        | EVM  | \$2  | ETH, USDC, USDT                             |
| Monad           | EVM  | \$2  | MON, USDC, USDT                             |
| Ethereal        | EVM  | \$2  | USDe, WUSDe                                 |
| Katana          | EVM  | \$2  | AUSD                                        |
| Lighter         | EVM  | \$2  | USDC                                        |

<Note>
  受支持资产会随时间变化。发起存款前，请始终调用 `/supported-assets`
  获取当前列表。
</Note>

## 最低金额

每种资产都有一个 `minCheckoutUsd` 值，表示以美元等值计算的最低存款金额。
低于此阈值的存款可能无法处理，因此在告知用户发送金额前请先检查该值。

大多数 L2 链（Polygon、Arbitrum、Base、Optimism）的最低金额较低，为 $2；
Ethereum 存款最低为 $7。由于桥接成本较高，Bitcoin 和 Tron 的最低金额为 \$9。

## 后续步骤

<CardGroup cols={2}>
  <Card title="创建存款" icon="arrow-right-to-bracket" href="/cn/trading/bridge/deposit">
    为你的钱包生成跨链桥地址。
  </Card>

  <Card title="检查状态" icon="clock" href="/cn/trading/bridge/status">
    跟踪存款进度。
  </Card>
</CardGroup>
