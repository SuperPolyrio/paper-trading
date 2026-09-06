> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 错误码

> CLOB API 错误响应完整参考

所有 CLOB API 错误都返回一个包含 `error` 字段的 JSON 对象：

```json theme={null}
{
  "error": "<message>"
}
```

***

## 全局错误

以下错误可能出现在**任何需要身份验证的端点**上。

<ResponseField name="401" type="Unauthorized">
  `Unauthorized/Invalid api key` — API key
  缺失、过期或无效。确保你发送了所有必需的[身份验证
  header](/cn/getting-started/api#身份验证)。
</ResponseField>

<ResponseField name="401" type="Unauthorized">
  `Invalid L1 Request headers` — L1 身份验证 header（HMAC
  签名）格式错误或签名不匹配。参见[身份验证](/cn/getting-started/api#身份验证)。
</ResponseField>

<ResponseField name="503" type="Service Unavailable">
  `Trading is currently disabled. Check polymarket.com for updates` —
  交易所暂时停止运行。不接受任何订单（包括取消）。
</ResponseField>

<ResponseField name="429" type="Too Many Requests">
  `Too Many Requests` —
  你已超过[速率限制](/cn/api-reference/rate-limits)。请使用指数退避策略重试。
</ResponseField>

***

## 订单簿

订单簿端点的错误。

### GET book

<ResponseField name="400" type="Bad Request">
  `Invalid token id` — `token_id` 查询参数缺失或不是有效的 token ID。
</ResponseField>

<ResponseField name="404" type="Not Found">
  `No orderbook exists for the requested token id`
</ResponseField>

### POST books

<ResponseField name="400" type="Bad Request">
  `Invalid payload` — 请求体格式错误或缺少必填字段。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Payload exceeds the limit` — 单次请求中的 token ID 数量过多，请减少批量大小。
</ResponseField>

***

## 价格查询

价格、中间价和价差端点的错误。

### GET price

<ResponseField name="400" type="Bad Request">
  `Invalid token id` — `token_id` 参数缺失或无效。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Invalid side` — `side` 参数必须为 `BUY` 或 `SELL`。
</ResponseField>

<ResponseField name="404" type="Not Found">
  `No orderbook exists for the requested token id`
</ResponseField>

### POST prices

<ResponseField name="400" type="Bad Request">
  `Invalid payload` — 请求体格式错误或缺少必填字段。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Invalid side` — `side` 字段必须为 `BUY` 或 `SELL`。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Payload exceeds the limit` — 单次请求中的 token ID 数量过多。
</ResponseField>

### GET midpoint

<ResponseField name="400" type="Bad Request">
  `Invalid token id` — `token_id` 参数缺失或无效。
</ResponseField>

<ResponseField name="404" type="Not Found">
  `No orderbook exists for the requested token id`
</ResponseField>

### POST midpoints

<ResponseField name="400" type="Bad Request">
  `Invalid payload` — 请求体格式错误或缺少必填字段。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Payload exceeds the limit` — 单次请求中的 token ID 数量过多。
</ResponseField>

### GET spread

<ResponseField name="400" type="Bad Request">
  `Invalid token id` — `token_id` 参数缺失或无效。
</ResponseField>

<ResponseField name="404" type="Not Found">
  `No orderbook exists for the requested token id`
</ResponseField>

### POST spreads

<ResponseField name="400" type="Bad Request">
  `Invalid payload` — 请求体格式错误或缺少必填字段。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Payload exceeds the limit` — 单次请求中的 token ID 数量过多。
</ResponseField>

***

## 下单

下单端点的错误。

### POST order

<ResponseField name="400" type="Bad Request">
  `Invalid order payload` — 请求体格式错误、缺少必填字段或包含无效值。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `the order owner has to be the owner of the API KEY` — 订单中的 `maker`
  地址与你的 API key 关联的地址不匹配。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `the order signer address has to be the address of the API KEY`
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `'{address}' address banned` — 该地址已被禁止交易。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `'{address}' address in closed only mode`
</ResponseField>

<ResponseField name="503" type="Service Unavailable">
  `Trading is currently cancel-only. New orders are not accepted, but cancels
      are allowed.` — 交易所处于仅取消模式。你可以取消现有订单，但不能下新单。
</ResponseField>

<ResponseField name="503" type="Service Unavailable">
  `post-only mode: only post-only orders and cancels are allowed` — 交易所处于
  post-only 模式。你可以取消订单，也可以提交设置了 `postOnly: true` 的订单；非
  post-only 订单会被拒绝。响应包含 `code: "post_only_mode"` 和
  `retry_after_seconds`，相同的重试延迟也会出现在 `Retry-After` HTTP header 中。
</ResponseField>

响应示例：

```json theme={null}
{
  "error": "post-only mode: only post-only orders and cancels are allowed",
  "code": "post_only_mode",
  "retry_after_seconds": 79
}
```

重试延迟也会通过 `Retry-After` HTTP header 返回。

### POST orders

包含 `POST /order` 的端点级错误，以及：

<ResponseField name="400" type="Bad Request">
  `Too many orders in payload: {N}, max allowed: {M}` —
  批量请求中的订单数量超过了每次请求的最大允许数量。
</ResponseField>

单个订单的错误会在 `200` 响应数组中返回，每个失败的订单都有独立的错误信息。

在 post-only 模式下，批量请求中的非 post-only 订单会返回单独的错误：

```json theme={null}
[
  {
    "errorMsg": "post-only mode: only post-only orders and cancels are allowed",
    "orderID": "",
    "takingAmount": "",
    "makingAmount": "",
    "status": "",
    "success": true
  },
  {
    "errorMsg": "post-only mode: only post-only orders and cancels are allowed",
    "orderID": "",
    "takingAmount": "",
    "makingAmount": "",
    "status": "",
    "success": true
  }
]
```

***

## 订单处理错误

当订单通过初始验证但在处理过程中失败时，会返回这些错误。它们出现在 `POST /order` 和 `POST /orders` 的响应体中。

<ResponseField name="400" type="Bad Request">
  `invalid post-only order: order crosses book` —
  Post-only（maker）订单会立即成交。请调整价格使其挂在订单簿上。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `order {id} is invalid. Price ({price}) breaks minimum tick size rule: {tick}`
  — 订单价格不符合市场的最小价格精度。使用 [`GET
      /tick-size`](/api-reference/market-data/get-tick-size) 查看有效的价格精度。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `order {id} is invalid. Size ({size}) lower than the minimum: {min}` —
  订单数量低于市场最小值。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `order {id} is invalid. Duplicated.`
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `order {id} crosses the book`
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `not enough balance / allowance` — pUSD
  余额不足或代币授权额度不够。请确保账户持有足够的
  pUSD，并按照[设置交易授权](/cn/trading/wallets-auth#设置交易授权)授予 Exchange
  合约相应额度。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `invalid expiration` — 订单过期时间已过或无效。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `order canceled in the CTF exchange contract`
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `order match delayed due to market conditions`
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `order couldn't be fully filled. FOK orders are fully filled or killed.` —
  Fill-or-Kill 订单无法被现有流动性完全填充，整个订单被拒绝。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `no orders found to match with FAK order. FAK orders are partially filled or
      killed if no match is found.` — Fill-and-Kill
  订单未找到任何匹配订单。至少需要一个匹配。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `the market is not yet ready to process new orders`
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `order timed out` — Exchange
  未能在请求截止时间前处理订单。这通常发生在同一账户并发提交大量订单时。订单在进入订单簿前已被拒绝，可以安全地重新提交。
</ResponseField>

***

## 撮合引擎错误

订单执行过程中可能出现的内部撮合引擎错误。

<ResponseField name="425" type="Too Early">
  撮合引擎正在重启。请使用指数退避策略重试。有关重启处理方式，请参阅[撮合引擎重启](/cn/trading/matching-engine)。
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `there are no matching orders`
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `FOK orders are filled or killed` — Fill-or-Kill 订单无法被完全满足。
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `the trade contains rounding issues`
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `the price of the taker's order has a discrepancy greater than allowed with
      the worst maker order`
</ResponseField>

***

## 取消订单

取消订单端点的错误。

### DELETE order

<ResponseField name="400" type="Bad Request">
  `Invalid order payload` — 请求体格式错误。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Invalid orderID` — 提供的订单 ID 格式无效。
</ResponseField>

### DELETE orders

<ResponseField name="400" type="Bad Request">
  `Invalid order payload` — 请求体格式错误。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Too many orders in payload, max allowed: {N}` — 单次取消请求中的订单 ID
  数量过多。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Invalid orderID` — 一个或多个订单 ID 无效。
</ResponseField>

### DELETE cancel-market-orders

<ResponseField name="400" type="Bad Request">
  `Invalid order payload` — 请求体格式错误或包含无效的筛选参数。
</ResponseField>

***

## 查询订单

查询订单端点的错误。

### GET order by ID

<ResponseField name="400" type="Bad Request">
  `Invalid orderID` — URL 路径中的订单 ID 无效。
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `Internal server error` — 获取订单时发生意外错误。
</ResponseField>

### GET orders

<ResponseField name="400" type="Bad Request">
  `invalid order params payload` — 查询参数格式错误或包含无效值。
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `Internal server error` — 获取订单时发生意外错误。
</ResponseField>

***

## 交易记录

### GET trades

<ResponseField name="400" type="Bad Request">
  `Invalid trade params payload` — 查询参数格式错误或包含无效值。
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `Internal server error` — 获取交易记录时发生意外错误。
</ResponseField>

### GET last-trade-price

<ResponseField name="400" type="Bad Request">
  `Invalid token id` — `token_id` 参数缺失或无效。
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `Internal server error` — 获取最近成交价时发生意外错误。
</ResponseField>

### POST last-trades-prices

<ResponseField name="400" type="Bad Request">
  `Invalid payload` — 请求体格式错误或缺少必填字段。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Payload exceeds the limit` — 单次请求中的 token ID 数量过多。
</ResponseField>

***

## 市场信息

### GET market by condition ID

<ResponseField name="400" type="Bad Request">
  `Invalid market` — condition ID 格式无效。
</ResponseField>

<ResponseField name="404" type="Not Found">
  `market not found` — 不存在该 condition ID 对应的市场。
</ResponseField>

### GET tick-size

<ResponseField name="400" type="Bad Request">
  `Invalid token id` — token ID 无效。
</ResponseField>

<ResponseField name="404" type="Not Found">
  `market not found` — 未找到该 token ID 对应的市场。
</ResponseField>

### GET neg-risk

<ResponseField name="400" type="Bad Request">
  `Invalid token id` — token ID 无效。
</ResponseField>

<ResponseField name="404" type="Not Found">
  `market not found` — 未找到该 token ID 对应的市场。
</ResponseField>

***

## 价格历史

### GET prices-history

<ResponseField name="400" type="Bad Request">
  筛选条件验证错误 —
  一个或多个查询参数（`market`、`startTs`、`endTs`、`fidelity`）无效。
</ResponseField>

### GET ohlc

<ResponseField name="400" type="Bad Request">
  `startTs is required` — 缺少 `startTs` 查询参数。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `asset_id is required` — 缺少 `asset_id` 查询参数。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `invalid fidelity: {val}` — `fidelity`
  参数必须为以下值之一：`1m`、`5m`、`15m`、`30m`、`1h`、`4h`、`1d`、`1w`。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `limit cannot exceed 1000` — 请将 `limit` 参数减少到 1000 或以下。
</ResponseField>

### GET orderbook-history

<ResponseField name="400" type="Bad Request">
  `startTs is required` — 缺少 `startTs` 查询参数。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `either market or asset_id must be provided` — 你必须指定 `market`（condition
  ID）或 `asset_id`（token ID）其中之一。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `limit cannot exceed 1000` — 请将 `limit` 参数减少到 1000 或以下。
</ResponseField>

***

## 身份验证和 API Key

### POST auth api-key

<ResponseField name="401" type="Unauthorized">
  `Invalid L1 Request headers` — L1 身份验证 header 缺失或无效。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Could not create api key`
</ResponseField>

### GET auth api-keys

<ResponseField name="500" type="Internal Server Error">
  `Could not retrieve API keys` — 获取 API key 时发生意外错误。
</ResponseField>

### DELETE auth api-key

<ResponseField name="500" type="Internal Server Error">
  `Could not delete API key` — 删除 API key 时发生意外错误。
</ResponseField>

### GET auth derive-api-key

<ResponseField name="401" type="Unauthorized">
  `Invalid L1 Request headers` — L1 身份验证 header 缺失或无效。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Could not derive api key!`
</ResponseField>

***

## Builder API Key

### POST auth builder-api-key

<ResponseField name="500" type="Internal Server Error">
  `could not create builder api key` — Builder API key 创建失败。
</ResponseField>

### GET auth builder-api-key

<ResponseField name="500" type="Internal Server Error">
  `could not get builder api keys` — 获取 Builder API key 时发生意外错误。
</ResponseField>

### DELETE auth builder-api-key

<ResponseField name="400" type="Bad Request">
  `invalid revoke builder api key body` — 请求体格式错误。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `invalid revoke builder api key headers` — 缺少必要的身份验证 header。
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `could not revoke the builder api key: {key}` — 撤销 key 时发生意外错误。
</ResponseField>

***

## Builder 交易记录

### GET builder trades

<ResponseField name="400" type="Bad Request">
  `invalid builder trade params` — 查询参数格式错误或包含无效值。
</ResponseField>

<ResponseField name="500" type="Internal Server Error">
  `could not fetch builder trades` — 获取 Builder 交易记录时发生意外错误。
</ResponseField>

***

## 余额和授权

### GET balance-allowance

<ResponseField name="400" type="Bad Request">
  `Invalid asset type` — `asset_type` 参数不是可识别的资产类型。
</ResponseField>

<ResponseField name="400" type="Bad Request">
  `Invalid signature_type` — `signature_type` 参数必须为 `EOA`、`POLY_PROXY` 或
  `GNOSIS_SAFE`。
</ResponseField>

***

## 状态码速查

| 状态码   | 含义                    | 常见原因                                                        |
| ----- | --------------------- | ----------------------------------------------------------- |
| `400` | Bad Request           | 无效参数、格式错误的请求体、业务逻辑违规                                        |
| `401` | Unauthorized          | API key 缺失或无效、HMAC 签名错误、时间戳过期                               |
| `404` | Not Found             | 市场不存在、订单未找到、token ID 无法识别                                   |
| `425` | Too Early             | 撮合引擎正在重启——请使用退避策略重试。参阅[撮合引擎重启](/cn/trading/matching-engine) |
| `429` | Too Many Requests     | 超过速率限制——请实施指数退避策略                                           |
| `500` | Internal Server Error | 服务器意外错误——请使用退避策略重试                                          |
| `503` | Service Unavailable   | 交易所暂停，或下单因仅取消 / post-only 模式被阻止                             |

<Note>
  CLOB API 内部存在状态码覆盖机制：任何包含 `"not found"` 的错误信息会返回
  `404`，包含 `"unauthorized"` 的会返回 `401`，包含 `"context canceled"`
  的会返回 `400`，无论原始状态码是什么。
</Note>
