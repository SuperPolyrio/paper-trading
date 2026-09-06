> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 速率限制

> Polymarket API 的 Cloudflare IP 速率限制

本页列出的速率限制基于 IP 地址，并通过 Cloudflare 的限流系统执行。当你超过任何端点的限制时，请求会被限流（延迟/排队），而非立即拒绝。限制基于滑动时间窗口重置。

<Note>
  CLOB
  下单和取消请求还受到独立的[按签名者地址划分的令牌桶限制](/cn/api-reference/trading-rate-limits)。
</Note>

***

## 通用

| 端点          | 限制               |
| ----------- | ---------------- |
| 通用速率限制      | 15,000 req / 10s |
| 健康检查（`/ok`） | 100 req / 10s    |

***

## Gamma API

Base URL: `https://gamma-api.polymarket.com`

| 端点                        | 限制              |
| ------------------------- | --------------- |
| 通用                        | 4,000 req / 10s |
| `/events`                 | 500 req / 10s   |
| `/markets`                | 300 req / 10s   |
| `/markets` + `/events` 列表 | 900 req / 10s   |
| `/comments`               | 200 req / 10s   |
| `/tags`                   | 200 req / 10s   |
| `/public-search`          | 350 req / 10s   |

***

## Data API

Base URL: `https://data-api.polymarket.com`

| 端点                  | 限制              |
| ------------------- | --------------- |
| 通用                  | 1,000 req / 10s |
| `/trades`           | 200 req / 10s   |
| `/positions`        | 150 req / 10s   |
| `/closed-positions` | 150 req / 10s   |
| 健康检查（`/ok`）         | 100 req / 10s   |

***

## CLOB API

Base URL: `https://clob.polymarket.com`

### 通用

| 端点                         | 限制              |
| -------------------------- | --------------- |
| 通用                         | 9,000 req / 10s |
| `GET` balance allowance    | 200 req / 10s   |
| `UPDATE` balance allowance | 50 req / 10s    |

### 市场数据

| 端点                | 限制              |
| ----------------- | --------------- |
| `/book`           | 1,500 req / 10s |
| `/books`          | 500 req / 10s   |
| `/price`          | 1,500 req / 10s |
| `/prices`         | 500 req / 10s   |
| `/midpoint`       | 1,500 req / 10s |
| `/midpoints`      | 500 req / 10s   |
| `/prices-history` | 1,000 req / 10s |
| Market tick size  | 200 req / 10s   |

### 账本

| 端点                                               | 限制            |
| ------------------------------------------------ | ------------- |
| `/trades`, `/orders`, `/notifications`, `/order` | 900 req / 10s |
| `/data/orders`                                   | 500 req / 10s |
| `/data/trades`                                   | 500 req / 10s |
| `/notifications`                                 | 125 req / 10s |

### 身份验证

| 端点         | 限制            |
| ---------- | ------------- |
| API key 端点 | 100 req / 10s |

### 交易

Cloudflare 对交易端点同时应用**突发**限制（允许短期峰值）和**持续**限制（较长期的平均值）。

| 端点                             | 突发限制            | 持续限制                 |
| ------------------------------ | --------------- | -------------------- |
| `POST /order`                  | 5,000 req / 10s | 120,000 req / 10 min |
| `DELETE /order`                | 5,000 req / 10s | 120,000 req / 10 min |
| `POST /orders`                 | 2,000 req / 10s | 21,000 req / 10 min  |
| `DELETE /orders`               | 2,000 req / 10s | 15,000 req / 10 min  |
| `DELETE /cancel-all`           | 250 req / 10s   | 6,000 req / 10 min   |
| `DELETE /cancel-market-orders` | 1,500 req / 10s | 21,000 req / 10 min  |

***

## Bridge API

Base URL: `https://bridge.polymarket.com`

| 端点 | 限制           |
| -- | ------------ |
| 通用 | 50 req / 10s |

***

## 其他

| 端点                | 限制             |
| ----------------- | -------------- |
| Relayer `/submit` | 25 req / 1 min |
| User PNL API      | 200 req / 10s  |
