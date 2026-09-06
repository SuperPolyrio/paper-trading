> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# SDK 与 API

> 选择与 Polymarket 集成的方式。

选择最适合应用的接口。使用 SDK 可获得统一、带类型的集成；需要更底层的控制时，也可以直接使用 API。

<CardGroup cols={3}>
  <Card title="TypeScript SDK" icon="https://mintcdn.com/polymarket-292d1b1b/1lJ_npwaE_MShiVL/images/icons/typescript.svg?fit=max&auto=format&n=1lJ_npwaE_MShiVL&q=85&s=151a411953a3aa74c5505188c336ae09" href="/cn/getting-started/typescript" width="40" height="40" data-path="images/icons/typescript.svg">
    使用 `@polymarket/client` 构建服务、机器人、脚本和应用。
  </Card>

  <Card title="Python SDK" icon="python" href="/cn/getting-started/python">
    使用 `polymarket-client` 构建服务、笔记本、脚本和数据工作流。
  </Card>

  <Card title="API" icon="globe" href="/cn/getting-started/api">
    直接使用 Polymarket REST API 和 WebSocket 数据流构建集成。
  </Card>
</CardGroup>

<Note>
  统一的 Rust SDK 正在开发中。目前请参阅 [Rust
  迁移指南](/cn/getting-started/migrate-from-previous-sdks#安装-rust-sdk)。
</Note>

## 选项对比

|                | TypeScript SDK    | Python SDK | API                 |
| -------------- | ----------------- | ---------- | ------------------- |
| 最适合            | Node.js 应用、机器人和服务 | 服务、脚本和笔记本  | 不受支持的运行时和底层控制       |
| 数据模型           | 带类型的模型            | 带类型的模型     | 原始 JSON             |
| Polymarket 产品面 | 一个客户端接口           | 一个客户端接口    | 各服务使用不同 API         |
| 身份验证与签名        | SDK 辅助方法          | SDK 辅助方法   | 直接实现                |
| 分页             | 统一的分页器            | 统一的分页器     | 因 API 而异            |
| 实时数据           | SDK 订阅            | SDK 订阅     | 分别连接各 WebSocket 产品面 |

## 现有集成

已经在使用较早版本的 Polymarket SDK？请按照 [SDK 迁移指南](/cn/getting-started/migrate-from-previous-sdks)更新集成。
