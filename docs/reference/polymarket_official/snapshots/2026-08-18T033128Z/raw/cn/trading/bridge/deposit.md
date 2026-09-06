> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 存款

> 从任何受支持的链桥接资产，为你的 Polymarket 账户充值

Polymarket 使用 Polygon 上的 **pUSD**（Polymarket USD）作为所有交易的抵押品。
Bridge API 允许你从 Ethereum、Solana、Bitcoin 和其他链存入资产，这些资产会
自动转换为 Polygon 上的 pUSD。

## 工作原理

1. 为你的 Polymarket 钱包请求跨链桥地址
2. 将资产发送到与源链对应的地址
3. 资产会自动桥接并兑换为 pUSD
4. pUSD 会记入你的钱包，用于交易

## 创建跨链桥地址

生成与你的 Polymarket 钱包关联的唯一跨链桥地址。有关完整的请求和响应模式，
请参阅[创建跨链桥地址](/api-reference/bridge/create-bridge-addresses)。

<Tip>
  **构建者：附加你的代码。** 如果你通过此端点路由用户资金，请通过可选的
  `X-Builder-Code` 标头传递构建者代码（bytes32 十六进制；`0x` + 64 个
  十六进制字符）。这让跨链桥提供商能够将流量归因到你的应用，从而追踪并
  优先处理卡住或延迟的转账。该标头为可选项。未提供时请求仍会成功，但会返回
  `missing_builder_code` 警告；格式错误的代码会返回 `400`。请前往 [Settings →
  Builder](https://polymarket.com/settings?tab=builder) 获取代码。
</Tip>

```bash theme={null}
curl -X POST https://bridge.polymarket.com/deposit \
  -H "Content-Type: application/json" \
  -H "X-Builder-Code: <builder_code>" \
  -d '{"address": "0x56687bf447db6ffa42ffe2204a05edaa20f55839"}'
```

响应会为每种地址类型（`evm`、`svm`、`btc`、`tron`）返回一个跨链桥地址。
请从匹配的源链向对应类型的地址发送资产。

### 地址类型

| 地址     | 用途                                        |
| ------ | ----------------------------------------- |
| `evm`  | Ethereum、Arbitrum、Base、Optimism 和其他 EVM 链 |
| `svm`  | Solana                                    |
| `btc`  | Bitcoin                                   |
| `tron` | Tron                                      |

<Warning>
  每个地址都只与你的钱包关联。只能从受支持的链向正确的地址类型发送资产。
</Warning>

## 存款流程

<Steps>
  <Step title="获取跨链桥地址">
    使用你的 Polymarket 钱包地址调用 `POST /deposit`，获取跨链桥地址。
  </Step>

  <Step title="检查受支持资产">
    通过 `/supported-assets` 验证代币是否受支持，并确认其满足最低存款金额。
  </Step>

  <Step title="发送资产">从源链将代币转入相应的跨链桥地址。</Step>
  <Step title="跟踪状态">使用 `/status/{address}` 监控存款进度。</Step>
</Steps>

## USDC 与 pUSD

你可以将 USDC（原生）或 USDC.e（桥接）作为源资产存入 Polymarket 钱包。
无论使用哪一种，收到的 USDC 或 USDC.e 都会通过 Collateral Onramp 封装为
pUSD；pUSD 是你在 Polymarket 上持有和交易的资产。

## 大额存款

对于从 Polygon 以外链发起且超过 \$50,000 的存款，建议使用第三方跨链桥
以尽量降低滑点：

* [DeBridge](https://app.debridge.finance/)
* [Across](https://app.across.to/bridge)
* [Portal](https://portalbridge.com/)

请直接桥接到你的 Polymarket USDC（Polygon）跨链桥地址。Polymarket 与任何
第三方跨链桥均无关联，也不对其承担责任。

## 最低存款金额

每种资产都有最低存款金额。低于最低金额的存款不会被处理。请通过
`/supported-assets` 查看当前最低金额。

## 存款恢复

如果存入了错误的代币，请使用此工具恢复资金：

[recovery.polymarket.com](https://recovery.polymarket.com/)

<Warning>
  发送不受支持的代币可能造成**无法挽回的损失**。存款前，请务必确认代币
  已列在[受支持资产](/cn/trading/bridge/supported-assets)中。
</Warning>

## 后续步骤

<CardGroup cols={2}>
  <Card title="受支持资产" icon="coins" href="/cn/trading/bridge/supported-assets">
    查看所有受支持的链、代币和最低金额。
  </Card>

  <Card title="检查状态" icon="clock" href="/cn/trading/bridge/status">
    跟踪存款进度直至完成。
  </Card>
</CardGroup>
