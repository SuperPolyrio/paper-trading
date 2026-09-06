> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# API

> 直接使用 Polymarket REST API 和 WebSocket 流进行构建。

Polymarket API 提供对 Polymarket 平台的编程访问。
每个 API 负责集成流程中的不同部分。

## 集成接口

<Tabs>
  <Tab title="REST APIs">
    <CardGroup cols={2}>
      <Card title="Gamma API" icon="database">
        **`https://gamma-api.polymarket.com`**

        发现事件和市场，并检索与其交互所需的元数据。
      </Card>

      <Card title="CLOB API" icon="arrows-rotate">
        **`https://clob.polymarket.com`**

        读取价格和订单簿，然后下单和管理订单。
      </Card>

      <Card title="Data API" icon="chart-line">
        **`https://data-api.polymarket.com`**

        分析持仓、活动和市场参与情况。
      </Card>

      <Card title="Relayer API" icon="bolt">
        **`https://relayer-v2.polymarket.com`**

        提交钱包交易，无需账户持有 POL 支付 Gas 费。
      </Card>
    </CardGroup>
  </Tab>

  <Tab title="WebSocket API">
    <CardGroup cols={2}>
      <Card title="CLOB 市场频道" icon="chart-line" href="/api-reference/wss/market">
        **`wss://ws-subscriptions-clob.polymarket.com/ws/market`**

        关注公开订单簿、价格和市场生命周期更新。
      </Card>

      <Card title="CLOB 用户频道" icon="key" href="/api-reference/wss/user">
        **`wss://ws-subscriptions-clob.polymarket.com/ws/user`**

        关注账户中经过身份验证的订单和交易更新。
      </Card>

      <Card title="RTDS" icon="radio" href="/cn/market-data/realtime-data">
        **`wss://ws-live-data.polymarket.com`**

        流式传输公开参考价格、评论和交易活动。
      </Card>

      <Card title="体育 WebSocket" icon="trophy" href="/api-reference/wss/sports">
        **`wss://sports-api.polymarket.com/ws`**

        关注公开的比赛状态和比分。
      </Card>
    </CardGroup>
  </Tab>
</Tabs>

## 何时使用 API

在以下情况下使用直接集成：

* 官方 SDK 不支持您的运行环境。
* 您需要精确控制请求签名、请求头、重试或传输。
* 您需要 SDK 尚未提供的端点或传输格式。

SDK 为 Polymarket 提供了统一的、强类型的接口，并处理分页、错误处理和钱包设置等常见集成问题。

## 发起公开请求

公开市场数据无需凭据即可获取。例如，从 Gamma API 列出活跃市场：

```bash theme={null}
curl -G "https://gamma-api.polymarket.com/markets" \
  --data-urlencode "closed=false" \
  --data-urlencode "limit=5"
```

## 身份验证

CLOB 身份验证包含两个层级：

| 层级 | 工作原理                            | 目的              |
| -- | ------------------------------- | --------------- |
| L1 | 钱包对 EIP-712 消息进行签名              | 建立钱包控制权并创建或派生凭据 |
| L2 | 使用 API 凭据通过 HMAC-SHA256 对请求进行签名 | 验证私有 CLOB 请求    |

提交订单时，使用 L2 对请求进行身份验证，并使用钱包签名授权订单本身。

要验证私有 CLOB 请求，请遵循以下步骤：

<Steps>
  <Step title="创建 L1 类型化数据">
    构建 `ClobAuth` 类型化数据负载。

    ```json clobAuthTypedData theme={null}
    {
      "domain": {
        "name": "ClobAuthDomain",
        "version": "1",
        "chainId": 137
      },
      "types": {
        "ClobAuth": [
          { "name": "address", "type": "address" },
          { "name": "timestamp", "type": "string" },
          { "name": "nonce", "type": "uint256" },
          { "name": "message", "type": "string" }
        ]
      },
      "primaryType": "ClobAuth",
      "message": {
        "address": "<signer_address>",
        "timestamp": "<unix_seconds>",
        "nonce": "<nonce>",
        "message": "This message attests that I control the given wallet"
      }
    }
    ```

    提供以下值：

    | 占位符                | 值                          |
    | ------------------ | -------------------------- |
    | `<signer_address>` | 由签名私钥控制的 Polygon 地址        |
    | `<unix_seconds>`   | 当前 Unix 时间戳（秒）             |
    | `<nonce>`          | 凭据 nonce；除非管理多组凭据，否则使用 `0` |
  </Step>

  <Step title="创建 CLOB L1 签名">
    使用控制 `<signer_address>` 的私钥对 `clobAuthTypedData` 进行签名。本示例使用 Viem。

    ```ts theme={null}
    import { privateKeyToAccount } from "viem/accounts";

    const signer = privateKeyToAccount("<SIGNER_PRIVATE_KEY>");
    const clobL1Signature = await signer.signTypedData(clobAuthTypedData);
    ```

    返回的 `clobL1Signature` 是该负载的 L1 签名。
  </Step>

  <Step title="创建或派生 CLOB API 凭证">
    使用步骤 1 中的签名者地址、时间戳和 nonce，结合步骤 2 中的签名来创建凭证。如果该地址和 nonce 已有凭证，则直接派生它们。

    <CodeGroup>
      ```bash Create theme={null}
      curl -X POST "https://clob.polymarket.com/auth/api-key" \
        -H "POLY_ADDRESS: <signer_address>" \
        -H "POLY_SIGNATURE: <clob_l1_signature>" \
        -H "POLY_TIMESTAMP: <unix_seconds>" \
        -H "POLY_NONCE: <nonce>"
      ```

      ```bash Derive theme={null}
      curl "https://clob.polymarket.com/auth/derive-api-key" \
        -H "POLY_ADDRESS: <signer_address>" \
        -H "POLY_SIGNATURE: <clob_l1_signature>" \
        -H "POLY_TIMESTAMP: <unix_seconds>" \
        -H "POLY_NONCE: <nonce>"
      ```
    </CodeGroup>

    响应包含用于 L2 认证的三个值：

    <CodeGroup>
      ```json Response theme={null}
      {
        "apiKey": "<clob_api_key>",
        "secret": "<clob_api_secret>",
        "passphrase": "<clob_api_passphrase>"
      }
      ```

      ```json Example theme={null}
      {
        "apiKey": "7b1e2d60-6f9a-4dd7-8f3e-21b8f94c77a2",
        "secret": "Rnl2cWN0Rk5…c1ZzR1E9PQ==",
        "passphrase": "9fH3kL7m…2qW8xP4r"
      }
      ```
    </CodeGroup>

    其中：

    * `apiKey` 是 API 凭证的唯一标识符。
    * `secret` 是用于通过 HMAC-SHA256 签署 L2 请求的 Base64 编码密钥。
    * `passphrase` 是通过 `POLY_PASSPHRASE` 请求头发送的凭证值。
  </Step>

  <Step title="创建 CLOB L2 签名">
    为需要身份验证的请求创建一个以秒为单位的新 Unix 时间戳。依次连接时间戳、大写 HTTP 方法、路由路径和与实际发送内容完全一致的序列化请求体，然后使用 API 凭证 `secret` 对结果进行签名。

    示例请求使用 `GET /data/orders`，该请求没有主体：

    ```text theme={null}
    clob_request_timestamp = <unix_seconds>
    method = "GET"
    request_path = "/data/orders"

    message = clob_request_timestamp + method + request_path
    clob_l2_signature = urlsafeBase64WithPadding(
      HMAC-SHA256(base64Decode(<clob_api_secret>), message)
    )
    ```

    对于包含请求体的请求，追加实际通过网络发送的序列化请求体。
  </Step>

  <Step title="发送经过身份验证的请求">
    使用第 4 步中的时间戳和 L2 签名，以及第 3 步中的签名者地址和 API 凭据。此请求用于获取账户的未平仓订单。

    ```bash theme={null}
    curl "https://clob.polymarket.com/data/orders" \
      -H "POLY_ADDRESS: <signer_address>" \
      -H "POLY_SIGNATURE: <clob_l2_signature>" \
      -H "POLY_TIMESTAMP: <clob_request_timestamp>" \
      -H "POLY_API_KEY: <clob_api_key>" \
      -H "POLY_PASSPHRASE: <clob_api_passphrase>"
    ```

    L2 身份验证的 CLOB 请求使用相同的五个标头：

    | 请求头               | 值                      |
    | ----------------- | ---------------------- |
    | `POLY_ADDRESS`    | Polygon 签名者地址          |
    | `POLY_SIGNATURE`  | 第 4 步中的 HMAC-SHA256 签名 |
    | `POLY_TIMESTAMP`  | 用于构建签名的 Unix 时间戳       |
    | `POLY_API_KEY`    | API 凭据的 `apiKey` 值     |
    | `POLY_PASSPHRASE` | API 凭据的 `passphrase` 值 |
  </Step>
</Steps>

## 后续步骤

<CardGroup cols={3}>
  <Card title="读取市场数据" icon="chart-line" href="/cn/market-data/overview">
    发现市场，并使用价格、订单簿和历史数据。
  </Card>

  <Card title="订阅实时更新" icon="radio" href="/cn/market-data/realtime-data">
    实时流式传输市场和账户更新。
  </Card>

  <Card title="下第一笔订单" icon="rocket" href="/cn/trading/quickstart">
    设置账户并完成您的第一笔经过身份验证的交易。
  </Card>
</CardGroup>
