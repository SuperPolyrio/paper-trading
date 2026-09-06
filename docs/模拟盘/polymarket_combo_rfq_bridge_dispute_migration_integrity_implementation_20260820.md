# Polymarket Combo/RFQ 与官方产品行为实施记录

日期：2026-08-20

## 范围

本阶段完成以下模拟盘功能，并保持所有外部请求位于 adapter/client wrapper：

1. 官方 Combo market catalog、Builder RFQ REST 和 Quoter WebSocket adapter。
2. 服务端 deadline 驱动的 RFQ 状态机、六位定点数、BUY notional 与 SELL shares。
3. Quote cancel、Last Look、执行状态 reconciliation 和禁止未知结果盲目重提。
4. Combo position、SELL cash-out、实现 PnL 和 collateral return 原子记账。
5. Bridge 官方 client 与用户 CLI。
6. Dispute 动态 bond、2 小时 challenge、24-48 小时 discussion 和最终链上 truth。
7. CLOB V1/USDC.e 到 V2/pUSD migration replay。
8. Market integrity surveillance、证据账本和人工调查状态机。

## Combo/RFQ

实现目录：`quant/simulator/combo/`

- `models.py`：官方 Combo market 和 RFQ payload 映射；金额使用整数 e6，拒绝超过六位小数；BUY 只能使用 notional，SELL 只能使用 shares。
- `adapters.py`：公开 Combo 分页、Builder create/accept/status、Maker quote/cancel/confirmation，以及 Combo SDK position/collateral wrapper。GET 可重试，状态变更请求不做传输层自动重试。
- `quoter_ws.py`：Quoter auth 首消息、断线指数退避重连、事件规范化和 checkpoint 去重；兼容 awaitable、async context manager 和普通 websocket 连接对象。
- `state_machine.py`：使用官方 `submission_deadline`、`expires_at`、`confirm_by`，维护 `MATCHED/MINED/RETRYING/CONFIRMED/FAILED`；超时进入 reconciliation，禁止重新提交。
- `service.py`：Builder requester 和 Quoter maker 命令都通过 `UnifiedAdmissionService`；cancel/decline 保留为风控动作；迟到或倒退的非终态状态不能覆盖较新状态。
- `store.py`：RFQ、原始 Quoter 事件、command attempt、Combo position、accounting event 和 collateral plan 持久化。`SUBMITTING` 与 `UNKNOWN` 在重启后都只能查询官方状态，不能盲目重提。

只有官方 RFQ 为 `CONFIRMED` 且 transaction hash 匹配时才能进入账户记账。BUY、SELL 和 collateral return 均在单个 PostgreSQL transaction 内完成；失败不会留下半套 Combo position 或现金变更。

## Bridge

实现：

- `quant/simulator/economics/bridge_client.py`
- `quant/simulator/economics/bridge_cli.py`

支持 `supported-assets`、`quote`、`deposit-address`、`withdrawal-address`、`status` 和 `recovery`。状态历史按 opaque cursor 遍历并去重；没有官方 transaction evidence 时 recovery 返回 `NO_OFFICIAL_EVIDENCE`，禁止增加现金。地址创建默认 dry-run，必须显式传 `--execute` 才调用官方写接口。

## Dispute、Migration 与 Integrity

- `quant/settlement/dispute_policy.py`：版本化官方规则 snapshot、动态 proposer/disputer bond、challenge/discussion 窗口、重启恢复和 UMA transaction truth。
- `quant/simulator/regime/venue_migration.py`：顺序化 migration event replay，清空 V1 book/order，验证 1:1 collateral conversion，保留 position 数量，并在 reconciliation 后完成。
- `quant/simulator/integrity/`：self-dealing、reciprocal wash、spoof/layering、front-running 候选检测；检测器只能开 case，不能自动定罪。confidential information 和 outcome influence 必须由人工证据进入调查账本。

## 数据库

迁移版本：`0026-combo-command-attempts-v1`

新增表已进入 `quant/paper/db_migration.py` 的 execution、snapshot 和 parity 清单。2026-08-20 本机 PostgreSQL schema apply 为 PASS，数据库为 `poly_data_core`，迁移 checksum：

`3bd5fd76042f0c2d0a49eb9ace4ba615c767bfeb1585f2776fbb48e8b38cad0f`

## 验收结果

1. 官方 OpenAPI/AsyncAPI contract、Combo/Bridge/Dispute/Migration/Integrity 单测：PASS。
2. 相关模拟盘宽回归：`93 passed`。
3. migration/tenant/CTF schema 回归：`28 passed`。
4. PostgreSQL Combo acceptance：PASS，覆盖 BUY、SELL cash-out、实现 PnL、失败回滚、collateral restart、WS event restart 去重、`SUBMITTING/UNKNOWN` 禁止盲重提。
5. 官方在线只读验收：PASS。通过显式进程代理读取 5 个 Combo market，验证 YES/NO position ID 和分页 cursor；Bridge 返回 229 个 supported assets。
6. 官方本地契约 SHA256：
   - Combo OpenAPI：`2fd460508ace0b906948c07d0c835a49de59acf95abaed27ac59a8bffa45f638`
   - RFQ AsyncAPI：`6f4d9a814f457a4aa81b7eb7759e6b277491a9131edb35aab6e6b9d45236523b`
   - Bridge OpenAPI：`e6ea7f3209c4b33f2e1fe8360681c0248f8a0f304c336b60b4d265fb731b6f15`

验收产物：

- `runtime_outputs/simulator/official_product_acceptance/combo-latest.json`
- `runtime_outputs/simulator/official_product_acceptance/official-products-latest.json`

## 真实权限边界

当前环境没有 Builder API key/secret/passphrase，也没有 Quoter 角色所需的 CLOB API credential 和授权身份，因此没有执行 authenticated Combo 成交、真实 Bridge 转账或真实 dispute。正式结果保持 `NOT_ATTEMPTED_NO_BUILDER_OR_QUOTER_AUTH_REQUIRED`，没有用 fixture、测试 header 或 mock transaction 冒充真实成交。

这符合阶段要求：无 Builder/Quoter 权限时完成只读与协议验收，并保留真实权限接入后的同一 adapter、Admission、command ledger 和 reconciliation 路径。
