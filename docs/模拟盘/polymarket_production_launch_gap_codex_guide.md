# Polymarket 模拟盘完全生产上线差距审计与 Codex 开发路线

> **2026-08-06 需求基线，不是当前实现状态。** 本文的 `MISSING` 和“下一步”用于保留原始产品/生产要求；当前功能闭环以 `docs/模拟盘/polymarket_retail_paper_trading_product_idea.md` 第 26 节和 `runtime_outputs/simulator_closure/current/closure.{json,md}` 为准。生产环境安装、WAF/IAM/TLS、长时间 soak 和真实多用户 UAT 仍是独立外部 Gate。

> 文档日期：2026-08-06  
> 适用对象：`prediction-market-quant` 当前 paper execution 系统  
> 目标：从“核心模拟撮合已经可用”升级到“可以长期常驻、对外提供服务、可审计、可恢复、可扩展的生产级模拟交易产品”  
> 原则：不重写已经通过验收的撮合、账本、结算和回放内核；优先补齐部署、SRE、安全、多租户、产品化和证据链。

---

## 0. 先给出准确结论

根据当前状态快照，你的系统已经达到：

```text
专业内部研究用 paper execution engine：
    核心功能基本完整

单团队、少量策略的内部生产 shadow：
    接近可用，但仍有明确上线阻塞项

面向外部用户的 TradingView / 券商级模拟交易产品：
    还需要一整层生产平台、账户产品、安全合规和运营能力
```

当前最重要的判断不是“还缺不缺撮合功能”，而是：

```text
模拟器内核是否正确
≠
服务是否可以长期生产运行
≠
产品是否可以安全地交给多个用户使用
≠
策略是否具有真实盈利能力
```

当前已经证明了大量第一层能力；接下来要重点建设第二、第三层。

---

## 1. 公开参考平台能学到什么

大型金融公司的内部撮合代码通常不公开。下面只能比较其官方公开产品行为、限制说明和开放源码框架，不能声称知道其内部实现。

### 1.1 TradingView Paper Trading

公开能力包括：

- 订单票据、Depth of Market、图表直接下单；
- 多个模拟账户；
- 自定义初始资金、账户币种、杠杆和佣金；
- Positions、Orders、History、Account History、Trading Journal；
- CSV 导出；
- Bar Replay、Strategy Report；
- 公共模拟交易竞赛与排行榜；
- 对过期合约自动取消订单和结算。

对本项目的启示：

```text
一个生产模拟盘不只是 execution engine，
还必须具有账户生命周期、订单解释、历史审计、数据导出、
重放、绩效报告和明确的用户界面。
```

### 1.2 thinkorswim paperMoney

公开产品把 paperMoney 放在桌面、Web 和移动端交易平台中，并与图表、筛选器、watchlist、条件订单、策略分析和教育内容整合。

对本项目的启示：

```text
模拟交易必须与“发现市场—分析—下单—监控—复盘”形成闭环，
而不是一个孤立的后端撮合 API。
```

### 1.3 Interactive Brokers Paper Trading

IBKR 明确说明：

- paper account 使用真实市场条件和交易设施；
- 实时或延迟行情权限继承自真实账户；
- 模拟与实盘仍有区别；
- 其 paper fills 公开说明主要基于 top of book；
- 某些订单类型、组合交易和 corporate actions 不完全支持。

对本项目的启示：

```text
专业产品不会假装 paper 与 live 完全一致。
必须明确披露：
    使用了什么数据；
    延迟是多少；
    哪些订单语义准确；
    哪些行为只是假设；
    哪些结果不能用于真实盈利承诺。
```

### 1.4 Alpaca Paper Trading

Alpaca 的关键设计：

- paper 与 live 使用相同 API 形状；
- paper 使用独立域名和独立凭证；
- 可创建/删除多个 paper account；
- 官方明确披露 paper 不模拟 market impact、information leakage、
  latency slippage、queue position、price improvement 等；
- 公开提醒不同模拟器会因 fill、liquidity、data source 和收益计算方法产生差异。

对本项目的启示：

```text
策略接口尽量保持 backtest/paper/live 一致；
但环境、凭证、数据库和权限必须彻底隔离。
同时必须把 simulation assumptions 作为产品的一等公民。
```

### 1.5 东方财富模拟交易

公开产品能力包括：

- 用户创建多个模拟账户；
- 自定义初始资金；
- 调整佣金；
- 查询当日委托、当日成交、历史成交和资金流水；
- 以真实交易所行情作为模拟基础；
- 明确列出不支持的交易规则；
- 模拟组合、公开展示和收益分析；
- 期货模拟账户出现穿仓后会被明确标记并禁止继续下单。

对本项目的启示：

```text
账户管理、资金流水、异常账户状态、可解释拒单、
公开/私有组合和排行榜，是消费级产品的重要组成部分。
```

### 1.6 Robinhood

截至本次检索，Robinhood 官方公开的是 options 的 `Simulated Returns`：
它是基于期权定价模型的情景收益可视化，不是完整的 paper brokerage order-lifecycle 环境。

对本项目的启示：

```text
“情景收益模拟器”和“订单执行模拟盘”必须在产品命名上严格区分。
不能让用户把 scenario PnL 当成可成交 PnL。
```

### 1.7 QuantConnect LEAN

LEAN 公开架构将以下能力拆成可插拔现实模型：

```text
BrokerageModel
FillModel
FeeModel
SlippageModel
BuyingPower/MarginModel
SettlementModel
TransactionHandler
Reconciliation
```

对本项目的启示：

```text
不要把 venue 行为写死在策略里。
所有市场规则、费用、成交、容量、结算和风险都应版本化，
由 adapter/model 注入。
```

### 1.8 NautilusTrader

NautilusTrader 使用单线程核心处理 message bus、策略、OMS、风险和 execution coordination，以获得确定性事件顺序和 backtest-live parity。

你的 causal FIFO、journal hash 和 online/offline parity 已经沿着正确方向实现。下一步要把这种确定性延伸到：

```text
多实例部署
故障切换
租约交接
数据库重连
版本升级
```

### 1.9 ABIDES

ABIDES 是 agent-based discrete-event market simulator，可让大量 agent 与 exchange agent 交互，并配置网络延迟，用于研究内生市场影响。

对本项目的启示：

```text
当前 counterfactual liquidity overlay 解决的是“模拟器内部不能重复吃同一深度”；
它仍不等于市场会对你的订单产生真实反应。

对于大资金、市场影响和信息泄漏研究，
未来需要单独的 agent-based / endogenous market simulator。
这不是基础生产上线的 P0，但应作为独立 fidelity tier。
```

---

## 2. 当前已确认的生产阻塞项

这些不是推测，而是当前快照已经明确给出的阻塞项。

### 2.1 GCP 常驻 worker 不存在

当前：

```text
poly-quant-gcp-paper-live-shadow.service:
    unit 不存在
```

这意味着：

```text
代码完整
验收产物存在
≠
生产服务已经部署
```

上线前必须完成：

- 可重复安装的 deployment artifact；
- service unit / container manifest；
- 自动启动；
- 崩溃自动恢复；
- 版本、配置和 schema 检查；
- health/readiness endpoint；
- 部署后 canary；
- 自动回滚。

### 2.2 跨区/跨网络 PostgreSQL 位于 hot path

当前 GCP 通过反向 SSH 访问本机 PostgreSQL，单次 canary 约 `50.5s`。

这不是普通性能 caveat，而是明确的生产架构失败：

```text
实时执行 worker
    → 反向 SSH
    → 异地本机 PostgreSQL
```

任何网络抖动、SSH tunnel 重连、本地断电或本地公网变化都会影响：

- 下单状态持久化；
- reservation；
- journal；
- finality；
- 重启恢复；
- SLO。

必须把实时 paper DB/read model 放到 GCP 同区。

### 2.3 最终 24 小时和 7 天验收未完成

6 小时通过只能证明短期稳定。生产前至少要完成：

```text
单实例 24h soak
跨日 7d soak
主动重启
VM 故障
DB failover
部署升级
网络抖动
事件洪峰
市场批量结算
```

### 2.4 Contract audit 仍为 FAIL

虽然失败来源可能只是 fixture 中的 legacy marker，但 promotion gate 不能通过“人工解释”绕过。

正确处理方式：

```text
修复 fixture
重新生成 contract snapshot
重新运行 contract audit
保存 PASS artifact
绑定 build SHA
```

### 2.5 Taker/Maker 模型证据不足

这仍然是模型可信度问题，但不应阻塞其他生产化工作。

需要继续积累：

```text
Taker grouped holdout
Maker resting-order holdout
特殊 delay market 验证
drift baseline
```

同时所有输出继续保留：

```text
SHADOW_UNCALIBRATED
TAKER_CALIBRATED_IN_DOMAIN
TAKER_EXTRAPOLATED
MAKER_RESEARCH
```

---

## 3. 当前快照没有证明、必须专项审计的能力

下面不能直接断言“代码没有”，但当前证据没有证明它们达到生产要求。Codex 必须逐项审计，并标记：

```text
IMPLEMENTED_AND_ACCEPTED
IMPLEMENTED_NOT_ACCEPTED
PARTIAL
MISSING
NOT_APPLICABLE
```

### 3.1 多租户与身份系统

需要审计：

- user / organization / tenant；
- paper account 所有权；
- strategy 所有权；
- API key；
- OAuth/session；
- RBAC；
- tenant isolation；
- row-level security；
- 管理员 impersonation 审计；
- 用户删除与数据保留；
- 跨租户数据泄漏测试。

单人研究系统不需要完整多租户；公开产品必须有。

### 3.2 Paper 与 Live 的强隔离

必须证明：

```text
paper worker 没有真实下单权限
paper worker 没有真实私钥
paper endpoint 与 live endpoint 不同
paper DB 与 live calibration DB 分离
paper UI 始终有明确模式标识
```

推荐：

```text
GCP project / service account 分离
Secret Manager secret 分离
网络 egress policy 分离
独立数据库或至少独立 instance/schema + IAM
```

真实 probe 应是单独服务：

```text
poly-quant-live-calibration-probe
```

而不是在 paper worker 中通过一个布尔开关启用。

### 3.3 用户级配额与资源隔离

需要：

- 每用户 intents/s；
- 每账户 open orders；
- 每策略 token watchlist；
- replay 并发；
- API request quota；
- DB query budget；
- archive export budget；
- noisy-neighbor 隔离；
- abuse detection。

### 3.4 公共 API 的稳定性

需要：

```text
/v1 paper API
OpenAPI schema
idempotency-key
request_id / trace_id
pagination
error taxonomy
rate-limit headers
SDK
deprecation policy
changelog
contract tests
```

不能让用户直接依赖内部数据库表和 Python 类。

### 3.5 用户可理解的产品界面

至少需要：

```text
Account Overview
Orders
Order Detail
Positions
Fills
Ledger
PnL/NAV
Market Detail
Book Quality
Execution Audit
Strategy Runs
Replay
System Status
```

每笔拒绝/成交必须能回答：

```text
为什么成交？
为什么没有成交？
用了哪个 checkpoint？
数据质量是什么？
模拟模型属于哪个 fidelity tier？
哪些结果没有经过真实校准？
```

### 3.6 数据导出与可携带性

专业平台通常允许用户导出交易和账户历史。

建议支持：

```text
CSV
JSONL
Parquet
signed run manifest
artifact bundle
```

导出至少包含：

- orders；
- lifecycle；
- fills；
- ledger；
- positions；
- NAV；
- TCA；
- model/config version；
- market metadata；
- data quality；
- checkpoint reference。

### 3.7 支持、运营和管理后台

需要审计：

- admin console；
- 用户/账户冻结；
- kill account；
- replay failed event；
- DLQ；
- 手工 reconciliation；
- 系统公告；
- maintenance banner；
- incident notes；
- customer support evidence bundle。

---

## 4. 推荐生产架构

## 4.1 最小内部生产版

适合单团队、单组织使用。

```text
GCP Region
│
├── paper-live-shadow VM
│   ├── systemd/container
│   ├── strategy ingress
│   ├── deterministic kernel
│   └── local health endpoints
│
├── Cloud SQL PostgreSQL HA（同区、private IP）
│   ├── OMS/ledger/journal
│   ├── account/position
│   └── model registry
│
├── GCP L2 Collector
│   └── BookState shared feed
│
├── Cloud Storage
│   ├── Parquet archive
│   ├── benchmark episodes
│   └── run artifacts
│
├── Secret Manager
│
└── Cloud Monitoring / Logging
```

最低要求：

- paper worker 与 DB 同区；
- private IP；
- Cloud SQL regional HA；
- PITR；
- deletion protection；
- systemd 自动启动；
- black-box canary；
- 24h/7d soak；
- 每季度 restore drill。

## 4.2 对外 Beta 版

```text
HTTPS Load Balancer
        ↓
Stateless API/UI services（multi-zone）
        ↓
Durable intent/event bus
        ↓
Partitioned paper workers
        ↓
Cloud SQL HA + object archive
```

关键原则：

```text
API 可以 active-active；
同一个 account/asset partition 的 execution authority 不能同时有两个 owner。
```

需要租约与 fencing：

```text
partition_key
owner_id
lease_epoch
lease_until
last_heartbeat
```

每次权威写入必须携带 `lease_epoch`。旧 worker 即使恢复，也不能继续写账。

## 4.3 商业 GA 版

进一步增加：

- regional managed instance group 或 GKE multi-zone；
- cross-region DR replica；
- RTO/RPO；
- WAF；
- DDoS/abuse protection；
- dedicated read replicas；
- analytics warehouse；
- status page；
- on-call；
- incident management；
- security review；
- billing/metering。

---

## 5. 数据库迁移方案

### 5.1 不要把 reverse SSH 作为生产连接方式

目标状态：

```text
worker → private IP → same-region PostgreSQL
```

### 5.2 数据分层

#### 权威事务库

存：

- paper intents；
- order lifecycle；
- reservations；
- fills；
- ledger；
- positions；
- finality；
- account state；
- worker leases；
- idempotency；
- outbox。

#### 热 read model

存：

- current account summary；
- open orders；
- current positions；
- current NAV；
- strategy status；
- recent TCA。

可以在 PostgreSQL 内做物化/增量表，也可以使用 Redis 做缓存，但 Redis 不可成为权威账本。

#### 冷数据

存：

- raw L2；
- replay episode；
- full audit artifact；
- large reports；
- calibration bundle。

使用 GCS/Parquet，不要让这些查询与 OMS 事务争用同一个数据库。

### 5.3 迁移步骤

```text
1. 创建同区 Cloud SQL HA。
2. 应用完整 migration。
3. 导入静态 metadata 和 paper history。
4. 双写 shadow 验证。
5. 独立 journal rebuild 对比。
6. 暂停新 intent。
7. 最终增量同步。
8. 切换 connection。
9. 运行 canary。
10. 观察并保留回滚窗口。
11. 停止 reverse SSH hot path。
```

### 5.4 验收

必须满足：

```text
ledger final hash 一致
account cash/position 一致
open order/reservation 一致
journal sequence 连续
idempotency key 数量一致
migration 后 replay parity PASS
rollback 演练 PASS
```

---

## 6. 高可用与故障切换

### 6.1 不能只靠 systemd restart

systemd 能处理进程崩溃，但不能处理：

- VM 故障；
- zone 故障；
- stale worker 恢复；
- 双实例同时处理同一账户；
- 数据库 failover；
- 版本切换。

### 6.2 Authority 与 fencing

实现：

```text
paper_execution_partition_leases
```

建议字段：

```text
partition_key
owner_instance_id
lease_epoch
lease_until
heartbeat_at
acquired_at
released_at
```

规则：

```text
同一 partition 只有一个权威 owner；
lease_epoch 单调递增；
所有 order/fill/ledger authority write 带 epoch；
DB 拒绝过期 epoch；
失去 lease 的 worker 立即 fail-closed。
```

### 6.3 分区方式

优先：

```text
account_id
```

或：

```text
tenant_id + paper_account_id
```

市场数据可以共享，账户执行权必须单一。

### 6.4 Failover test

必须自动化：

```text
kill -9 worker
stop VM
drop network
DB failover
lease DB timeout
old worker delayed resume
new worker takeover
```

验收：

```text
0 duplicate fill
0 duplicate ledger
0 double reservation
0 lost terminal event
sequence 连续
recovery time 达到 SLO
```

---

## 7. SLO、监控与运行门禁

## 7.1 核心 SLI

### 数据

```text
book_ready_ratio
book_freshness_ms
feed_divergence_count
gap_count
rest_rebuild_count
coverage_grade_distribution
```

### 执行

```text
intent_queue_age_ms
decision_to_admission_ms
admission_to_arrival_ms
arrival_to_result_ms
order_terminal_latency_ms
unsafe_fill_count
unknown_terminal_count
capacity_reject_count
```

### 账本

```text
ledger_reconciliation_mismatch
reservation_mismatch
negative_cash_count
invalid_position_count
journal_hash_mismatch
finality_reversal_count
```

### 平台

```text
service_availability
DB latency
DB connection saturation
event lag
DLQ size
worker lease age
deployment version skew
```

### 用户

```text
API p50/p95/p99
error rate
rate limit rate
export failure
replay completion time
cross-tenant access denial
```

## 7.2 初始 SLO 建议

按产品目标调整，但至少要明确。

```text
API availability:
    Beta ≥ 99.9%

paper order acceptance API:
    p95 < 300ms
    p99 < 1s

FOK/FAK simulated terminal result:
    p95 < 500ms
    p99 < 2s

execution queue age:
    p99 < 500ms

book freshness:
    依 market activity 分层；
    execution-eligible token 必须在配置阈值内

unknown terminal:
    0

ledger mismatch:
    0

unsafe fill:
    0
```

当前 `50.5s` canary 明确不满足任何实时 paper 产品 SLO。

## 7.3 健康端点

实现：

```text
/health/live
/health/ready
/health/deep
/health/authority
```

`ready` 不能只检查进程存在，还要检查：

- DB；
- BookState；
- lease；
- schema；
- config；
- clock；
- outbox；
- execution backlog。

## 7.4 自动降级

```text
GREEN:
    正常接受

YELLOW:
    只接受 calibrated taker 小单；
    拒绝 maker / large order

ORANGE:
    只读；
    不接受新 intent；
    允许取消和查询

RED:
    fail-closed
```

---

## 8. 发布、升级与回滚

### 8.1 Build manifest

每次部署固定保存：

```text
git_sha
image_digest
schema_version
config_hash
venue_contract_version
execution_model_version
calibration_model_version
benchmark_version
build_time
```

### 8.2 数据库变更

所有 migration 必须：

- additive first；
- old/new binary 兼容；
- 有 down/forward recovery；
- 在 staging 使用生产规模数据验证；
- 不在高峰期长时间锁表；
- migration 失败时 paper service fail-closed。

### 8.3 Canary

部署步骤：

```text
staging replay
→ shadow canary
→ 1% tenant/account
→ 10%
→ 50%
→ 100%
```

每一步检查：

- journal hash；
- latency；
- mismatch；
- memory；
- DB；
- order outcome；
- account rebuild。

### 8.4 自动回滚

触发条件：

```text
unsafe_fill > 0
ledger mismatch > 0
unknown terminal > 0
journal parity fail
error rate 超阈值
latency 超阈值持续 N 分钟
```

---

## 9. 安全

## 9.1 Secrets

必须使用 Secret Manager：

- DB password；
- API key；
- signing key；
- live probe key；
- webhook secret。

要求：

- 不进 repo；
- 不进日志；
- 不进 crash dump；
- 最小 IAM；
- rotation；
- access audit；
- checksum/integrity；
- 环境隔离。

## 9.2 Paper 服务不应持有 live key

这是硬门禁：

```text
paper service account:
    无真实 order submit 权限

calibration service account:
    仅允许白名单账户、白名单市场和额度
```

## 9.3 身份与访问

对外产品至少：

- MFA；
- session management；
- API key scope；
- read/trade/admin permission；
- IP allowlist 可选；
- revoke；
- login audit；
- suspicious activity detection。

## 9.4 数据安全

- TLS；
- private DB；
- encryption at rest；
- backup encryption；
- tenant authorization；
- audit log；
- deletion/retention；
- PII minimization。

## 9.5 安全验收

```text
SAST
dependency scan
secret scan
container scan
penetration test
RBAC negative tests
cross-tenant tests
backup access test
key rotation drill
```

---

## 10. Polymarket 特有的合规与 venue preflight

如果系统只做 paper，不提交真实订单，可以将合规逻辑作为提示层。

如果包含 micro-live 或未来 real adapter，必须在真实下单前：

```text
geoblock check
actual egress IP check
market eligibility
account status
close-only status
balance/allowance
venue mode
```

Polymarket 官方文档要求 builders 在下单前检查 geographic restriction；被限制区域的订单会被拒绝。

生产系统还要保存：

```text
geoblock_result
checked_at
egress_ip
country
region
policy_version
```

不要根据用户浏览器位置推断，必须检查真正下单请求的 egress IP。

---

## 11. 消费级账户产品

## 11.1 多个 paper account

支持：

```text
account template
initial balance
strategy purpose
visibility
fee profile
risk profile
model profile
created_at
archived_at
```

### 不建议“重置并删除历史”

TradingView 允许 reset 删除历史；研究级产品更应采用：

```text
fork new account generation
```

旧账户只读归档，保证审计。

## 11.2 账户模板

例如：

```text
Beginner
Research Conservative
Taker Calibrated
Maker Research
High Liquidity Only
Custom
```

## 11.3 账户状态

```text
ACTIVE
READ_ONLY
RISK_LOCKED
INSOLVENT
DATA_DEGRADED
ARCHIVED
```

东方财富期货模拟账户在穿仓后会明确标记并禁止继续下单。你的账户也应有：

```text
INSOLVENT / RISK_LOCKED
```

而不是继续产生无意义负资金交易。

---

## 12. 用户可见的订单和账户界面

### 12.1 Order ticket

至少展示：

```text
market
outcome
side
shares / notional
limit price
TIF
post-only
estimated fee
estimated worst cost
visible depth
capacity ratio
book quality
expected fill
fidelity label
```

### 12.2 DOM / Book

展示：

- L2 深度；
- spread；
- last trade；
- book age；
- feed redundancy；
- REST reconcile 状态；
- simulated own orders；
- counterfactual consumed depth。

### 12.3 Order detail

展示完整时间线：

```text
CREATED
RISK_ACCEPTED
VENUE_ADMITTED
ARRIVED
PARTIALLY_FILLED
FILLED/CANCELED/REJECTED
FINALITY
LEDGER
```

并给出：

```text
decision checkpoint
arrival checkpoint
reject reason
model version
coverage grade
capacity gate
TCA
```

### 12.4 Account manager

应达到 TradingView 公开 account manager 的基本水平：

```text
Balance
Equity
Available cash
Reserved cash
Realized PnL
Unrealized PnL
Confirmed NAV
Liquidation NAV
Open orders
Positions
History
Account history
Trading journal
CSV export
```

---

## 13. 模拟保真度必须显式展示

每一笔订单都输出：

```text
execution_fidelity
model_confidence
calibration_domain
data_quality
capacity_status
```

建议枚举：

```text
TAKER_L2_CALIBRATED_IN_DOMAIN
TAKER_L2_UNCALIBRATED
TAKER_EXTRAPOLATED
MAKER_STRICT_NO_FILL
MAKER_RESEARCH_PROBABILISTIC
ORDERFILLED_ONLY
CAPACITY_EXCEEDED
MARKET_IMPACT_UNMODELED
DATA_DEGRADED
```

用户报告必须分开：

```text
raw_shadow_pnl
calibrated_taker_pnl
conservative_pnl
maker_research_pnl
uncalibrated_pnl
capacity_excluded_pnl
```

IBKR、Alpaca 等专业平台都会公开说明模拟限制。你的产品也必须在 UI、API 和报告中展示，而不是只写在开发文档里。

---

## 14. Replay、Scenario 和 Strategy Report

TradingView 将实时 paper、Bar Replay 和 Strategy Report 分开。你的产品也应区分：

```text
Live Paper
Historical Replay
Scenario Simulation
Backtest
```

### 14.1 Replay Session

```text
replay_session_id
data_snapshot
start_ts
speed
pause/resume
seed
strategy version
account initial state
execution model
```

要求：

- 可暂停；
- 可恢复；
- 可 fork；
- 同输入 deterministic；
- 结果可导出。

### 14.2 Scenario

支持不依赖真实订单流的研究：

```text
resolution outcome
payout vector
fee change
latency shock
book depth haircut
market closure
feed outage
dispute duration
```

必须明确标记：

```text
SCENARIO
```

不能混入 live paper PnL。

### 14.3 Strategy Report

至少：

```text
net PnL
confirmed PnL
Sharpe/Sortino
max drawdown
turnover
fill ratio
capacity rejected ratio
fee drag
latency slippage
implementation shortfall
market/event/category attribution
benchmark comparison
confidence coverage
```

---

## 15. 高级订单产品

Polymarket venue 原生订单主要是 limit + TIF，但面向用户的模拟产品可提供 synthetic order：

```text
stop
stop-limit
take-profit
trailing stop
OCO
OTO
bracket
time-triggered order
signal-triggered order
```

必须由单独的 `ConditionalOrderEngine` 实现，并记录：

```text
trigger_source
trigger_ts
trigger_price
data_quality
generated_child_intent
```

规则：

```text
book stale 时不触发或进入 pending；
gap 期间不使用未来价格补触发；
child order 仍走正常 OrderIntent、risk、venue 和 ledger 链路。
```

---

## 16. 多租户数据模型建议

```text
paper_tenants
paper_users
paper_memberships
paper_api_keys
paper_accounts
paper_account_generations
paper_strategies
paper_strategy_deployments
paper_orders
paper_fills
paper_ledger_entries
paper_exports
paper_replay_sessions
paper_audit_events
paper_usage_meter
paper_quotas
```

所有权链：

```text
tenant
  → user
  → account
  → strategy deployment
  → intent/order/fill/ledger
```

所有查询必须带 tenant scope。

---

## 17. API 设计建议

### 17.1 环境隔离

```text
https://paper-api.<domain>
https://live-probe-api.<internal-domain>
```

### 17.2 核心 API

```text
POST   /v1/paper/accounts
GET    /v1/paper/accounts
GET    /v1/paper/accounts/{id}
POST   /v1/paper/accounts/{id}/fork

POST   /v1/paper/orders
GET    /v1/paper/orders/{id}
DELETE /v1/paper/orders/{id}
POST   /v1/paper/orders/{id}/replace

GET    /v1/paper/positions
GET    /v1/paper/ledger
GET    /v1/paper/performance
GET    /v1/paper/audit/{order_id}

POST   /v1/replays
POST   /v1/replays/{id}/pause
POST   /v1/replays/{id}/resume
POST   /v1/replays/{id}/fork
```

### 17.3 Idempotency

`POST /orders` 强制：

```text
Idempotency-Key
```

同 key + 同 payload 返回同结果；同 key + 不同 payload 返回冲突。

### 17.4 Error taxonomy

```text
DATA_NOT_READY
BOOK_STALE
BOOK_GAP
MARKET_NOT_TRADABLE
INSUFFICIENT_CASH
INSUFFICIENT_POSITION
CAPACITY_EXCEEDED
RATE_LIMITED
ACCOUNT_LOCKED
MODEL_NOT_ALLOWED
VENUE_MAINTENANCE
INTERNAL_RECONCILIATION_REQUIRED
```

---

## 18. 数据治理

### 18.1 数据来源标记

每个结果绑定：

```text
source
connection_id
book_generation
coverage_grade
exchange_ts
receive_ts
checkpoint_id
archive_file
schema_version
```

### 18.2 延迟和权限标签

IBKR 会让 paper account 的行情权限与真实账户权限一致，并明确延迟数据。你的产品需要显示：

```text
REALTIME
DELAYED
STALE
RECONSTRUCTED
PARTIAL_COVERAGE
```

### 18.3 保留策略

定义：

```text
raw L2 retention
order audit retention
ledger retention
calibration retention
user export retention
deleted tenant retention
```

账本和审计通常比 UI cache 保留更久。

### 18.4 可验证 artifact

每个 run 生成：

```text
manifest.json
SHA256SUMS
orders.parquet
fills.parquet
ledger.parquet
metrics.json
model.json
config.json
data_lineage.json
```

---

## 19. Disaster Recovery

### 19.1 目标

建议初始目标：

```text
Internal production:
    RTO <= 30 min
    RPO <= 5 min

Commercial beta:
    RTO <= 15 min
    committed ledger RPO 接近 0
```

### 19.2 必须做的演练

```text
Cloud SQL failover
PITR restore
accidental table delete restore
GCS artifact restore
worker region rebuild
secret loss/rotation
DNS/LB failover
```

### 19.3 恢复后验证

```text
journal final hash
account rebuild
open order authority
lease epoch
outbox position
BookState generation
no duplicate processing
```

---

## 20. 线上运营能力

需要：

- status page；
- scheduled maintenance；
- incident severity；
- pager/on-call；
- runbook；
- postmortem；
- error budget；
- customer notice；
- changelog；
- model promotion notice；
- venue regime change notice。

### 20.1 典型 runbook

```text
LOB feed divergence
DB high latency
worker lost lease
journal mismatch
unknown order terminal
mass market resolution
venue 425/503
contract schema changed
cross-tenant access alert
```

---

## 21. 仍需继续的模型工作，但不应阻塞平台开发

### 21.1 Taker

继续：

- 200+ grouped samples；
- independent holdout；
- latency distribution；
- depth survival；
- price/quantity correction；
- drift；
- calibration-domain promotion。

### 21.2 Maker

当前只允许：

```text
StrictNoFillMakerModel
Maker Research
```

继续做：

- post-only real lifecycle；
- partial/full/no-fill；
- queue ahead；
- cancel latency；
- Brier；
- reliability curve；
- time-to-fill；
- model domain。

### 21.3 Endogenous Market Impact

新增独立研究模块：

```text
AgentBasedMarketSimulator
```

目标不是替换 Live LOB paper，而是用于：

- 大订单；
- 多策略拥挤；
- 做市商反应；
- 信息泄漏；
- price impact；
- counterfactual market path。

生产报告必须将其标记为：

```text
AGENT_BASED_SCENARIO
```

而不是 actual-execution evidence。

---

## 22. 上线 Gate 分级

# Gate A：内部研究生产

必须全部通过：

```text
[ ] GCP worker 常驻部署
[ ] 同区数据库
[ ] reverse SSH 移出 hot path
[ ] contract audit PASS
[ ] 24h soak PASS
[ ] 7d soak PASS
[ ] VM restart/failover PASS
[ ] DB failover/restore PASS
[ ] journal/ledger rebuild PASS
[ ] health/readiness/canary PASS
[ ] paper/live credential isolation PASS
[ ] unsafe fill = 0
[ ] unknown terminal = 0
[ ] ledger mismatch = 0
```

# Gate B：受邀用户 Beta

除 Gate A 外：

```text
[ ] identity/RBAC
[ ] tenant isolation
[ ] multiple accounts
[ ] quotas/rate limits
[ ] public API versioning
[ ] account manager
[ ] order audit UI
[ ] CSV/JSON export
[ ] fidelity labels
[ ] simulation limitation disclosure
[ ] admin console
[ ] user support workflow
[ ] security scan
[ ] privacy/retention policy
```

# Gate C：商业 GA

除 Gate B 外：

```text
[ ] multi-zone service
[ ] fencing/authority failover
[ ] regional HA DB
[ ] cross-region DR
[ ] on-call/status page
[ ] formal SLO/error budget
[ ] penetration test
[ ] quarterly restore drill
[ ] release canary/rollback
[ ] billing/usage meter
[ ] abuse prevention
[ ] legal/compliance review
[ ] customer-facing incident process
```

---

## 23. 推荐 Codex PR 顺序

## PR-0：Production Capability Audit

任务：

- 读取现有代码、migration、service unit、Terraform、报告；
- 不做重写；
- 输出 `production_capability_audit.md`；
- 对本文每项标记状态；
- 给出代码路径和验收证据。

## PR-1：Deployable GCP Worker

交付：

```text
Dockerfile 或正式 systemd unit
install script
environment validation
health endpoints
version manifest
auto restart
shutdown drain
canary command
```

验收：

- 新 VM 一键部署；
- 重启自动启动；
- SIGTERM 无丢单；
- service unit 存在且 active。

## PR-2：Same-region Authoritative DB

交付：

- Cloud SQL schema；
- migration tool；
- dual-write verifier；
- cutover；
- rollback；
- DB latency metrics。

验收：

- 50.5s 问题消失；
- ledger/journal hash 一致；
- reverse SSH 不在 execution hot path。

## PR-3：HA Lease and Fencing

交付：

- partition lease；
- epoch；
- stale writer rejection；
- takeover；
- failover tests。

## PR-4：SLO and Operations

交付：

- metrics；
- dashboards；
- alerts；
- runbooks；
- status endpoint；
- 24h/7d soak automation；
- report generator。

## PR-5：Paper/Live Security Isolation

交付：

- separate service accounts；
- separate secrets；
- network policy；
- paper no-live-order test；
- secret rotation；
- audit log。

## PR-6：Tenant, User and Account Platform

交付：

- tenant/user/account；
- RBAC；
- RLS；
- multiple accounts；
- account generation/fork；
- quotas。

## PR-7：Public Paper API and SDK

交付：

- OpenAPI；
- `/v1`；
- idempotency；
- error taxonomy；
- Python/TypeScript SDK；
- contract tests。

## PR-8：Account Manager and Audit UI

交付：

- accounts；
- orders；
- positions；
- ledger；
- NAV；
- history；
- journal；
- TCA；
- export；
- data-quality labels。

## PR-9：Replay and Strategy Report

交付：

- replay session；
- pause/resume/fork；
- deterministic artifact；
- performance/risk report；
- benchmark comparison。

## PR-10：Admin, Support and Data Governance

交付：

- admin console；
- tenant freeze；
- event replay；
- reconciliation；
- retention；
- export；
- incident evidence bundle。

## PR-11：Maker Promotion and Agent-based Research

与生产平台并行，不阻塞前面的工程上线。

---

## 24. Codex 总 Prompt

```text
You are upgrading an existing Polymarket paper execution system from a
validated single-team simulator into a production service.

Important:
- Do not rewrite the existing deterministic execution, OMS, ledger,
  finality, resolution, replay, liquidity overlay, or risk core.
- Audit existing code first.
- Treat the current capability report as evidence, not as permission to
  assume unmentioned production capabilities exist.
- Every item must be classified as:
  IMPLEMENTED_AND_ACCEPTED,
  IMPLEMENTED_NOT_ACCEPTED,
  PARTIAL,
  MISSING,
  or NOT_APPLICABLE.
- All schema changes must be additive and migration-safe.
- The paper worker must never possess real-order credentials.
- The live calibration probe must remain a separate, disabled-by-default
  service with hard risk limits.
- A stale worker must never retain execution authority after failover.
- Do not use Redis as the source of truth for ledger, orders, positions,
  or finality.
- Do not mark uncalibrated or capacity-exceeded PnL as trustworthy.
- Do not remove immutable audit history when a paper account is reset;
  create a new account generation instead.

Priority:
1. Restore a deployable and supervised GCP paper worker.
2. Move the authoritative hot-path database into the same GCP region.
3. Add health, readiness, canary, SLOs, soak automation, HA lease/fencing,
   failover and restore evidence.
4. Enforce paper/live security isolation.
5. Only then build multi-tenant accounts, public API, account manager,
   replay, exports and admin operations.
6. Continue taker/maker calibration opportunistically, but do not let
   sample collection block production platform work.

Required first deliverable:
reports/production_capability_audit.md

The audit must include:
- exact code paths;
- migration/table evidence;
- tests;
- deployment evidence;
- missing acceptance evidence;
- P0/P1/P2 actions;
- no unsupported claims.
```

---

## 25. 最终判断

你现在缺的主要已经不是：

```text
“怎么根据 L2 撮合一笔订单”
```

而是：

```text
如何把这个撮合系统变成：
    一直在线；
    同区低延迟；
    故障不重复记账；
    升级可回滚；
    多用户不串数据；
    paper/live 不会误切；
    用户能理解每个结果；
    数据能导出；
    限制被明确披露；
    有 SLO、监控、支持和灾备的产品。
```

最先做的四件事：

```text
1. 恢复并产品化 GCP 常驻 worker。
2. 将 PostgreSQL 权威 hot path 迁移到 GCP 同区。
3. 完成 24h、7d、DB failover 和 restore 验收。
4. 修复 contract audit，建立 paper/live 强隔离。
```

完成这四项后，系统才适合称为：

```text
内部生产级 Polymarket paper execution service
```

再完成多租户、账户产品、API、UI、安全、支持和 DR，才接近 TradingView/券商式的外部商业模拟交易产品。

---

## 26. 官方与开源参考

- TradingView Paper Trading — main functionality  
  https://www.tradingview.com/support/solutions/43000516466-paper-trading-main-functionality/

- TradingView Account Manager  
  https://www.tradingview.com/support/solutions/43000786138-what-is-the-account-manager/

- TradingView Demo Features / Bar Replay / Strategy Report  
  https://www.tradingview.com/support/solutions/43000754966-demo-features-on-tradingview/

- thinkorswim / paperMoney  
  https://www.schwab.com/trading/thinkorswim

- Interactive Brokers Paper Trading limitations  
  https://www.interactivebrokers.com/campus/trading-lessons/paper-trading-vs-live-trading-whats-the-difference/

- Alpaca Paper Trading  
  https://docs.alpaca.markets/us/docs/paper-trading

- 东方财富模拟交易  
  https://emdesk.eastmoney.com/pc_activity/pages/viptrade/pages/link/mnjy.html

- Robinhood Simulated Returns  
  https://robinhood.com/us/en/support/articles/simulated-returns/

- QuantConnect Paper Trading / Brokerage Models  
  https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/quantconnect-paper-trading  
  https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/brokerages/key-concepts

- QuantConnect LEAN  
  https://github.com/QuantConnect/Lean

- NautilusTrader Architecture  
  https://nautilustrader.io/docs/latest/concepts/architecture/

- NautilusTrader Polymarket Adapter  
  https://nautilustrader.io/docs/nightly/integrations/polymarket/

- ABIDES  
  https://github.com/abides-sim/abides  
  https://arxiv.org/abs/1904.12066

- Google Cloud SQL High Availability  
  https://cloud.google.com/sql/docs/postgres/high-availability

- Google Compute Engine Managed Instance Groups  
  https://cloud.google.com/compute/docs/instance-groups

- Google Secret Manager  
  https://cloud.google.com/secret-manager/docs

- Polymarket Geographic Restrictions  
  https://docs.polymarket.com/api-reference/geoblock

- Polymarket User Channel  
  https://docs.polymarket.com/market-data/websocket/user-channel

- Polymarket Matching Engine Restarts  
  https://docs.polymarket.com/trading/matching-engine

- Polymarket Error Codes  
  https://docs.polymarket.com/resources/error-codes
