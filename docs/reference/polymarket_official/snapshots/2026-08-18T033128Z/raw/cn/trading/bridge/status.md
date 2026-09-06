> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 交易状态

> 跟踪跨链桥存款和提款直至完成

将资产发送到跨链桥地址后，使用状态端点跟踪转账进度，直到资金到账。存款和提款使用同一个请求：始终查询接收资金的跨链桥地址，而不是两端的钱包地址。

## 检查状态

查询某个跨链桥地址的交易。

```bash theme={null}
curl --get https://bridge.polymarket.com/status/0x23566f8b2E82aDfCf01846E54899d110e97AC053 \
  --data-urlencode 'limit=50'
```

<Note>
  请使用 `/deposit` 或 `/withdraw` 响应中的跨链桥地址（EVM、SVM、Tron 或
  BTC），而不是你的 Polymarket 钱包地址。
</Note>

```json theme={null}
{
  "transactions": [
    {
      "fromChainId": "1",
      "fromTokenAddress": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
      "fromAmountBaseUnit": "1000000000",
      "toChainId": "137",
      "toTokenAddress": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
      "status": "COMPLETED",
      "txHash": "0xabc123…",
      "createdTimeMs": 1697875200000
    }
  ],
  "nextCursor": "eyJsYXN0SWQiOiI0MiJ9"
}
```

| 字段                   | 描述                         |
| -------------------- | -------------------------- |
| `transactions`       | 一页交易，按时间从新到旧排列             |
| `nextCursor`         | 下一页的游标；没有更早的数据时为 `null`    |
| `fromChainId`        | 源链 ID                      |
| `fromTokenAddress`   | 发送的代币                      |
| `fromAmountBaseUnit` | 以最小单位表示的金额                 |
| `toChainId`          | 目标链 ID（存款时 Polygon 为 137）  |
| `toTokenAddress`     | 接收的代币                      |
| `status`             | 当前状态（见下表）                  |
| `txHash`             | 目标交易哈希（仅在 `COMPLETED` 时提供） |
| `createdTimeMs`      | Unix 毫秒时间戳（仅在交易开始处理后提供）    |

如果该地址尚未检测到任何转账，`transactions` 会返回空数组。

### 查询参数

交易按时间从新到旧返回，因此不带游标的请求始终返回最近的活动。要跟踪刚发起的转账，只看第一页即可。

| 参数         | 类型  | 默认值  | 描述                                          |
| ---------- | --- | ---- | ------------------------------------------- |
| `limit`    | 整数  | `50` | 每页返回的交易数量，取值范围为 `1` 到 `100`                 |
| `cursor`   | 字符串 | 无    | 上一次响应中的 `nextCursor` 延续令牌。请求第一页时省略该参数。      |
| `paginate` | 字符串 | 无    | 为现有集成保留的兼容参数。可传 `paginate=true` 或省略；分页均会生效。 |

要继续读取更早的记录，请原样回传游标，并重复请求直到 `nextCursor` 为 `null`：

```bash theme={null}
curl --get https://bridge.polymarket.com/status/0x23566f8b2E82aDfCf01846E54899d110e97AC053 \
  --data-urlencode 'limit=100' \
  --data-urlencode 'cursor=eyJsYXN0SWQiOiI0MiJ9'
```

游标是不透明的。请勿解码、修改或自行构造游标，也不要把某个地址的游标用于另一个地址；同时务必进行 URL 编码，因为游标可能包含 `+`、`/` 或 `=`。失效的游标会返回 `400 {"error": "invalid request"}`，此时请省略 `cursor` 从头开始分页。

<Warning>
  只有当 `nextCursor` 为 `null` 时才停止分页。某一页可能为空，或少于请求的
  `limit`，但后面仍可能有更多数据。
</Warning>

## 交易状态

每笔转账会依次经历以下状态：

| 状态                    | 终态 | 描述               |
| --------------------- | -- | ---------------- |
| `DEPOSIT_DETECTED`    | 否  | 已在源链检测到资金，尚未开始处理 |
| `PROCESSING`          | 否  | 正在路由和兑换交易        |
| `ORIGIN_TX_CONFIRMED` | 否  | 源链交易已确认          |
| `SUBMITTED`           | 否  | 已提交到目标链          |
| `COMPLETED`           | 是  | 资金已到账——交易成功      |
| `FAILED`              | 是  | 交易遇到错误           |

<Note>
  如果跨链桥交易失败、长时间卡住，或资金因合规检查而被暂扣，请引导用户联系
  [Bridge API
  提供商的支持团队](https://intercom.help/funxyz/en/articles/10732578-contact-us)
  解决问题。
</Note>

<Tip>
  转账通常会在几分钟内完成，但根据网络状况可能需要更长时间。请每 10–30
  秒轮询一次，直到状态变为 `COMPLETED` 或 `FAILED`。
</Tip>

## 后续步骤

<CardGroup cols={2}>
  <Card title="创建存款" icon="arrow-right-to-bracket" href="/cn/trading/bridge/deposit">
    为你的钱包生成跨链桥地址。
  </Card>

  <Card title="受支持资产" icon="coins" href="/cn/trading/bridge/supported-assets">
    查看受支持的链和最低金额。
  </Card>
</CardGroup>
