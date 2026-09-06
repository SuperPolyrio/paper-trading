# Polymarket 模拟盘生产能力审计

> **历史快照，不是当前状态页。** 本文保留 2026-08-16 当时的部署与阻塞证据，其中的 `MISSING`、`NO_GO` 和运行数字不得用于描述当前系统。当前权威状态见 `docs/模拟盘/polymarket_retail_paper_trading_product_idea.md` 第 26 节、`runtime_outputs/simulator_closure/current/closure.{json,md}` 和 `reports/simulator_capability_audit.{json,md}`。

> 审计时间：2026-08-16 13:37 CST  
> 审计对象：`prediction-market-quant` paper execution 系统  
> 指导文档：`docs/模拟盘/polymarket_production_launch_gap_codex_guide.md`  
> 审计方式：本地代码与产物检查、只读数据库查询、GCP 实时运行检查、现有验收报告核对  
> 变更边界：PR-1 已部署；PR-2 与 PR-3 已在本机临时 PostgreSQL 完成迁移、worker、lease/takeover/fencing 演练；PR-4 的 metrics/dashboard/alerts/runbook/status/soak automation/report generator 已在本机实现并回归；PR-5 的 paper-only client、credential loader、负向测试、rotation/audit/IAM/egress/DB role 产物已在本机实现，但未应用 GCP IAM、Secret Manager、VPC 或生产 DB；未修改 LOB 采集或触发真实订单
>
> PR-8/9/10 与 Scenario/Conditional 本机增量验收：2026-08-16；新增 tenant-scoped API/SDK/UI、Replay Session/Strategy Report、admin/support/governance、Scenario、ConditionalOrderEngine、Parquet export、一次性 PostgreSQL 验收和浏览器视觉验收，未修改生产库、GCP、LOB 或真实订单。

## 2026-08-16 20:34 CST 增量：maker 热成交证据部署

本节覆盖旧快照中 GCP worker 构建和 maker trade evidence 的状态，不覆盖同区数据库与最终 soak 结论。

| 项目 | 当前证据 |
|---|---|
| GCP paper worker build | 已部署 `3dad9605d7ed9acdd481dc90a8f3ec26de327fb5400b11db38d802e6de97b9c3` |
| 核心代码一致性 | `live_shadow_service.py=051e453d...42d5f`，`live_shadow_store.py=697b6423...ef9d`，本地/GCP SHA256 一致 |
| 部署状态连续性 | 升级前后 `432 intents / 266 fills / 330 ledger keys`，missing=`0` |
| worker 运行态 | user service active，`NRestarts=0`，transport=`REDUNDANT`，queue=`0/0`，backpressure=`ACCEPT` |
| 热成交落库 | 观测从 `7` 增长到 `12` 行；`12/12` 唯一 event_id，worker 计数 `persisted=12`、duplicates=`0`、maker errors=`0`；一条延迟写入在 12 秒内由队首幂等重试自动收敛 |
| maker 只读候选 | 60 秒窗口 `READY`，6 个候选，source=`paper_live_ws_last_trade_price`，coverage=`redundant_health_window_complete`，`exchange_submit_called=false` |
| 严格窗口行为 | 180 秒窗口因部署前后的 health coverage 不连续返回 `NO_CANDIDATES/EVIDENCE_NOT_READY`，没有回退为可执行 maker 证据 |
| 回归 | maker/production-runtime/operations 定向测试 `45 passed` |

安装器同时完成三项加固：使用完整 `MANIFEST_FILES` 作为同步/备份/回滚集合；数据库相关 preflight/state verification 使用五次有限重试且最终仍 fail-closed；发布后强制重启 health 进程，避免磁盘代码已升级但 health 仍运行旧模块。

当前不能将本节标记为 production ready：GCP worker 虽持有 `paper-global` lease epoch `1`，数据库 trigger fencing 仍未启用，因此 `/health/authority` 与 `/health/ready` 正确返回 `503`；GCP 仍通过 `127.0.0.1:45434` 反向 SSH 访问本机 PostgreSQL，本次 preflight 的 DB 延迟一度达到 `26.768s`。启用全局 fencing 前还必须隔离或迁移共享 schema 中的其他本地 paper writer，并将权威 paper DB/read model 迁到 GCP 同区。

## 2026-08-16 20:54 CST 增量：PR-2 切换闭环部署

本节只上线同区数据库切换能力，不代表已创建或切换 Cloud SQL。

| 项目 | 当前证据 |
|---|---|
| active route wiring | GCP worker、health 和后续 6h/24h soak unit 均按最后覆盖顺序读取可选 `paper-db-active.env` |
| 密钥隔离 | `paper-db-target.env` 只允许非秘密连接参数；目标密码必须来自 0600 `paper-db-target-password` credential，active override 不写密码 |
| 原子切换 | 切换前同时备份旧 active route 和旧 runtime credential；成功后同时写新 route/credential；任何后续失败触发 restore |
| fail-closed rollback | 存在旧 route 但缺旧 credential 时返回 `78`，不会用错配的路由/密码重启 worker |
| installer routing | 部署 preflight、state snapshot 和 state verification 均读取可选 active route，未来切库后不会校验旧数据库 |
| installer transient handling | preflight、state snapshot、state verification 和 deploy canary 均为五次有限重试；最终失败仍自动恢复上一构建 |
| 故障实证 | 首次部署因 `/health/deep` DB timeout 失败并自动恢复 build `3dad...`；修复后第二次部署成功 |
| 当前 GCP build | `f8d53bf797f59729a1ad0f0b8e60e0e5e07e0f362657cf02bc8494270e8a1931` |
| 状态连续性 | 部署前后 `432 intents / 266 fills / 330 ledger keys`，missing=`0` |
| 当前 worker | worker/health active，transport=`REDUNDANT`，watched/ready=`153/152`，queue=`0/0`，backpressure=`ACCEPT`，last_error=`null` |
| 回归 | cutover/security/production-runtime/migration/operations 定向测试 `43 passed` |

当前仍无 `paper-db-target.env` 和 `paper-db-active.env`，现网数据库路由保持 `127.0.0.1:45434`。因此 PR-2 的代码、部署 wiring、原子切换与回滚工具已经完成并上线，但 Cloud SQL/private-IP 目标、数据迁移、正式 cutover、fencing 启用和 cutover 后 latency/soak 仍是外部基础设施阻塞，不能标记为最终 PASS。

## 1. 状态定义

| 状态 | 含义 |
|---|---|
| `IMPLEMENTED_AND_ACCEPTED` | 代码存在，已有可重复验收证据 |
| `IMPLEMENTED_AND_ACCEPTED_LOCAL` | 代码与本机验收均通过，但尚未完成生产部署或真实多用户验收 |
| `IMPLEMENTED_NOT_ACCEPTED` | 代码存在，但缺生产窗口或故障注入证据 |
| `PARTIAL` | 只覆盖了能力的一部分，或仅适用于单人研究系统 |
| `MISSING` | 未找到实现或可用证据 |
| `NOT_APPLICABLE` | 当前产品边界不需要 |

## 2. 执行结论

| 目标 | 结论 | 原因 |
|---|---|---|
| 专业内部研究/shadow | `USABLE_WITH_GATES` | 核心撮合、OMS、账本、finality、回放和风控已验收；worker 当前运行 |
| Gate A：内部研究生产 | `NO_GO` | 同区权威 DB、reverse SSH 移除、24h/7d、DB failover/restore、PR-5 生产 IAM/secret/network 落地未通过 |
| Gate B：受邀用户 Beta | `NO_GO` | PR-6..10 与 Scenario/Conditional 已在本机实现并验收；尚未部署公网生产入口、生产 identity/DB role/WAF，也未完成真实多用户 UAT |
| Gate C：商业 GA | `NO_GO` | 无多区 authority failover、跨区 DR、正式 SLO/on-call/安全与合规验收 |

本次审计的最重要纠正：

```text
之前使用 system-level `systemctl` 得到 unit not found，是查询 scope 错误。
实际 worker 使用 jhuaiyu3 的 user-level systemd：
    systemctl --user is-enabled = enabled
    systemctl --user is-active  = active
    user linger                 = yes
```

因此 `GCP worker 完全不存在` 不再是准确结论。准确状态是：

```text
代码、user unit、watchdog、独立安装器、build manifest、HTTP health 和部署 canary 都存在；
当前 VM 已通过 drain/restart/state continuity 验收；
但空白 VM、整机 reboot 和故意失败后的自动回滚演练仍未完成。
```

## 3. 实时证据快照

### 3.1 代码与部署产物

| 项目 | 实测 |
|---|---|
| 本地 Git HEAD | `8ad77fb82e5ccbd6b521965bfe608210dcd6b84d` on `main` |
| 工作树 | `modified=1, deleted=22, untracked=22` |
| 核心 paper 代码 Git 状态 | `quant/`、GCP run script 和 paper unit 均未被 Git 跟踪 |
| Terraform | `0` 个 `.tf/.tfvars` |
| Docker/Compose | `0` 个 Dockerfile/compose 产物 |
| systemd 产物 | `151` 个 service/timer/path，其中包含 paper worker/watchdog/soak |
| 权威 DB migration | `quant/paper/db_migration.py`，显式版本/checksum、COPY snapshot、catalog sync、逐表 canonical SHA parity |
| 本地与 GCP `live_shadow_service.py` | SHA256 一致：`77ec852d...a0d34` |
| 本地与 GCP run script | SHA256 一致：`089497dd...49ac` |
| 本地与 GCP user unit | SHA256 一致：`2fe86bc9...16a0` |
| 独立部署命令 | `scripts/install_gcp_paper_worker.sh` |
| build manifest | `paper_worker_build_manifest_v1`，build ID `3cc86bf9...e8f50` |
| health unit | `poly-quant-gcp-paper-health.service`，user unit enabled+active |

安装器现在会保留部署前 tar backup、校验文件 SHA256，并在失败时恢复旧文件和旧 unit。当前核心文件仍未被 Git 跟踪，因此 manifest 中的 Git SHA 只能描述外围仓库状态，尚不是完整不可变源码标识。

### 3.2 GCP worker 运行态

| 指标 | 2026-08-06 20:02 CST 实测 |
|---|---|
| paper user unit | `loaded / enabled / active / running` |
| health user unit | `loaded / enabled / active / running` |
| 启动时间 | `2026-08-06 12:01:53 UTC` |
| MainPID | `1811958` |
| NRestarts | `0` |
| watchdog timer | `enabled / active` |
| user linger | `yes` |
| transport | `REDUNDANT` |
| watched / ready | `291 / 288` |
| queue | `queued=0, processing=0` |
| backpressure | `ACCEPT` |
| latest error | `null` |
| `/health/live` | `200 PASS`，status age `1.154s` |
| `/health/ready` | `200 PASS` |
| `/health/deep` | `200 PASS`，manifest/socket/DB 均通过 |
| `/health/authority` | `503 FAIL`，明确标记 `unfenced_singleton` |

结论：PR-1 运行健康为 GREEN，但生产 authority 明确不通过；该 503 是正确的 fail-closed 行为，需要 PR-3 lease/fencing 才能消除。

### 3.3 数据库 hot path

| 路径 | 实测 |
|---|---|
| 本机 paper DB | `127.0.0.1:45432` |
| GCP paper DB 入口 | `127.0.0.1:45434` |
| 连接方式 | 本机 SSH `-R 127.0.0.1:45434:127.0.0.1:45432` |
| 本机连接+查询 | `11.917ms` |
| GCP 经 tunnel 的 5 次简单计数查询 | `876.054–1002.300ms` |
| 最终 authority canary | `50.525077s`, 报告标记 `NEEDS_COLOCATED_PAPER_DB` |

结论：同区权威数据库仍是硬 P0。当前 tunnel 可以用于 shadow，不能作为生产 execution hot path。

### 3.5 PR-2 本地集成验收

| 项目 | 结果 |
|---|---|
| PostgreSQL 目标 | 空白 `postgres:16` 临时实例，从零建库 |
| schema migration | `PASS`，`0002-paper-execution-catalog-authority-v1` |
| full snapshot | `PASS`，`1,114,971` 行，`33.257s` |
| execution catalog | `PASS`，`8,464` 个执行/持仓相关 token；包含 condition peers、neg-risk event、风险与结算元数据 |
| data-plane 隔离 | `PASS`；目标库不存在 registry、CLOB coverage 和 `core.markets`，worker hot path 不再依赖这些表 |
| core parity | `PASS`，失败 `0`，`0.947s` |
| ledger | `327/327`，SHA256 `0eee2f66...6885a0` |
| journal | `126/126`，SHA256 `503fd219...1ecda` |
| paper worker E2E | `PASS`；双路 mock L2 Grade A，BUY `5@0.60`、SELL `5@0.40`，realized PnL `-1.00` |
| 重启/幂等 | `PASS`；重启前后 `2 fills / 2 ledger / 8 journal / 0 active reservation`，重复 client ID 返回同一 intent |
| 源库隔离 | `PASS`；测试 strategy 在源库保持 `0` 行，未提交真实订单 |
| rollback test | `PASS`，恢复旧 active DB override 并重启 worker/health |
| 本地目标 latency | 10 次新连接+查询，p50 `12.748ms`，p95 `13.902ms` |

运行中 full parity 仅有 `position_marks / NAV current / NAV snapshots / current_books / venue_shadow_runs` 五张滚动读模型变化；core authority 仍全部一致。正式 cutover 会先停 worker，再执行最终 full sync/full parity。

当前不能生产切换：GCP 上 `paper-db-target.env` 与 `paper-db-active.env` 均不存在，VM 服务账号调用 Cloud SQL API 返回 `insufficient authentication scopes`；worker 实际仍使用 `127.0.0.1:45434`。因此“50.5s 消失”和“reverse SSH 不在 hot path”尚未验收。

### 3.6 PR-3 本地 authority/fencing 验收

报告：`runtime_outputs/production/paper-db-local-e2e-20260807T023346Z/acceptance.json`

| 项目 | 结果 |
|---|---|
| schema migration | `PASS`，`0003-paper-execution-authority-fencing-v1` |
| full snapshot / catalog | `PASS`，`1,117,755` 行 / `8,818` token |
| core parity | `PASS` |
| partition lease | `PASS`，首个 owner 获得 epoch `1` |
| 并发 owner | `PASS`，有效 lease 存在时 contender 被拒绝 |
| clean takeover | `PASS`，重启 owner 获得 epoch `2` |
| stale writer | `PASS`，旧 epoch `1` 对 order intent 和 ledger account 的 UPDATE 均被 PostgreSQL trigger 以 `stale or expired lease epoch` 拒绝 |
| crash/expiry failover | `PASS`，lease 到期后新 owner 获得下一 epoch，旧 controller heartbeat fail-closed |
| worker fail-closed | `PASS`，worker 未持有 lease 时 fenced authority 写入无法通过 DB |
| BUY / SELL / ledger | `PASS`，BUY/SELL 均 FILLED，重启前后 row count、现金、仓位和 realized PnL 不变 |
| idempotency | `PASS`，重复 `client_order_id` 返回同一 intent `520` |
| source isolation | `PASS`，源数据库 strategy rows 仍为 `0` |
| execution tests | `118 passed` |

PR-3 当前是“本机实现并验收”，不是“GCP 生产启用”。GCP 现有 worker 仍运行旧构建，`/health/authority` 的历史 `503` 只有在同区 DB 完成并部署新 worker 后才能消除。

### 3.7 PR-4 本机 SLO / Operations 实现与验收

| 项目 | 结果 |
|---|---|
| SLI/SLO 快照 | `PASS` 实现；覆盖 book/queue/分阶段 execution latency/unsafe/unknown/ledger/reservation/journal/DB/lease/version |
| error budget | `PASS` 实现；输出 availability、error rate、burn rate 和 remaining budget |
| 自动降级 | `PASS` 单测；GREEN 放行，YELLOW 仅小额 FOK/FAK taker，ORANGE/RED 拒绝新 intent，过期状态 fail-closed |
| 状态端点 | `PASS` 单测；`/health/slo`、`/status`、`/metrics` 与 live/ready/deep/authority 分离 |
| metrics/dashboard/alerts | `PASS` 静态验证；Prometheus exposition、Grafana JSON、11 条 alert rule、FIRING/RESOLVED 追加审计 |
| runbook | `PASS` 实现；覆盖 feed、DB、lease、journal、unknown terminal、unsafe fill、backlog、version、soak promotion |
| soak automation | `PASS` 实现；6h 通过后启 24h，24h 且 longitudinal SLO PASS 后启 7d，失败不自动重跑 |
| report generator | `PASS` 单测；每次 soak 刷新 `latest.json`、`metrics.json`、`report.md` |
| 回归 | `128` 个 execution tests + `34` 个 acceptance/live-shadow/SLO tests 全部 PASS |
| 确定性故障注入 | `11/11 PASS`；明确标记 `DETERMINISTIC_FAULT_INJECTION_NOT_PRODUCTION_SOAK`，真实下单 `false` |
| 当前本机真实快照 | 产物链 `PASS`，运行判定 `RED/FAIL_CLOSED`；DB 可达，但 worker status 停留在 2026-07-29 且无 lease/fencing |

当前产物：`runtime_outputs/paper_operations/pr4-local-current/`。该 RED 不是 PR-4 SQL/报告失败，而是旧本机 worker 证据已过期；不得冒充新架构 24h/7d PASS。

### 3.8 权威数据库快照

只读查询确认主要交易表已存在：

| 表 | 行数 |
|---|---:|
| `quant.paper_accounts` | 5,492 |
| `quant.paper_live_order_intents` | 427 |
| `quant.paper_order_events` | 1,549 |
| `quant.paper_fills` | 263 |
| `quant.paper_ledger_entries` | 327 |
| `quant.paper_positions` | 221 |
| `quant.paper_sim_events` | 645 |
| `quant.paper_live_shadow_health` | 211 |
| `quant.paper_inflight_commands` | 10 |
| `quant.execution_tca` | 10 |
| `quant.simulator_fill_finality_events` | 12 |
| `quant.simulator_oms_orders` | 6 |
| `quant.simulator_liquidity_levels` | 6 |
| `quant.venue_regime_snapshots` | 1 |

在 `quant` schema 中没有找到 `tenant_id/user_id/owner_user_id/lease_owner/fencing_token/authority_epoch` 等生产多租户或 authority fencing 列。

### 3.9 PR-5 本机 Paper / Live 安全隔离

| 项目 | 状态 | 证据 |
|---|---|---|
| paper CLOB capability | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | 独立 `PolymarketPaperClobClient` 仅暴露 book/market/fee read；无 create/post/submit/cancel order 方法 |
| live key startup guard | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | fake private key 注入时在 DB/网络初始化前拒绝，异常和 audit 均不含值 |
| dotenv 隔离 | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | paper 模块默认设置 `POLY_QUANT_DISABLE_DOTENV=1`，不读取仓库 `.env` |
| systemd credential | `IMPLEMENTED_NOT_ACCEPTED` | GCP worker/health 已部署 `paper-runtime.env` + `LoadCredential=paper-db-password`，并支持无秘密 active route override；目标 DB credential/cutover 尚未执行 |
| 独立 DB role | `IMPLEMENTED_NOT_ACCEPTED` | 最小权限 role/verification SQL 已提供；尚未应用生产 DB |
| separate service accounts/secrets | `IMPLEMENTED_NOT_ACCEPTED` | 幂等 dry-run GCP 脚本已提供；尚未创建/绑定生产 identity 与 Secret Manager ACL |
| egress policy | `IMPLEMENTED_NOT_ACCEPTED` | paper-SA 定向 allow DB/DNS/HTTPS + deny-all 脚本已提供；尚未应用 VPC |
| secret rotation | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | checksum 变化、0600 credential、无值输出的 deterministic drill PASS；真实 Secret Manager + DB rotation 待执行 |
| audit log | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | 0600 append-only hash chain PASS，篡改检测 PASS |
| 回归 | `PASS` | 项目 conda 环境 `139 passed`；security acceptance `8/8 PASS`；未联网、未读 live secret、未提交订单 |

PR-5 准确状态是 `IMPLEMENTED_NOT_ACCEPTED`。代码边界与本地负向证据已完成，但 production service account、Secret Manager、DB role、VPC 和真实 rotation audit 仍属于外部部署验收。

### 3.10 PR-6..10 与研究产品层本机验收

| 项目 | 结果 |
|---|---|
| tenant/account/RBAC/RLS | `PASS_LOCAL`；tenant-owned 表启用 FORCE RLS，跨租户读取负测试通过 |
| public API / OpenAPI / SDK | `PASS_LOCAL`；bearer auth、幂等、错误分类、分页、request ID、Python/TypeScript SDK 与 checked-in OpenAPI 一致 |
| Account Manager / Audit UI | `PASS_LOCAL`；账户、订单、持仓、成交、账本、journal、NAV/TCA、订单证据与桌面/移动端浏览器测试通过 |
| Replay / Strategy Report | `PASS_LOCAL`；冻结输入、pause/resume/fork、deterministic replay，以及 PnL、Sharpe/Sortino、drawdown、turnover、fill/capacity/fee/latency、market/event/category attribution |
| admin/support | `PASS_LOCAL`；tenant/account freeze、kill、reconcile、DLQ replay、maintenance、incident notes 与审计链 |
| data governance | `PASS_LOCAL`；retention/legal hold、tenant-scoped CSV/JSONL/ZSTD Parquet export、SHA256、signed evidence ZIP（12 个 Parquet）与跨租户下载拒绝 |
| Scenario product | `PASS_LOCAL`；resolution payout、price/fee/latency/depth/closure/outage/dispute 输入，结果明确标为 `SCENARIO_NOT_EXECUTION` 且不写 paper ledger |
| ConditionalOrderEngine | `PASS_LOCAL`；stop/stop-limit/take-profit/trailing/time/signal/OCO/OTO/bracket schema 与服务，stale/gap fail-closed，normal paper child intent，worker recovery 与 OCO single-winner |
| model disclosure | `PASS_LOCAL`；集中限制对话框与逐订单 fidelity、confidence、calibration domain、data quality、capacity、model version 展示 |
| PostgreSQL acceptance | `PASS`；migration `0009-paper-scenario-conditional-snapshot-v1`，Scenario/conditional 12 项关键行为通过，`network_calls=0`、`live_orders_submitted=false` |
| browser acceptance | `PASS`；5/5 场景通过，覆盖 account/audit、replay、admin、research 与 mobile viewport |

本节只证明本机产品代码和一次性 PostgreSQL 行为。它不证明公网部署、生产身份隔离、真实多用户负载、24h/7d 稳定性或模型 holdout 样本量。

## 4. 生产能力矩阵

| 能力 | 状态 | 证据 | 主要缺口 |
|---|---|---|---|
| 核心 taker/ledger/finality/risk/replay | `IMPLEMENTED_AND_ACCEPTED` | `reports/simulator_capability_audit.{json,md}`：22 项中 18 项 accepted | 模型 holdout 样本不足不等于内核缺失 |
| GCP paper worker unit | `IMPLEMENTED_AND_ACCEPTED` | user unit enabled+active，watchdog active，linger=yes，本地/GCP 哈希一致 | 仅证明当前 VM，不等于新 VM 可重复安装 |
| 可重复、不可变部署 | `IMPLEMENTED_NOT_ACCEPTED` | 独立 install command、部署前 backup、文件 hash、user units 均已在当前 VM 验证 | 核心文件未进 Git；空白 VM 尚未验收 |
| 启动校验和 graceful shutdown | `IMPLEMENTED_AND_ACCEPTED` | preflight PASS_WITH_WARNINGS；SIGTERM 五阶段关闭；重启前后 427 intents/263 fills/327 ledger keys 无丢失 | 整机 reboot 尚未执行 |
| health/readiness/deep/authority | `PARTIAL` | 本地 authority health 已校验 epoch/expiry/enforcement；GCP live/ready/deep/version 为 200，旧 authority 仍为 503 | PR-3 尚未部署到 GCP |
| build manifest 和版本可追溯 | `PARTIAL` | manifest 绑定 Git SHA、dirty flag、Python/schema 版本及 6 个关键文件 SHA256 | 核心文件未进 Git，Git SHA 尚不能完整重建该 build |
| 自动 canary/回滚 | `IMPLEMENTED_AND_ACCEPTED` | 成功路径 canary `PASS_WITH_BLOCKERS`；`after_preflight` 故障注入后旧 build `3cc86bf9...`、deep health 和 427/263/327 状态全部恢复 | 仍无 1%/10%/50% 分阶段 rollout |
| 同区权威 PostgreSQL | `MISSING` | GCP 实际使用 `127.0.0.1:45434` reverse tunnel | 需 Cloud SQL HA 或同区受管 PostgreSQL |
| 双写/cutover/rollback verifier | `MISSING` | 未找到 paper DB dual-write/cutover 产物 | 需事务表全量+增量校验、journal rebuild、回滚窗口 |
| 应用 migration 管理 | `PARTIAL` | `0011-paper-authority-dr-v1` 已应用；一次性 PostgreSQL 会先应用 schema 再恢复 91 张表，migration checksum 校验 PASS | 已有版本记录和 forward restore proof；仍无完整 down migration、staging lock test |
| worker lease/authority/fencing | `IMPLEMENTED_AND_ACCEPTED` | 本地 DB lease、单调 epoch、trigger fencing、contender rejection、takeover 与 stale writer fault injection 全部 PASS | 尚未部署到同区生产 DB/GCP worker |
| 多实例 HA/failover | `IMPLEMENTED_NOT_ACCEPTED` | 本地 active/passive contender 和 clean takeover PASS，旧 epoch 被 DB 拒绝 | 仍需同区两实例 crash/lease-expiry 故障演练 |
| SLI/SLO 和自动降级 | `IMPLEMENTED_NOT_ACCEPTED` | `quant/paper/operations.py`、Prometheus/Grafana、alerts、runbook、status endpoints、error budget 与 worker admission 均已实现，本机定向回归 PASS | 尚未在同区权威 DB + fenced worker 上完成最终 24h/7d |
| 6h soak | `IMPLEMENTED_AND_ACCEPTED` | `gcp_soak_6h_spool_v1`，持续 6.000h，报告 PASS | 有部分 SLO 字段当时为 `NOT_ENOUGH_DATA` |
| 24h soak | `IMPLEMENTED_NOT_ACCEPTED` | `gcp_soak_24h_execution_v2` 持续 24.000h；post-hoc observer/data-plane 分类为 `PASS`，8/8 observer gap 均有双路消息进展，unexplained data-plane gap=0；新 gate 已区分 execution-ready 与历史持仓/watch target | 原始结果仍为 `FAIL`；旧 worker 未发布 execution-specific health counters，修复后的最终 24h 尚未开始 |
| 7d soak | `IMPLEMENTED_NOT_ACCEPTED` | 7d unit、独立 resume state、24h PASS promotion gate 已实现 | 新架构上尚未实际跑满 7d |
| DB backup/failover/restore | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | `runtime_outputs/production/db-restore/restore-drill-20260818T012321Z/report.json`：91 表、2,059,135 行、107,767,451 bytes 压缩包逐表 SHA PASS；隔离恢复 RTO 111.803s；journal/fill/finality/lease gate PASS | 只证明本机 logical snapshot restore；`pitr_validated=false`，regional failover、WAL PITR、跨区备份和季度持续演练仍待生产验收 |
| paper/live 凭证隔离 | `PARTIAL` | GCP paper env key 清单未见 Polymarket private key；真实 probe 有独立配置 | paper unit 仍加载包含大量其他 API secret 的通用 `polydata.env`，无独立 service account/Secret Manager/egress policy |
| secret 管理 | `PARTIAL` | env 文件权限主要为 `0600` | 未见 Secret Manager、rotation/access audit/key drill；`gcp-l2.env` 为 `0664` |
| 账户与策略子账本 | `IMPLEMENTED_AND_ACCEPTED` | paper accounts/positions/ledger/OMS attribution 表和验收存在 | 属于单系统账户，不包含用户所有权 |
| tenant/user/auth/RBAC/RLS | `IMPLEMENTED_NOT_PRODUCTION_ACCEPTED` | PR-6 tenant/user/account、RBAC、16 张 FORCE RLS 表及跨租户 PostgreSQL 负测试 PASS | 尚未在生产 DB 和公开入口激活 |
| 用户配额和 noisy-neighbor 隔离 | `IMPLEMENTED_NOT_PRODUCTION_ACCEPTED` | user intents、account open orders、watchlist、replay、API、DB query、archive export quota 已实现并做幂等验收 | 仍需生产负载和 abuse SLO 验收 |
| 公共 `/v1` paper API | `IMPLEMENTED_NOT_PRODUCTION_ACCEPTED` | 独立 paper-only Flask 服务具备 bearer auth、幂等租约、request ID、pagination、error taxonomy、rate-limit headers；真实一次性 PostgreSQL 验收 PASS | 尚未部署到公网、WAF 或独立生产 API DB role |
| OpenAPI/SDK/deprecation policy | `IMPLEMENTED_NOT_PRODUCTION_ACCEPTED` | checked-in OpenAPI 3.1、Python/TypeScript SDK、90 天弃用规则、changelog 和契约测试已实现 | SDK 尚未发布到包仓库，公网兼容性未验收 |
| Account Manager / Order Audit UI | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | `/paper.html` 已覆盖 accounts/orders/positions/fills/ledger/journal/NAV/TCA/export；订单详情展示 lifecycle、checkpoint、quality 和 TCA；PostgreSQL 与桌面/移动端验收 PASS | 尚未部署到公开生产入口或完成真实多用户 UAT |
| fidelity 和模拟限制展示 | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | 集中 model limits disclosure；order audit UI 展开 fidelity、confidence、calibration domain、data quality、capacity、model version | 尚未公网部署和真实用户可理解性 UAT |
| CSV/JSONL/Parquet 用户导出 | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | tenant-scoped orders/positions/fills/ledger/journal/NAV/TCA 支持 CSV/JSONL/ZSTD Parquet，带行数、SHA256 和 export-byte quota；signed evidence ZIP 含 12 个 Parquet 并通过 HMAC/SHA256 验证 | 尚未公网部署和大租户 export load 验收 |
| replay session / strategy report 产品 | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | tenant-scoped session、冻结 data/config hash、pause/resume/fork、deterministic artifact、完整 performance/risk/data-quality/attribution/benchmark report、OpenAPI/SDK/UI 与 PostgreSQL/browser 验收 PASS | 当前 source mode 是已记录 paper lifecycle，不是历史 L2 rematch；尚未公网部署和真实多用户 UAT |
| scenario 产品 | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | 用户级 API/OpenAPI/Python+TypeScript SDK/UI；resolution/fee/latency/depth/closure/outage/dispute 场景，deterministic hash 且明确不写 ledger；PostgreSQL 验收 PASS | 无 endogenous market reaction；该限制已在结果和 UI 披露 |
| conditional/advanced order product | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | 独立 ConditionalOrderEngine、状态/事件表和 trusted worker；stop/stop-limit/take-profit/trailing/time/signal/OCO/OTO/bracket，gap fail-closed、child 走 normal paper intent、OCO single-winner 与重启恢复 PostgreSQL 验收 PASS | 尚未部署常驻 worker 和完成生产负载 UAT |
| admin/support/operations console | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | admin dashboard、tenant/account freeze、kill、reconciliation、jobs、DLQ replay/ignore、maintenance、incident notes、support evidence bundle；PostgreSQL/browser 验收 PASS | 尚未公网部署、接入 on-call 身份与真实工单流程 |
| 数据治理与保留 | `IMPLEMENTED_AND_ACCEPTED_LOCAL` | retention policy、legal hold、tenant deletion、hash-chain audit、signed Parquet evidence bundle、SHA256/HMAC verification 与跨租户下载拒绝 | WORM/object-lock、生产备份保留与法规策略仍需生产基础设施验收 |
| taker 校准 | `PARTIAL` | 代表性 BUY/SELL/domain 验收 PASS；2026-08-16 数据库核验为 23 个 calibratable probe/15 markets | grouped holdout 仍只有 2 个样本；最近 7 天无新样本且尚无 active-model drift baseline，不得称 calibrated PnL |
| maker research | `IMPLEMENTED_AND_ACCEPTED` | queue model/offline shadow/live probe path存在，Gate 3 PASS_RESEARCH_MODE | 只能 research，不能作 calibrated maker PnL |
| maker promotion | `PARTIAL` | 2026-08-16 当前 holdout 为 4/4 完整、4 events，但均为同一 UTC 日的 NO_FILL；`reports/maker/holdout-current/` 明确列出 promotion checks；候选规划已改为优先使用 paper WS `last_trade_price` 热证据，并校验 REDUNDANT 健康窗口 | 缺多日独立样本、PARTIAL/FULL、filled-size 与 time-to-fill 观测；新热证据 worker 尚未部署并形成完整窗口，当前候选会以 `maker_trade_evidence_not_ready` fail-closed，不再把延迟 ClickHouse 的空窗口误报为 NO_FILL |
| strategy profitability | `IMPLEMENTED_NOT_ACCEPTED` | 有 PnL/NAV/TCA 和策略报告基础 | execution calibration gate 尚未通过，不能宣称策略盈利 |

## 5. Gate A 逐项判定

| Gate A 项目 | 当前判定 | 说明 |
|---|---|---|
| GCP worker 常驻部署 | `PARTIAL` | 当前 VM 独立一键部署、drain restart、autostart unit 和健康 API 已通过；空白 VM/reboot 未验收 |
| 同区数据库 | `FAIL` | 仍为本机 DB + reverse SSH |
| reverse SSH 移出 hot path | `FAIL` | GCP `45434` 明确由本机 `ssh -R` 提供 |
| contract audit PASS | `PASS` | `professional-simulator-current-v5/readiness.json` 已从 v4 FAIL 修复为 PASS |
| 24h soak PASS | `FAIL_PENDING_RERUN` | 原始报告因 31.95s health observer gap 失败；`health-gap-analysis-v1.json` 证明该窗口 8/8 gap 均为 observer 写入延迟、双路数据继续前进；预跑又发现旧 worker 将 STALE/RESOLVED 历史目标计入 readiness 分母，已修代码但尚待新 worker 验收 |
| 7d soak PASS | `FAIL` | 无 PASS 产物 |
| VM restart/failover PASS | `PARTIAL` | SIGTERM worker restart/state continuity PASS；整机故障与 authority failover 未证明 |
| DB failover/restore PASS | `FAIL` | 无 paper DB 演练 |
| journal/ledger rebuild PASS | `PASS` | 现有 restart/replay/reconciliation 证据为 PASS |
| health/readiness/canary PASS | `PARTIAL` | live/ready/deep/version 与部署 canary 已通过；authority 仍为 503 |
| paper/live credential isolation PASS | `FAIL` | 未证明最小服务账号与 Secret Manager 隔离，paper 进程加载通用 secret env |
| unsafe fill = 0 | `PASS_AT_LAST_ACCEPTANCE` | readiness SLO 证据 PASS，需在新架构 soak 重验 |
| unknown terminal = 0 | `PASS_AT_LAST_ACCEPTANCE` | readiness SLO 证据 PASS，需在新架构 soak 重验 |
| ledger mismatch = 0 | `PASS_AT_LAST_ACCEPTANCE` | readiness SLO 证据 PASS，需在新 DB 迁移后重验 |

Gate A 结论：`NO_GO`。

## 6. Gate B / Gate C 快速判定

### Gate B：受邀用户 Beta

| 能力组 | 状态 |
|---|---|
| identity/RBAC/tenant isolation | `PASS_LOCAL / DEPLOY_PENDING` |
| multiple user-owned accounts | `PASS_LOCAL / UAT_PENDING` |
| user quotas/rate limits | `PASS_LOCAL / LOAD_PENDING` |
| public API versioning/OpenAPI/SDK | `PASS_LOCAL / PUBLISH_PENDING` |
| account manager/order audit UI | `PASS_LOCAL / DEPLOY_PENDING` |
| CSV/JSONL/Parquet/signed evidence | `PASS_LOCAL / LOAD_PENDING` |
| fidelity labels/limitation disclosure | `PASS_LOCAL / UAT_PENDING` |
| replay/scenario/conditional product | `PASS_LOCAL / WORKER_DEPLOY_PENDING` |
| admin/support workflow | `PASS_LOCAL / ON_CALL_INTEGRATION_PENDING` |
| security/privacy/retention | `CODE_PASS_LOCAL / INFRA_ACCEPTANCE_PENDING` |

Gate B 结论：`NO_GO`。

### Gate C：商业 GA

multi-zone service、authority fencing、regional HA DB、cross-region DR、on-call/status page、error budget、penetration test、quarterly restore drill、billing/abuse/legal/compliance 均未形成完整证据链。

Gate C 结论：`NO_GO`。

## 7. P0 / P1 / P2 行动

### P0：Gate A 和实时安全

1. **PR-1 Deployable GCP Worker 收口（当前 VM 已完成）**
   - 已提供独立 paper install/deploy command、build manifest、config/schema preflight、health API、部署 canary 和自动回滚。
   - 已验证 SIGTERM drain、自动重启、状态连续性、故障注入自动回滚和 L2 feed 不受影响。
   - 剩余发布治理：把核心文件纳入 Git/不可变 image，并在空白 VM 验证安装和 reboot autostart。
2. **PR-2 Same-region Authoritative DB**
   - 代码已实现 migration、精简 execution catalog、shadow parity verifier、final sync、cutover、自动 rollback 和 DB latency SLI。
   - 临时 PostgreSQL 已通过 ledger/journal/cash/position/order/reservation/finality/PnL 核心哈希验收。
   - 剩余基础设施动作：建立同区 Cloud SQL HA private IP、backup/PITR/最小 IAM，配置 target env 后执行 `prepare -> shadow-verify -> cutover`。
   - 切换后验证 50.5s 消失，并确认 paper worker 不再使用 `127.0.0.1:45434`；L2 所需 DB 路径不在本 PR 中强停。
3. **PR-3 HA Lease and Fencing**
   - 建立 DB-backed lease + fencing token + authority epoch，所有状态写入验证 epoch。
   - 做 primary kill、network partition、stale worker resurrection 和双实例故障注入。
4. **PR-4 SLO and Operations**
   - 代码交付已完成：metrics/alerts/dashboard/runbook/error budget/status endpoints/自动降级/24h+7d promotion/report generator。
   - 剩余为外部验收：同区 DB 与 fenced worker 部署固定后，只重跑一次最终 24h，通过后自动跑 7d。
5. **PR-5 Paper/Live Security Isolation**
   - 本机代码已完成：paper-only CLOB client、live-key startup guard、dotenv 禁用、systemd credential、DB role、service-account/Secret Manager/egress 脚本、rotation drill 和 hash-chain audit。
   - 本地验收已通过：security `8/8`、execution `139/139`，且明确 `live_submission_performed=false`。
   - 剩余生产动作：在独立 VM identity 应用 IAM/secret/VPC/DB role，执行真实 key rotation 与 Cloud Audit Logs 核对，再重跑最终 soak。

### P1：受邀 Beta 产品层

1. tenant/user/auth/RBAC/RLS 与 paper account ownership：本机代码和 PostgreSQL 隔离验收完成，剩余部署/UAT。
2. 用户级配额、API rate limit、replay/export budget 和 noisy-neighbor 隔离：本机完成，剩余生产负载验收。
3. `/v1/paper` API、OpenAPI、SDK、idempotency、trace ID 和 error taxonomy：本机完成，剩余公网/WAF/SDK 发布。
4. Account Manager、Orders、Positions、Fills、Ledger、NAV、Order Detail、Execution Audit UI：本机 PR-8 已完成；剩余公网部署和真实多用户 UAT。
5. Replay Session、pause/resume/fork、event timeline 和完整 Strategy Report：本机 PR-9 已完成；剩余公网部署、真实多用户 UAT 和独立 historical L2 rematch 产品。
6. CSV/JSONL/ZSTD Parquet、SHA256、signed evidence bundle、retention/legal hold：本机 PR-10 已完成；剩余对象锁和生产保留验收。
7. admin console、freeze/kill account、DLQ/reconciliation、maintenance/incident/support workflow：本机 PR-10 已完成；剩余 on-call/工单集成。
8. Scenario 与 ConditionalOrderEngine：本机 API/SDK/UI、PostgreSQL 行为与浏览器验收完成；剩余常驻 worker 部署和生产负载验收。

### P2：模型与商业 GA

1. 继续 taker grouped holdout 和 maker resting-order holdout，不伪造生产样本。
2. 只在样本门禁通过后 promotion calibrated PnL。
3. 按需增加 endogenous/agent-based market impact fidelity tier。
4. multi-zone、cross-region DR、status page/on-call、billing、abuse prevention、安全与合规审计。

## 8. 推荐开发顺序

```text
PR-0 Production Capability Audit                         DONE（本报告）
  ↓
PR-1 Deployable GCP Worker                              CURRENT VM DONE / BLANK VM PENDING
  ↓
PR-2 Same-region Authoritative DB                       CODE PASS / CLOUD SQL CUTOVER PENDING
  ↓
PR-3 HA Lease and Fencing                              LOCAL PASS / DEPLOY PENDING
  ↓
PR-4 SLO and Operations                               CODE PASS / 24H+7D PENDING
  ↓
PR-5 Paper/Live Security Isolation
  ↓
PR-6..8 Tenant/API/Account UI                         LOCAL PASS / DEPLOY PENDING
  ↓
PR-9 Replay Product                                      LOCAL PASS / DEPLOY PENDING
  ↓
PR-10 Admin/Data Governance                            LOCAL PASS / DEPLOY PENDING
  ↓
Scenario + ConditionalOrderEngine                     LOCAL PASS / DEPLOY PENDING
  ↓
PR-11 Maker Promotion and Agent-based Research         EXTERNAL EVIDENCE PENDING
```

PR-1 不应重写 paper worker。最小正确做法是把已经运行的 unit/script/code 变成可版本化、可一键安装、可健康判定、可回滚的部署产物。

## 9. 证据索引

- 指导文档：`docs/模拟盘/polymarket_production_launch_gap_codex_guide.md`
- GCP worker unit：`deploy/systemd/poly-quant-gcp-paper-live-shadow.service`
- watchdog：`deploy/systemd/poly-quant-gcp-paper-live-shadow-watchdog.{service,timer}`
- worker launcher：`scripts/run_gcp_paper_live_shadow.sh`
- 独立安装器：`scripts/install_gcp_paper_worker.sh`
- production runtime：`quant/paper/production_runtime.py`
- DB migration/verifier：`quant/paper/db_migration.py`
- DB cutover/rollback：`scripts/manage_gcp_paper_db_cutover.sh`
- Cloud SQL env 模板：`deploy/systemd/paper-db-target.env.example`
- PR-2 临时库验收：`runtime_outputs/production/paper-db-pr2-integration-final-20260806T124400Z/`
- PR-2 本机 worker E2E：`runtime_outputs/production/paper-db-local-e2e-20260807T015200Z/acceptance.json`
- PR-2 可重复验收入口：`scripts/run_local_paper_db_e2e.sh`
- health unit：`deploy/systemd/poly-quant-gcp-paper-health.service`
- PR-4 operations：`quant/paper/operations.py`
- PR-4 dashboard/alerts：`deploy/grafana/paper-operations-dashboard.json`、`deploy/prometheus/paper-alert-rules.yml`
- PR-4 runbook：`docs/模拟盘/paper_operations_runbook.md`
- PR-4 本机快照：`runtime_outputs/paper_operations/pr4-local-current/`
- PR-4 故障注入：`reports/simulator_acceptance/paper-operations-pr4-local.{json,md}`
- PR-4 7d unit：`deploy/systemd/poly-quant-professional-simulator-soak-7d.service`
- PR-1 验收产物：`runtime_outputs/production/gcp-paper-worker-pr1-20260806T115823Z/`
- worker 实现：`quant/paper/live_shadow_service.py`
- paper schema/store：`quant/paper/live_shadow_store.py`
- 账本：`quant/paper/paper_ledger.py`
- 能力审计：`reports/simulator_capability_audit.{json,md}`
- 最新 readiness：`reports/readiness/professional-simulator-current-v5/readiness.{json,md}`
- authority 验收：`reports/simulator_acceptance/paper-live-authority-wiring-final-20260806.json`
- causal kernel 验收：`reports/simulator_acceptance/live-paper-causal-kernel-20260806.json`
- PR-8 API/UI：`quant/paper/public_api.py`、`scripts/api/routes/paper_v1.py`、`webpage/paper.html`
- PR-8 PostgreSQL 验收：`runtime_outputs/security/paper-account-manager-postgres-acceptance.json`
- PR-8 浏览器验收：`tests/e2e/paper_account_visual.spec.cjs`、`runtime_outputs/ui/paper-account-{desktop,mobile,order-audit}.png`
- PR-9 replay service/schema：`quant/paper/replay_service.py`、`quant/paper/tenant_platform.py`
- PR-9 PostgreSQL 验收：`runtime_outputs/security/paper-replay-postgres-acceptance.json`
- PR-9 浏览器验收：`runtime_outputs/ui/paper-account-replay.png`
- PR-10 admin/governance：`quant/paper/admin_service.py`、`runtime_outputs/security/paper-admin-postgres-acceptance.json`
- Scenario/conditional：`quant/paper/scenario_service.py`、`quant/paper/conditional_orders.py`、`quant/paper/conditional_worker.py`
- Scenario/conditional PostgreSQL 验收：`runtime_outputs/security/paper-scenario-conditional-postgres-acceptance.json`
- Research/browser 验收：`runtime_outputs/ui/paper-account-{research,model-limits}.png`
- Public export：`/v1/paper/accounts/{account_id}/export`，CSV/JSONL/ZSTD Parquet + SHA256；signed bundle 由 admin evidence API 提供
- 6h soak：`runtime_outputs/paper_live_shadow/gcp_soak_6h_spool_v1/latest.json`
- 24h soak：`runtime_outputs/paper_live_shadow/gcp_soak_24h_execution_v2/latest.json`

## 10. 审计边界

- 本报告不把 `service active` 等同于生产验收通过。
- 本报告不把 mock/回放结果冒充真实 holdout。
- PR-9 的 recorded lifecycle replay 不等于 historical L2 rematch，也不证明 L2 数据完整性。
- 本报告未读取、输出或保存任何 secret 值。
- PR-2 仅在 disposable PostgreSQL 容器中写入迁移数据；未修改源生产数据库、GCP worker、L2 collector、Clash 或真实订单状态。
