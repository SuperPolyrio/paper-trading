> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 提款

> 将 pUSD 从 Polymarket 桥接到任何受支持的链

将 Polymarket 钱包中的 pUSD 提取为任何受支持链上的代币。资金会自动桥接，
并兑换为你在目标链上选择的代币。

## 工作原理

1. 指定目标链、代币和接收地址
2. 接收各目标链对应的跨链桥地址（EVM、Solana、Bitcoin）
3. 从 Polymarket 钱包向相应的跨链桥地址发送 pUSD
4. 资金会自动桥接并兑换为所需代币
5. 资金到达目标钱包

<Warning>
  不要预先生成提款地址。仅在准备执行提款时生成地址。每个地址都针对
  特定目标进行配置。
</Warning>

<Warning>
  提款时，pUSD 会通过 Collateral Offramp 解封装为 USDC，并通过 [Uniswap v3
  池](https://polygonscan.com/address/0xd36ec33c8bed5a9f7b6630855f1533455b98a418)
  兑换为 USDC（原生）。UI 会强制要求输出金额差异小于 10bp。有时该池的
  流动性可能耗尽。如果提款遇到问题，请尝试拆分为较小金额，或等待池完成
  再平衡。也可以直接提取 pUSD，这不需要 Uniswap 流动性，但请注意，部分
  交易所已不再直接接受 pUSD 存款。
</Warning>

<Tip>
  对于超大额提款（超过 \$50,000），可考虑拆分为较小金额，或使用第三方
  跨链桥以尽量降低滑点。
</Tip>

## 创建提款地址

生成根据提款目标配置的跨链桥地址。有关完整的请求和响应模式，请参阅
[创建提款地址](/api-reference/bridge/create-withdrawal-addresses)。

<Tip>
  **构建者：附加你的代码。** 如果你通过此端点路由用户资金，请通过可选的
  `X-Builder-Code` 标头传递构建者代码（bytes32 十六进制；`0x` + 64 个
  十六进制字符）。这让跨链桥提供商能够将流量归因到你的应用，从而追踪并
  优先处理卡住或延迟的转账。该标头为可选项。未提供时请求仍会成功，但会返回
  `missing_builder_code` 警告；格式错误的代码会返回 `400`。请前往 [Settings →
  Builder](https://polymarket.com/settings?tab=builder) 获取代码。
</Tip>

向跨链桥提供资金的目标链、目标代币和接收钱包，它会为每种地址类型返回
一个地址。

```bash theme={null}
curl -X POST https://bridge.polymarket.com/withdraw \
  -H "Content-Type: application/json" \
  -H "X-Builder-Code: <builder_code>" \
  -d '{
    "address": "0x9156dd10bea4c8d7e2d591b633d1694b1d764756",
    "toChainId": "1",
    "toTokenAddress": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "recipientAddr": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
  }'
```

```json theme={null}
{
  "address": {
    "evm": "0x23566f8b2E82aDfCf01846E54899d110e97AC053",
    "svm": "CrvTBvzryYxBHbWu2TiQpcqD5M7Le7iBKzVmEj3f36Jb",
    "btc": "bc1q8eau83qffxcj8ht4hsjdza3lha9r3egfqysj3g"
  },
  "note": "Send funds to these addresses to bridge to your destination chain and token."
}
```

你的 Polymarket 钱包位于 Polygon，因此始终应将 pUSD 发送到 `evm` 地址。
响应还会包含其他链系的跨链桥地址，因为提款和存款使用相同的响应格式。
提款时请忽略这些地址。

### 地址类型

| 地址     | 用途                                        |
| ------ | ----------------------------------------- |
| `evm`  | Ethereum、Arbitrum、Base、Optimism 和其他 EVM 链 |
| `svm`  | Solana                                    |
| `btc`  | Bitcoin                                   |
| `tron` | Tron                                      |

提款**即时到账**且**免费**——Polymarket 不收取提款费用。

## 提款流程

<Steps>
  <Step title="检查受支持资产">
    通过 `/supported-assets` 验证目标链和代币是否受支持。
  </Step>

  <Step title="获取报价">通过 `POST /quote` 预览费用和预计输出。</Step>

  <Step title="创建提款地址">
    使用钱包地址、目标链、代币和接收方调用 `POST /withdraw`。
  </Step>

  <Step title="发送 pUSD">从 Polymarket 钱包向相应的跨链桥地址转入 pUSD。</Step>
  <Step title="跟踪状态">使用 `/status/{address}` 监控进度。</Step>
</Steps>

## 后续步骤

<CardGroup cols={2}>
  <Card title="获取报价" icon="calculator" href="/cn/trading/bridge/quote">
    提款前预览费用和预计输出。
  </Card>

  <Card title="检查状态" icon="clock" href="/cn/trading/bridge/status">
    跟踪提款进度。
  </Card>
</CardGroup>
