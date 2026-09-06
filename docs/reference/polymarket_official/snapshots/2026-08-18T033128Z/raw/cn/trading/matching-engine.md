> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 撮合引擎重启

> 维护窗口、重启处理以及重启后的仅挂单模式

Polymarket 撮合引擎会因维护和升级而重启。本页面介绍如何检测和处理停机、
重启后的仅挂单时段，以及从哪里提前获知变更。

***

## 公告

撮合引擎变更（计划重启、更新和维护窗口）会在发生**之前**通过以下渠道公布：

<CardGroup cols={2}>
  <Card title="Telegram" icon="telegram" href="https://t.me/polytradingapis">
    加入 Polymarket Trading APIs 频道，获取实时公告。
  </Card>

  <Card title="Discord" icon="discord" href="https://discord.com/channels/710897173927297116/1473553279421255803">
    加入 Polymarket Discord 中的 #trading-apis 频道。
  </Card>
</CardGroup>

公告通常包括**变更内容**、**计划时间**和**预计停机窗口**。在条件允许时，
目标是提前约 2 天通知。

***

## 处理 HTTP 425

在重启窗口期间，CLOB API 会对所有订单相关端点返回 **HTTP 425（Too Early）**。
这表示撮合引擎正在重启，并会很快恢复。

每次重启后，撮合引擎会进入持续 **2 分钟的仅挂单模式**。在此期间可以取消
订单，新订单必须使用 `postOnly: true`；非仅挂单订单会被拒绝。

### 推荐的重试策略

<Steps>
  <Step title="检测 425">
    收到 HTTP `425` 响应时，撮合引擎正在重启。不要将其视为永久错误。
  </Step>

  <Step title="退避并重试">
    等待后使用指数退避重试。从 1–2 秒开始，并在每次重试时增加间隔。
  </Step>

  <Step title="处理仅挂单模式">
    `425` 响应停止后，引擎已恢复在线，但仍会保持 2 分钟的仅挂单模式。
    在此期间，仅接受取消请求和带有 `postOnly: true` 的订单。
  </Step>
</Steps>

### 代码示例

当撮合引擎返回 `425` 时，重试符合条件的仅挂单限价单：

<Tabs>
  <Tab title="TypeScript">
    给定一个 `SecureClient`，在每次退避间隔后再次调用
    `placeLimitOrder()`：

    ```typescript TypeScript theme={null} theme={null}
    import { OrderSide, RequestRejectedError } from "@polymarket/client";

    async function placeWithRestartRetry() {
      const MAX_RETRIES = 10;
      let delay = 1000;

      for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
        try {
          return await client.placeLimitOrder({
            tokenId: yesTokenId,
            side: OrderSide.BUY,
            price: "0.52",
            size: "10",
            postOnly: true,
          });
        } catch (error) {
          if (!(error instanceof RequestRejectedError) || error.status !== 425) {
            throw error;
          }

          await new Promise((r) => setTimeout(r, delay));
          delay = Math.min(delay * 2, 30000);
        }
      }

      throw new Error("Engine restart exceeded maximum retry attempts");
    }

    const response = await placeWithRestartRetry();
    ```
  </Tab>

  <Tab title="Python">
    给定一个 `AsyncSecureClient`，在每次退避间隔后再次调用
    `place_limit_order()`。同步 `SecureClient` 会抛出相同错误：

    ```python Python theme={null} theme={null}
    import asyncio

    from polymarket import RequestRejectedError


    async def place_with_restart_retry():
        max_retries = 10
        delay = 1

        for _ in range(max_retries):
            try:
                return await client.place_limit_order(
                    token_id=yes_token_id,
                    side="BUY",
                    price="0.52",
                    size="10",
                    post_only=True,
                )
            except RequestRejectedError as error:
                if error.status != 425:
                    raise

                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

        raise RuntimeError("Engine restart exceeded maximum retry attempts")


    response = await place_with_restart_retry()
    ```
  </Tab>
</Tabs>

***

## 受限交易模式

在受限交易模式下，`POST /order` 和 `POST /orders` 的下单行为会发生变化。
除非交易被完全禁用，否则取消端点仍会接受取消请求。

### 仅取消模式

在仅取消模式下，新订单会被拒绝，但仍接受取消请求。

`POST /order` 和 `POST /orders` 返回 `503`：

```json theme={null} theme={null}
{
  "error": "Trading is currently cancel-only. New orders are not accepted, but cancels are allowed."
}
```

### 仅挂单模式

每次重启后，撮合引擎会进入持续 **2 分钟的仅挂单模式**。取消请求会被接受，
新订单必须使用 `postOnly: true`。非仅挂单订单会被拒绝。

`POST /order` 返回 `503`，并在响应正文和 `Retry-After` HTTP 标头中提供
重试延迟：

```json theme={null} theme={null}
{
  "error": "post-only mode: only post-only orders and cancels are allowed",
  "code": "post_only_mode",
  "retry_after_seconds": 79
}
```

对于批次中的非仅挂单订单，`POST /orders` 会分别返回错误：

```json theme={null} theme={null}
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

收到任一种受限模式响应时，不要原样重试同一笔非仅挂单订单。请取消现有订单，
在提供延迟时等待后重试，或使用 `postOnly: true` 重新提交符合条件的挂单。

***

## 最佳实践

* **订阅公告频道** — 在重启发生前收到通知，以便提前准备
* **妥善处理 425** — 将其视为临时状态而非错误；重试逻辑应自动恢复
* **处理下单时的 503 模式响应** — 仅取消和仅挂单响应要求调整订单流程，而不是盲目重试
* **避免激进重试** — 引擎需要时间重新加载订单簿；快速连续重试不会加快恢复，反而可能在引擎恢复后触发速率限制
* **记录重启事件** — 跟踪客户端遇到 425 的时间，以便与已公告的维护窗口对应
