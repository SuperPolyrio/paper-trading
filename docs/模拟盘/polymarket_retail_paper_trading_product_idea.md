# Polymarket 普通用户模拟盘产品 Idea

> 工程状态（2026-09-03）：`LOCAL_CLOSURE_PASS_EXTERNAL_PENDING`
>
> 外部证据：`LIVE_EVIDENCE_PENDING`；长稳：`SOAK_PENDING`；生产安装：`NOT_YET_ACCEPTED`。
>
> 当前权威闭环报告：`runtime_outputs/simulator_closure/current/closure.{json,md}`。`documentation_closed=true` 只表示每项要求都已分类并绑定证据，不表示外部统计、真实到账、长时间 soak 或生产上线已经通过。
>
> 目的：记录传统券商模拟盘的成熟产品能力，定义本项目面向普通用户的产品层，并维护实现与验收证据。
>
> 边界：本文不是当前功能已经上线的声明，不改变撮合、账户、风控或生产验收状态，也不授权真实交易。

## 1. 产品定位

东方财富、同花顺等传统模拟盘的核心不是逐事件复制真实交易所，而是把以下能力组合成普通用户可以直接使用的交易练习产品：

```text
真实行情
  + 简化但明确的交易规则
  + 独立虚拟资金账户
  + 买卖、撤单、成交和持仓流程
  + 资产、成本和 PnL 核算
  + 排行、比赛、社区和投资教育
```

本项目应借鉴它们成熟的用户流程，但不应降低现有执行证据、账户记账和审计标准。目标是：

> 用传统模拟盘一样简单的操作体验，承载比传统模拟盘更透明、更保守、可追溯的 Polymarket 模拟执行内核。

---

# 第一部分：其他成熟模拟盘有什么功能

## 2. 东方财富

### 2.1 普通模拟交易

东方财富公开产品资料包含以下能力：

- 注册登录后创建模拟资金账户。
- 自定义账户名称和初始资金。
- 一个用户可以创建多个独立模拟账户。
- 从证券详情页直接进入买入或卖出。
- 买入、卖出和撤单。
- 当日委托、当日成交、历史成交和资金流水。
- 配置佣金、默认委托数量和默认委托价格。
- 使用真实交易所行情驱动模拟价格与数量。
- 交易时间和主要交易规则参照真实交易所。
- 行情、自选、K 线、技术指标、资讯与模拟交易联动。

公开规则同时说明，模拟盘并不覆盖所有真实业务，例如新股申购、市值配售、增发申购和部分集合竞价限制。[东方财富终端 FAQ](https://emdesk.eastmoney.com/pc_activity/Pages/VIPTrade/pages/answer.html)

### 2.2 模拟组合和社交

- 每个组合对应独立虚拟资金账户。
- 提供默认初始资金。
- Web 与 App 数据同步。
- 组合可以公开或隐藏。
- 展示收益、持仓和操作记录。
- 用户可以关注、讨论其他人的组合。

来源：[东方财富模拟组合](https://group.eastmoney.com/createcom.html)

### 2.3 Choice 专业组合

- 区分可编辑的回测组合和通过撮合产生记录的实时模拟组合。
- 模拟交易历史不可事后编辑。
- 支持股票、场内基金和部分基金申赎。
- 支持费率、资金和权益参数。
- 实时净值、分时走势和 K 线。
- 组合盈亏、持仓分布、风险水平、VaR 和收益驱动分析。
- 调仓历史和组合报告。

来源：[东方财富 Choice 模拟交易指南](https://choice.eastmoney.com/FileDownLoad/GXSC.pdf)

## 3. 同花顺

### 3.1 普通交易练习

- 使用真实行情进行虚拟资金交易。
- 提供接近真实交易的买卖和委托界面。
- 模拟账户、买入、卖出、委托、撤单和持仓。
- 行业资讯、市场热点和新手学习内容。
- App、PC 和小程序等多终端入口。

来源：[同花顺模拟炒股 App](https://apps.apple.com/cn/app/%E5%90%8C%E8%8A%B1%E9%A1%BA%E6%A8%A1%E6%8B%9F%E7%82%92%E8%82%A1-%E8%BD%BB%E6%9D%BE%E8%82%A1%E7%A5%A8%E5%85%A5%E9%97%A8/id1509655942)

### 3.2 排行、社区和高手跟踪

- 总收益率、月收益率、周收益率和日收益率排名。
- 选股成功率、总资产、用户等级和关注人数。
- 高手主页、公开交易动态和重仓品种。
- 模拟用户资金流向和持仓人数。
- 问答、讨论和高手互动。

来源：[同花顺模拟炒股平台](https://moni.10jqka.com.cn/)、[同花顺排行榜](https://moni.10jqka.com.cn/paihang.shtml)

### 3.3 模拟比赛

- 创建独立比赛。
- 自定义起止时间、初始资金和可交易品种。
- 比赛前热身和开始时统一重置。
- 用户激活独立参赛账户。
- 查看账户、排名和公开操作记录。
- 按总、月、周、日收益排名。
- 支持高校、公司和证券机构独立赛场。

来源：[同花顺比赛实例](https://moni.10jqka.com.cn/hezuo/newbsid_229325)、[同花顺比赛申请](https://moni.10jqka.com.cn/regbs/)

同花顺公开材料没有披露完整 L2 深度消耗、限价单队列或逐笔成交误差模型。因此“仿真体验”描述的是用户体验和基本交易流程，不能解释为每笔模拟成交都与实盘一致。

## 4. 其他成熟产品

### 4.1 富途

- 股票、期权、期货、融资融券和加密货币等多类模拟账户。
- 从行情详情或模拟交易中心快速下单。
- 多市场和多账户切换。
- 资产净值、今日盈亏、订单记录和资金明细。
- 热门推荐、投资教程、榜单、讨论和比赛。

来源：[富途模拟交易手册](https://www.futuhk.com/hans/manual/topic11_112)

富途也会主动简化规则。例如模拟期权可能不提供真实行权，而使用到期现金结算；部分资金处理和订单类型也与真实账户不同。[富途期权模拟规则](https://www.futuhk.com/hans/support/topic1_694)

### 4.2 雪球

- 创建公开或私有组合。
- 按组合权重调仓，不要求用户逐笔填写订单。
- 持仓分布、个股收益、净值曲线和最大回撤。
- 调仓历史、调仓说明和投资逻辑。
- 关注组合、排行榜和跟随。
- 分红、送股、拆股、停牌和退市处理。

来源：[雪球组合 FAQ](https://xueqiu.com/about/faq/4/0)

## 5. 行业通用功能矩阵

| 模块 | 成熟模拟盘的常见功能 |
|---|---|
| 用户与账户 | 登录、多个模拟账户、初始资金、切换、重置、公开或私有 |
| 行情发现 | 搜索、自选、分类、热门、榜单、资讯和提醒 |
| 市场详情 | 实时价格、K 线、盘口、成交和基本资料 |
| 交易入口 | 买入、卖出、价格、数量、仓位比例、确认 |
| 订单管理 | 当前委托、撤单、历史委托、成交明细 |
| 账户核算 | 现金、冻结资金、持仓市值、成本、费用和盈亏 |
| 绩效分析 | 日/月/总收益、净值、回撤、胜率、换手率和归因 |
| 社交和赛事 | 公开组合、关注、动态、排行、比赛和奖励 |
| 教育运营 | 新手教程、规则说明、风险提示、热点和练习任务 |

## 6. 传统模拟盘通常不保证什么

传统模拟订单没有进入真实交易所，因此没有真实队列位置，也不会改变真实盘口或其他交易者行为。行业产品通常不保证：

- 是否成交与真实订单完全相同。
- 成交时间、价格和数量完全相同。
- `PARTIAL/FULL` 状态完全相同。
- Maker 的 FIFO 排队位置相同。
- 撤单到达前是否发生相同成交。
- 大额订单产生相同市场冲击。

Alpaca 明确披露 Paper Trading 不完整建模市场冲击、延迟滑点和限价单排队；IBKR 也说明 Paper 订单没有真实执行或清算，模拟成交主要基于可见报价。[Alpaca Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading)、[IBKR Paper Trading](https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/)

因此传统模拟盘更准确的定义是：

> 交易训练工具、功能验证工具和近似执行模型，而不是实盘成交复制器。

## 7. 怎样评价模拟盘准不准

不能使用收益率或 UI 是否正常作为唯一标准。应按层验收：

| 层级 | 指标 | 目标 |
|---|---|---|
| 订单语义 | 接受/拒绝一致率、状态转换、TIF、撤单竞态、重复执行 | 规则和订单流正确 |
| Taker 成交 | fill/no-fill、VWAP、数量、滑点和执行延迟误差 | 可见深度消耗合理 |
| Maker 成交 | 假成交、漏成交、partial/full、time-to-fill、Brier/ECE | 保守下界或概率校准可信 |
| 账户核算 | 现金、token、成本、费用、realized/unrealized PnL、NAV | 给定成交后账本精确 |
| 数据系统 | gap、stale、重复事件、重启恢复、replay hash | 输入和运行可证明 |
| 策略表现 | 收益、Sharpe、回撤、胜率 | 评价策略，不用于证明模拟器准确 |

核心计算可统一为：

```text
价格误差      = paper VWAP - real VWAP
数量误差率    = abs(paper_qty - real_qty) / real_qty
BUY 到达滑点  = fill_price - arrival_mid
SELL 到达滑点 = arrival_mid - fill_price
Maker Brier   = mean((predicted_fill_probability - actual_fill)^2)
账户误差      = paper balance/PnL - official balance/PnL
```

准确率必须与覆盖率一起公布。被拒绝或 `ABSTAIN` 的订单不能从统计中静默删除，数据不足也不能被记成正确预测。

### 7.1 行业代表性验证方法

| 项目 | 验证方法 | 本项目借鉴点 |
|---|---|---|
| [HftBacktest](https://github.com/nkaz001/hftbacktest) | 用 L2/L3、成交带、延迟和 queue model 回放，再将同时窗 live 与 backtest 的仓位和 fill 叠加对比 | 使用 risk-averse 保守下界和 probabilistic 研究模型，不把 L2 推断宣称为真实 FIFO。[Backtest vs Live](https://github.com/nkaz001/hftbacktest/discussions/54) |
| [NautilusTrader](https://nautilustrader.io/docs/nightly/concepts/backtesting/fill-models/) | 确定性事件引擎、固定随机种子、queue position、延迟、流动性消耗和可插拔 FillModel | 历史订单簿不能显示假想订单对其他参与者的影响；必须冻结模型版本和披露假设。[Matching](https://nautilustrader.io/docs/nightly/concepts/backtesting/fill-prices-and-matching/) |
| [PredictionMarketBench](https://github.com/Oddpool/PredictionMarketBench) | 使用真实 Kalshi LOB/trade 对加密、天气和体育市场做确定性事件回放 | 做预测市场跨类别回归和 IOC/GTC/post-only 语义验证，但不把假想 Maker 队列当真值。[论文](https://arxiv.org/abs/2602.00133) |
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean) | 实盘期间并行运行相同时窗的 OOS 回测，逐 fill 和权益曲线对账 | 将差异归因到行情、时序、延迟、成交模型和市场冲击。[Live Reconciliation](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/reconciliation) |
| [ABIDES](https://github.com/abides-sim/abides) | 创建事件驱动交易所、多类代理和网络延迟，验证市场冲击与极端场景 | 补足 immutable replay 无法表达的内生冲击；人工市场不是 Polymarket 真实反事实。[论文](https://arxiv.org/abs/1904.12066) |
| [Get Real](https://arxiv.org/abs/1912.04941) | 对真实和模拟市场的收益、价差、深度、成交量、到达/撤单、波动聚集和冲击做统计对比 | “策略能跑”不等于“模拟市场真实”，市场真实性指标必须独立。 |
| [Alpaca](https://docs.alpaca.markets/us/docs/paper-trading) / [IBKR](https://www.ibkrguides.com/brokerportal/aboutpapertradingaccounts.htm) | 使用真实行情和简化成交验证 API、订单流和账户体验 | 传统 Paper 也明确不保证真实排队、市场冲击和完整延迟。 |

### 7.2 本项目的六层验收金字塔

```text
L0 规则/确定性：状态机、重放 hash、幂等和中断恢复
L1 历史回放：生产引擎与独立 reference/Nautilus/HftBacktest differential
L2 统计成交：Taker 价格/数量/费用，Maker Brier/ECE/false-positive/time-to-fill
L3 Paper-Live：相同 intent、到达时 book 和模型版本下的小额真实 holdout
L4 账户终态：User WS + REST + 链上 finality + official account truth
L5 市场真实性：价差、深度、成交量、波动、订单流和 impact 分布
```

验收报告必须区分三种规模，不得用小样本 `PASS` 冒充广泛稳定性：

| 等级 | 含义 |
|---|---|
| `SMOKE` | 代码路径和最小场景正常，不建立统计准确性 |
| `REPRESENTATIVE` | 有非重叠日期/event、四类市场、价格和流动性分层，可评价支持域 |
| `BROAD_CROSS_VALIDATED` | 至少 10,000 个模拟订单，使用新的非重叠日期、market、类别、价格和流动性区间，同时公布失败窗口、数据不足、支持域、全样本 coverage 和 abstain |

一个市场时点上的多个 horizon/quote-position 是不同的假想订单，但不是多个独立 event。报告必须同时公布 `simulated_order_count` 和 `independent_event_count`，禁止通过复制场景夸大统计证据。

最高声明必须取所有层的最低值：

```text
离线确定性 PASS + 小额 Taker 支持域
    != LIVE_TAKER_CALIBRATED

公开 L2 strict Maker 保守下界
    != LIVE_MAKER_CALIBRATED

最终余额一致
    != 中间订单流一致
```

真实订单只用于观测公开 L2 无法提供的自有订单排队、网络到达和 venue finality；确定性账本、故障恢复和历史回放不应靠批量真实下单验证。

### 7.3 单笔 Paper-Live 证据契约

真实样本必须能由第三方从原始材料重新判断，不能只保存一行 `MATCH`。每个样本至少绑定：

- 同一 intent 的 side、outcome、TIF、amount unit、数量和价格边界。
- 决策时与到达时冻结的 L2 checkpoint、book age、coverage、gap 和双路一致性。
- execution、fee、latency、queue model 版本及配置哈希。
- HTTP 提交响应、订单 ID、User WS 生命周期、REST order/trade 终态。
- 可获得时的交易哈希、receipt/finality、交易前后现金和 outcome token 余额。
- Paper fill、ledger、position、cost basis、realized/unrealized PnL 和 NAV 变化。
- 官方账户 delta 对账，以及所有原始文件的路径和 SHA256。
- `what_this_sample_proves` 与 `what_this_sample_does_not_prove`。

同一笔订单的 Gate 依次为：

```text
DATA_READY
  -> RULES_MATCH
  -> EXECUTION_MATCH
  -> ACCOUNTING_MATCH
  -> FINALITY_MATCH
  -> CALIBRATABLE_SAMPLE
```

官方非原子账户端点之间的瞬时 mark 差异应归为 `PENDING_CONVERGENCE`，不得覆盖内部账本；订单涉及 asset 的数量、成本、费用、realized PnL 和现金 delta 仍必须逐字段独立通过。模型晋级 Gate 与单笔操作 Gate 必须分开，不能因一次操作通过而自动晋级整个模型。

---

# 第二部分：我们的模拟盘要开发什么功能

## 8. 目标用户与产品原则

目标用户是希望练习 Polymarket 交易、验证策略或观察账户表现的普通用户，而不是只面向内部工程师的审计工作台。

产品原则：

1. 用户不需要输入 API Key、`asset_id`、`condition_id` 或内部 strategy ID。
2. 用户从市场标题、分类和 YES/NO 选项进入交易。
3. 每个用户拥有独立虚拟资金、订单、仓位、成本和 PnL。
4. 交易体验简单，但成交证据和模型限制不能被隐藏或夸大。
5. 复用现有撮合、OMS、账本、PnL、finality 和审计能力，不重写经济内核。
6. 概率 Maker 结果与确定性账本分开；未经证据支持的成交不进入确定性 PnL。

## 9. 产品架构

```text
Rabby/EVM 签名登录（可选身份）
              ↓
        用户与 tenant
              ↓
自动创建 PaperVirtualWallet
默认 10,000 pUSD，ID 为 pwallet_...
              ↓
市场发现 → 市场详情 → BUY/SELL 下单面板
              ↓
现有 Paper API / OMS / Execution Kernel
              ↓
独立 Ledger / Positions / PnL / NAV
              ↓
订单、成交、持仓、结算和绩效页面
```

`PaperVirtualWallet` 是现有账户和账本外层的用户身份模型，不创建私钥、助记词、链上钱包或假 `0x` 地址：

```text
real_wallet_address（可选登录身份）
  → user_id
  → virtual_wallet_id
  → paper_account_registry
  → ledger_strategy_id / economic ledger
```

## 10. P0：普通用户可用的交易闭环

### 10.1 登录和虚拟钱包

- Rabby/EVM 签名登录，或独立的无钱包试用身份。
- 首次登录幂等创建用户、默认虚拟钱包和默认策略。
- 默认一次性入账 `10,000 pUSD`。
- 使用 `pwallet_...` 展示 ID，避免误认为链上充值地址。
- 展示当前钱包、可用现金、冻结现金和总资产。
- 支持账户重置或 fork，但保留 generation 和完整审计历史。
- 严格 tenant、user 和 wallet 数据隔离。

### 10.2 市场发现

- 搜索市场标题、事件和标签。
- 按政治、体育、天气、加密货币和其他类别浏览。
- 展示 LIVE、即将结束、已结算等状态。
- 自选市场、最近查看和持仓相关市场。
- 热门、成交活跃、新市场和即将结算列表。
- 普通用户只看到标题和 outcome，不直接暴露原始 `asset_id`。

### 10.3 市场详情

- 市场标题、规则、截止时间和官方 resolution source。
- YES/NO 当前价格、价差和可用深度。
- 价格走势和近期成交。
- 最小订单、tick size、动态 fee 和 sports delay 等交易规则。
- 当前持仓、平均成本、可卖数量和本市场 PnL。
- 数据状态：实时、降级、间断、已关闭或不可执行。

### 10.4 标准下单面板

- BUY / SELL。
- YES / NO outcome。
- 市价语义与限价语义。
- 金额或 shares 两种输入方式。
- FOK、FAK、GTC、GTD 和 post-only；高级选项默认折叠。
- 25%、50%、75%、100% 仓位快捷输入。
- 提交前展示预计均价、预计数量、最大费用、最大滑点和剩余现金。
- 显示订单采用的执行模型和数据质量，但不要求用户理解内部 checkpoint。
- 风险或数据不合格时清楚说明拒绝原因。

### 10.5 订单与成交

- 当前订单、历史订单和成交明细。
- 撤单、可支持时的改单或 cancel-replace。
- 清楚展示 `PENDING/OPEN/PARTIAL/FILLED/CANCELLED/REJECTED/EXPIRED`。
- 每笔 fill 展示价格、数量、费用、maker/taker 和时间。
- 订单详情默认提供用户语言的结果；高级区域可展开模型、book checkpoint 和审计证据。

### 10.6 持仓与账户

- 现金、冻结资金、持仓市值和总资产。
- 每个 outcome 的数量、可用、冻结、平均成本和当前价格。
- 已实现 PnL、未实现 PnL、当日 PnL 和累计 PnL。
- 可平仓、可 Merge、可 Redeem 状态。
- 资金流水、费用流水、结算流水和奖励流水分开显示。

### 10.7 绩效

- NAV 曲线和收益率曲线。
- 当日、近 7 日、近 30 日和累计收益。
- 最大回撤、胜率、换手率和费用占比。
- 按市场、事件、类别和方向归因。
- 明确区分 confirmed、provisional、estimated 和 research-only 结果。

### 10.8 结算和 Redeem

- 市场结束后继续显示 `CLOSING/RESOLVED/REDEEMABLE`。
- 展示 winning outcome、payout 和结算依据。
- 自动完成 Paper 结算记账。
- 用户可查看赢家到账、输家归零和最终 realized PnL。
- Split、Merge、Convert、Redeem 等高级账户行为放在独立资产操作入口。

### 10.9 模型透明度

面向用户至少提供三档结论：

| 标签 | 含义 |
|---|---|
| `HIGH_FIDELITY` | 合格 L2 和已校准支持域内的保守模拟 |
| `CONSERVATIVE` | 使用 haircut、strict maker 或其他保守假设 |
| `UNAVAILABLE` | 数据或模型无法证明，拒绝模拟成交 |

高级审计信息继续保存：

- execution model/version。
- fee/latency/queue model version。
- arrival book checkpoint。
- coverage grade 和 calibration domain。
- 成交证据、拒绝原因和降级原因。

## 11. P1：留存、学习和社区

P0 交易闭环稳定后，再增加：

- 多个模拟钱包或比赛专用账户。
- 自定义初始资金和重置规则。
- 公开或私有组合。
- 日、周、月和累计收益排行榜。
- 按风险调整收益、回撤和活跃度排名，避免只鼓励高杠杆式赌博。
- 模拟比赛、赛季、起止时间、统一初始资金和限定市场范围。
- 关注用户、公开持仓和交易动态。
- 热门市场、高手观察和策略模板。
- 新手任务、规则说明和风险教育。
- 自选、提醒、到期通知和结算通知。

## 12. P2：专业与高级能力

- 条件单、OCO、OTO、bracket 和策略自动化。
- 回放、fork、scenario 和历史策略报告。
- CSV、JSONL、Parquet 和签名证据包导出。
- 团队、机构和课堂比赛管理。
- 管理员冻结、kill、reconciliation、DLQ 和支持工具。
- 可选的 API/SDK 量化入口。
- 更高级的 maker 概率区间和容量分析。

这些能力不应阻塞普通用户 P0，也不应在默认交易界面暴露内部工程概念。

## 13. 现有能力与产品缺口

以下是 idea 编写时基于现有审计文档的产品映射，不代表最新生产验收：

| 能力 | 可复用现状 | 产品层下一步 |
|---|---|---|
| 撮合和订单语义 | Taker、Maker research、TIF、post-only、撤单和费用已有实现 | 保持内核，接标准下单面板 |
| 账户经济 | cash、reservation、position、cost basis、PnL、NAV、ledger 已有 | 包装成用户虚拟钱包 |
| 多租户基础 | tenant/user/RBAC/RLS 和账户 ownership 已有本地实现 | 接登录、自动开户和真实多用户 UAT |
| API/SDK | `/v1/paper`、OpenAPI 和 SDK 已有本地实现 | 去掉普通用户手工 API Key 流程 |
| Account Manager | orders、positions、fills、ledger、NAV/TCA 和 audit UI 已有 | 保留为专业视图，不作为普通用户首页 |
| 市场发现 | registry 和 market 数据存在 | 新增用户市场列表、搜索、分类和自选 |
| 市场详情 | LOB、规则和 lifecycle 数据存在 | 新增标题驱动的详情页和交易入口 |
| 标准交易体验 | 当前界面偏账户审计，仍暴露内部 ID 和连接设置 | 新增普通 BUY/SELL ticket 和订单中心 |
| 结算 | 后端 resolution/redeem/accounting 已有 | 新增用户可理解的结算与到账页面 |
| 社交和比赛 | 不属于当前核心内核 | P1 独立开发 |

最重要的产品决策是：

> 不把现有 `paper.html` 审计工作台硬改成普通交易首页。普通用户入口应围绕“找市场、看市场、下订单、看持仓”组织；原审计工作台作为高级视图继续保留。

## 14. 核心用户流程

### 14.1 首次使用

```text
登录/签名
  → 幂等创建 pwallet
  → 入账 10,000 pUSD
  → 进入市场列表
  → 选择 YES/NO
  → 预览订单
  → 提交 Paper 订单
  → 查看成交、持仓和 PnL
```

### 14.2 平仓

```text
持仓页
  → 选择 outcome
  → SELL
  → 输入 shares 或仓位比例
  → 查看预计现金回流、费用和 realized PnL
  → 提交
  → 更新现金、剩余成本和 PnL
```

### 14.3 结算

```text
市场关闭
  → 暂停新增风险
  → resolution truth 确认
  → Paper settlement
  → 赢家 payout / 输家归零
  → 最终 realized PnL
  → 账本和结算记录可审计
```

## 15. 产品验收标准

### 15.1 普通用户体验 Gate

- 新用户不输入 API Key 或内部 ID 即可完成首次 BUY。
- 用户可以从市场标题完成 BUY、SELL、撤单和查看成交。
- 用户可以在一个页面理解现金、总资产、持仓市值和 PnL。
- 移动端和桌面端均无关键流程阻断。
- 错误提示使用用户语言，同时保留 request ID 供支持排查。
- 账户 A 的任何操作不能影响账户 B。

### 15.2 订单和账户正确性 Gate

- 订单生命周期非法转换为 `0`。
- 重试、重复事件和重启不重复成交或入账。
- 给定 fill 后，现金、token、成本、费用和确定性 PnL 在规则精度内误差为 `0`。
- SELL 正确释放成本并计算 realized PnL。
- settlement/redeem 不重复增加现金。
- provisional、confirmed、failed 和 voided 状态不混记。

### 15.3 执行模型 Gate

- 小额 Taker 只在支持域和合格 book 下模拟成交。
- Maker strict 模型以低假阳性为目标；没有证据时允许不成交。
- 概率 Maker 只展示概率或 PnL 区间，未经晋级不得写成确定性成交。
- 所有准确率报告同时提供 coverage、abstain 和 data-insufficient 数量。
- 策略收益不能被用作模拟器准确性的替代证据。

### 15.4 产品运营 Gate

- 排行榜使用统一账户规则和统一时间窗口。
- 重置账户不能保留旧收益进入新赛季。
- 比赛账户与普通账户资金、订单和排行隔离。
- 公开组合必须有隐私开关和延迟披露选项。

## 16. 分阶段开发顺序

### Phase A：普通用户 MVP

1. 登录和自动创建 `PaperVirtualWallet`。
2. 市场列表、搜索、分类和市场详情。
3. 标准 YES/NO BUY/SELL 下单面板。
4. 订单、撤单、成交、持仓和资产总览。
5. realized/unrealized/daily/total PnL。
6. 结算和 Redeem 用户视图。
7. 普通用户 E2E 和多钱包隔离验收。

### Phase B：可邀请 Beta

1. 自选、提醒、通知和交易历史筛选。
2. 净值、回撤、胜率、换手率和归因。
3. 模型可信等级和简化说明。
4. 生产身份、API、数据库、WAF、配额和支持流程。
5. 真实多用户 UAT、移动端 UAT 和可用性测试。

### Phase C：社区和比赛

1. 公开/私有组合。
2. 排行榜和赛季。
3. 比赛账户和统一规则。
4. 关注、动态和高手观察。
5. 教程、练习任务和运营内容。

## 17. 明确不做或不宣称

- 不宣称每笔 Paper 订单会与真实订单完全相同。
- 不用价格触达直接证明 Maker 已成交。
- 不用正收益证明模拟器准确。
- 不把数据缺失订单从准确率统计中静默删除。
- 不为 Paper 用户创建或托管真实私钥。
- 不使用假 `0x` 地址充当虚拟钱包。
- 不重写已经存在的订单、账本和 PnL 内核。
- 不让排行榜、社区或比赛阻塞 P0 交易闭环。
- 不把高级审计字段直接暴露给普通用户，但保留可展开证据。

## 18. 最终产品定义

完成 P0 后，本项目应达到：

> 普通用户可以像使用东方财富、同花顺的模拟盘一样，从市场发现开始完成 BUY、SELL、撤单、持仓、盈亏和结算；同时每笔模拟成交都带有比传统模拟盘更明确的数据质量、模型边界和审计证据。

这份 idea 的第一优先级不是继续增加撮合模型，而是把已有专业内核产品化为完整、易懂的用户交易闭环。

## 19. 当前工程闭环示例

截至 2026-09-02，项目已经生成一份真实、可复核的 FOK SELL 对照样本：

- 天气类市场，SELL YES `5` shares，Paper 与实盘均在 `0.13` FULL fill。
- 价格误差 `0 tick`、数量相对误差 `0`、fee error `0`。
- 真实现金 `+0.65`、仓位 `8 -> 3`；Paper 的现金、成本释放和 PnL delta 完全一致。
- User WS、REST order/trade、交易哈希、交易前后余额、冻结 L2 和 account-truth delta 均已绑定到同一证据文件。
- 官方账户 delta 的五个交易相关字段全部 `MATCH`；其他既有仓位的六个实时 mark 差异被单独标记为非原子端点 `PENDING_CONVERGENCE`，material mismatch 为 `0`。

证据：原始 FOK SELL Paper-Live JSON 保留在迁移前的私有证据归档中；本仓库不复制钱包级运行证据。

该结果只证明这一市场、时刻和订单参数下的完整闭环，不证明全部市场的 Taker promotion，不证明 Maker FIFO，也不替代非重叠日期、类别、价格和流动性区间上的广泛交叉验证。

## 20. 专业 PnL 与预测质量契约

普通用户页面不能只显示一个无法解释的“盈亏”。系统必须同时保留交易经济真值、可平仓估值、研究估值和预测能力，且不得混成一条曲线。

### 20.1 持仓成本口径

对每个 outcome token 同时保存：

```text
quantity
fee_exclusive_basis
entry_fees_usdc
gross_initial_value = fee_exclusive_basis + entry_fees_usdc
avg_price_excluding_fee = fee_exclusive_basis / quantity
available_quantity
reserved_quantity
```

这与 Polymarket `/positions` 的 `avgPrice / initialValue / grossInitialValue / entryFeesUsdc` 语义对齐。`SELL` 手续费是退出成本，不追加到剩余持仓的 entry fee。

### 20.2 逐事件记账

```text
BUY:
  cash_delta = -(fill_notional + buy_fee)
  fee_exclusive_basis += fill_notional
  entry_fees_usdc += buy_fee

SELL:
  gross_basis_released = average_gross_basis * sold_quantity
  cash_delta = fill_notional - sell_fee
  realized_pnl_delta = cash_delta - gross_basis_released

SETTLEMENT / REDEEM:
  payout = remaining_quantity * payout_per_share
  realized_pnl_delta = payout - remaining_gross_basis

SPLIT / MERGE / CONVERT:
  必须按 collateral 和 outcome token 守恒记账，
  任一腿失败时原子回滚。
```

所有事件必须有稳定的 `source_event_id` 和幂等键，重复 WS、REST 补拉、重启或 finality reconciliation 不得重复增加现金或 PnL。

### 20.3 四条权威曲线

| 曲线 | 估值口径 | 用途 |
|---|---|---|
| `OFFICIAL_MARK` | 官方 `curPrice/currentValue` 或同口径 mark | 与官方账户快照对账 |
| `RESEARCH_MID` | 合格双边盘口的 mid，或明确降级 mark | 策略研究，不代表立刻可兑现 |
| `LIQUIDATION` | 可用 bid 或逐档 walk-book，超出可见深度的数量不估乐观价值 | “现在平仓大约能拿回多少” |
| `CONFIRMED_RETURN` | 仅包含 confirmed fill、settlement/redeem 和真实 received reward/rebate | 权威账户绩效 |

每条曲线的每个点必须保存 `as_of`、mark source、book age、coverage grade、unpriced quantity 和 completeness。不可估值持仓不得被静默当成零，也不得从组合中删除。

### 20.4 账户收益与资金流

```text
economic_unrealized_pnl
  = marked_position_value - remaining_gross_initial_value

confirmed_account_return
  = realized_trading_pnl
  + economic_unrealized_pnl
  + received_rewards_and_rebates
  - operation_costs

performance_pnl
  = current_nav - initial_nav - net_external_capital_flows
```

默认 `10,000 pUSD` 虚拟钱包不允许外部资金流时，`NAV - initial_nav` 可作为简化 PnL。一旦启用 Deposit、Withdrawal 或 Bridge，必须使用资金流调整后收益，不得把入金当作赚钱。

### 20.5 必须展示的 PnL 指标

- 总资产、可用现金、冻结现金、持仓市值和未定价数量。
- 当日、近 7 日、近 30 日、累计 realized/unrealized/total PnL。
- 时间加权收益、资金流调整收益、最大回撤、波动率、Sharpe 和 Sortino。
- 按 market、event、category、YES/NO、maker/taker 和持有期归因。
- 预测优势、执行滑点、fee、rebate/reward、settlement 和 capital-days 归因。
- confirmed、provisional、estimated 和 research-only 结果分开显示。

### 20.6 预测准确性与交易 PnL 分离

每笔交易可选记录：

```text
subjective_probability
confidence
thesis
evidence_sources
invalidation_condition
exit_plan
decision_market_price
decision_ts
```

市场最终结算后计算：

```text
Brier = (subjective_probability - outcome)^2
Log Loss = -[outcome*ln(p) + (1-outcome)*ln(1-p)]
CLV = signed(final_pre_resolution_price - decision_market_price)
capital_days = capital_at_risk * holding_days
```

预测报告必须按独立 event、category、期限和决策概率桶计算 reliability curve。同一 event 的多个 token 不能伪装成完全独立样本。PnL、Brier、CLV 和 capital-days 必须同时展示，用于区分预测能力、仓位管理和执行质量。

### 20.7 官方账户真值 Gate

每日和手动验收必须对比：

- `/positions`：quantity、avgPrice、grossInitialValue、entryFeesUsdc、currentValue、cashPnl 和 realizedPnl。
- `/closed-positions`：已关闭仓位和官方 realized PnL。
- `/v1/accounting/snapshot`：`positions.csv` 和 `equity.csv`。
- User WS、REST order/trade、OrderFilled/receipt：订单和成交流程真值。

差异只记录和告警，不得直接覆盖内部账本。官方非原子端点的短期差异可以标记 `PENDING_CONVERGENCE`，但稳定窗口后必须收敛。

### 20.8 官网 PnL 曲线等价的硬 Gate

对固定初始资金、无外部入金的 Paper 账户，只能声明“给定 confirmed fill 后的基础账本和 PnL 可确定性验证”。只有以下条件同时成立，才能声明与 Polymarket 官网同一时点的 PnL 曲线口径等价：

1. 完成非空 Paper strategy 绑定，对账范围必须是 `WHOLE_ACCOUNT`，不得用单笔 `CALIBRATION_DELTA` 代替整账户曲线。
2. `/positions` 已实际比较 `size / avgPrice / initialValue / grossInitialValue / entryFeesUsdc / currentValue / cashPnl / realizedPnl`。
3. `/closed-positions` 和 Accounting Snapshot 已实际比较已关闭 realized PnL、cash、positions value、equity 和 total PnL。
4. `provisional_fill_count=0`；`MATCHED_PROVISIONAL / RETRYING / UNKNOWN` 不得进入 confirmed 曲线。
5. `ACCRUED/PAYABLE` reward/rebate 只进估算曲线，只有官方 `RECEIVED` 或链上确认才进 confirmed cash。
6. Deposit、Withdrawal 和 Bridge 等资本流必须从投资收益中剔除，且 `unmodeled_cashflow_count=0`。
7. `account_truth_gate=PASS`，不允许 `PASS_WITH_TIMING_LAG`、字段缺失或空 comparison 冒充完整等价。

聚合验收必须输出 `official_pnl_curve_equivalence`。未通过时，页面可继续展示 Paper 曲线，但必须标记为 Paper 口径，不得使用“与官网完全一致”的表述。

## 21. 全量产品范围

除第 10–12 节之外，最终产品还必须包含：

### 21.1 完整 P0

- Paper API、Web、worker、PostgreSQL 和 GCP BookState 的正式启动与健康检查。
- 浏览器 session 和 wallet-signature/guest 身份，不向普通用户暴露 Paper API key。
- `PaperVirtualWallet` 自动开通、默认资金、reset/fork generation 和多用户隔离。
- 市场搜索、分类、自选、详情、规则、盘口、最近成交和生命周期。
- 标准 BUY/SELL ticket、预估、风险检查、订单、撤单、改单、成交、持仓和结算。
- 每笔订单的用户回执与可展开证据包。
- 本文第 20 节定义的全部 PnL、NAV、归因和官方账户对账。
- 市场规则澄清、proposal、challenge、dispute、resolution 和 redeem 的用户流程。

### 21.2 完整 P1

- 预测日记、Brier、Log Loss、CLV、calibration curve 和 capital-days。
- 事件关系图：互斥、穷尽、包含、相反、共享 oracle 和时间嵌套。
- 联合 payout/risk solver：最坏事件损失、相关仓位和锁定资金。
- 用户级单笔、事件、类别、日亏损、回撤、流动性和频率风险限制。
- Maker 工作台：queue 区间、成交概率、resting horizon、markout、rebate 和库存风险。
- Replay Lab：盘口、规则、事件、订单、持仓和 PnL 的同一时间轴。
- 成交、拒单、partial、stale、临近结算、dispute、redeem 和风险超限通知。

### 21.3 完整 P2

- 排行榜按预测能力、执行质量、风险调整收益和净收益分榜。
- 比赛账户、统一初始资金、服务端重算、不可回填订单、时间窗口和可交易市场范围。
- 公开/私密组合、关注、延迟公开持仓、交易日记分享和隐私控制。
- 移动端、无障碍、国际化、时区、数据导出/删除和客服证据包。
- 可选的多代理 market-impact 压力沙盒，只用于容量研究，不冒充 Polymarket 反事实真值。

## 22. “全部完成”的证据边界

完成状态必须逐项报告：

| 状态 | 含义 |
|---|---|
| `IDEA_ONLY` | 仅有产品设计 |
| `IMPLEMENTED` | 有正式代码，尚未验收 |
| `OFFLINE_ACCEPTED` | 单元、集成、fixture 和故障注入通过 |
| `RUNTIME_ACCEPTED` | 真实 PostgreSQL、worker 和浏览器 E2E 通过 |
| `LIVE_EVIDENCE_PENDING` | 代码完成，缺官方到账、真实订单或结算样本 |
| `SOAK_PENDING` | 功能通过，但 24h/7d 时间尚未真实经过 |
| `PRODUCTION_ACCEPTED` | 代码、runtime、外部证据、安全和 soak 全部达标 |

对 Maker PARTIAL/FULL、reward/rebate 真实到账、官方账户收敛和 24h/7d soak，不允许使用 fixture 、时间压缩或页面截图冒充 `PRODUCTION_ACCEPTED`。

## 23. 2026-09-02 实现与验收状态

### 23.1 总结

当前已完成本文中可由本地代码和受控 PostgreSQL 证明的普通用户产品闭环。状态不是 `PRODUCTION_ACCEPTED`，原因不是仍缺一套 Paper 交易内核，而是若干结论必须等待真实官方结果、真实时间或正式部署环境。

| 范围 | 当前状态 | 结论 |
|---|---|---|
| 普通用户 Paper 产品 | `RUNTIME_ACCEPTED_LOCAL` | 浏览器、API、PostgreSQL 和后台 worker 已联通 |
| 确定性账户/PnL | `OFFLINE_ACCEPTED` + `RUNTIME_ACCEPTED_LOCAL` | 给定 confirmed fill 后的现金、数量、成本、费用、SELL 成本释放、结算和 PnL 可验证 |
| Taker/Maker 与实盘一致性 | `LIVE_EVIDENCE_PENDING` | 有真实样本，但独立样本和支持域仍不足以晋级全部模型 |
| 官方账户终态 | `LIVE_EVIDENCE_PENDING` | adapter 和 UI 已完成；最新报告仍是 `strategy_ids=[]`、比较项为 0 |
| 24h/7d 稳定性 | `SOAK_PENDING` | 真实经过时间不能由缩短测试替代 |
| systemd/WAF/GCP 正式安装 | `NOT_YET_ACCEPTED` | unit 已提供；本轮只完成本机隔离启动，不声明生产部署完成 |

### 23.2 已实现的 P0

- Guest session 与 EVM 签名登录；浏览器使用 HttpOnly session cookie 和 CSRF，不向用户暴露 Paper API key。
- 首次登录幂等创建 `PaperVirtualWallet`、默认策略和一次性 `10,000 pUSD`；支持钱包切换、fork、reset generation 和 tenant 隔离。
- 标题驱动的市场搜索、政治/体育/天气/加密货币分类、自选、最近、持仓、热门、新市场和即将结束集合。
- 市场详情、规则、截止时间、YES/NO、L2、价差、深度、价格图和成交带。
- BUY/SELL、SHARES/QUOTE、FOK/FAK/GTC/GTD、post-only、预估、改单、单笔撤单、按市场撤单和全部撤单。
- 订单、fill、ledger、position、reservation、finality 和用户可展开的执行证据。
- Split、Merge 和 Neg-risk Convert 的 Paper 资产操作；记录 `paper_receipt_ref`，不伪造链上哈希。Redeem 继续由 resolution/finality 生命周期管理，手工重复操作 fail closed。
- 市场澄清、proposal、challenge、dispute、resolution、payout 和 redeemable 状态的用户视图。
- Paper API、Quant API、Web、backtest worker、multi-tenant conditional worker、数据导出 worker、官方历史 worker 的统一启动、监控和退出。

### 23.3 已实现的清晰 PnL

- 独立展示初始资金、可用现金、冻结现金、持仓市值、总资产、已实现 PnL、未实现 PnL 和总 PnL。
- BUY 将成交名义金额和 entry fee 分开保存；SELL 用平均 gross basis 释放成本，退出 fee 不污染剩余持仓成本。
- 每笔 SELL、SETTLEMENT 和 REDEEM 显示 shares、现金变化、费用、当笔 realized PnL 和累计 realized PnL。
- 同时展示 `OFFICIAL_MARK`、`RESEARCH_MID`、`LIQUIDATION` 和 `CONFIRMED_RETURN` 四条曲线，不把研究估值冒充可兑现收益。
- 展示日、7 日、30 日和累计收益，以及回撤、波动、Sharpe、Sortino、换手和费用占比。
- 按 market、event、category、outcome、maker/taker 和持有期拆分数量、gross basis、mark、realized、unrealized、经济 PnL 与 fee。
- 单独展示预测、执行滑点、交易费用、结算、received reward/rebate 和未建模现金流；`ACCRUED/PAYABLE` 不进入 confirmed cash。
- 任何不可定价持仓均显示 completeness 和 unpriced quantity，不静默记零或删除。
- 官方账户真值只读比较，差异只告警，不覆盖 Paper ledger。

### 23.4 已实现的 P1/P2

- 预测日记：主观概率、信心、理由、证据、失效条件、退出计划和隐私；结算后计算 Brier、Log Loss、CLV、calibration bucket 和 capital-days。
- 事件关系与组合风险：互斥、穷尽、包含、相反、共享 oracle、时间嵌套、最坏 payout 和锁定资金均附证据来源。
- 用户风险闸门：单笔、事件、类别、日亏损、回撤、盘口参与率和订单频率，由服务端执行并持久化拒绝原因。
- Maker 工作台：strict authority 与 probabilistic research 分离，显示 queue 区间、概率、resting horizon、markout、rebate 和库存风险。
- Replay Lab、fork、scenario 与统一订单/盘口/持仓/PnL 时间轴；scenario 明确不写入权威 ledger。
- 条件单、OCO、OTO 和 Bracket；父单成功后才激活/提交，父单失败取消，重复事件和重启不重复创建子订单。
- 成交、拒绝、数据 gap、临近结束、redeemable 和风险通知。
- 公开/私密/延迟组合、关注和公开交易日记。
- 净收益、预测能力、执行质量和回撤分榜；比赛使用独立钱包、统一初始资金、服务端 as-of 评分和市场范围。
- CSV、JSONL、Parquet 与 SHA256 manifest 完整导出；删除申请先进入审核，不直接销毁。
- `zh-CN/en-US` 区域格式、IANA 时区、减少动画和高对比度偏好；当前产品文案以中文为主，不能把区域格式支持描述成完整双语翻译。
- 桌面和移动布局、键盘可达的原生控件、ARIA、无横向溢出。

### 23.5 验收证据

| 验收 | 结果 |
|---|---|
| PostgreSQL 产品验收 | `PASS`，60 项；本轮一次性验收库覆盖 13 个隔离钱包；`network_calls=0`；`live_orders_submitted=false` |
| 浏览器 E2E | `PASS`，32 项；使用一次性验收库的 2 个市场；桌面/移动 overflow 为 0；console/request error 为 0 |
| 一键启动 smoke | 7 个进程同时在线；5 个直连/反代健康端点全部通过；随后统一正常退出 |
| 模拟盘组合回归 | `904 passed`；覆盖 execution、Maker、simulator、settlement、risk、calibration 和 paper |
| 回测成交/账本差分回归 | `354 passed`；覆盖 L2 execution、OrderFilled replay、profile、ledger、settlement 和 PnL |
| PnL 语义与官方等价 Gate | post-CLOB-V2 clean cohort 为 `PASS`；整个历史钱包的旧版官网展示口径仍为独立外部证据项 |
| Clean-V2 PostgreSQL 故障注入 | `27/27 PASS`；`live_submission_performed=false`；`production_wallet_changed=false` |
| OpenAPI | checked-in artifact 与运行代码一致 |
| 静态质量 | Python compile、Ruff、Node syntax 和 Bash syntax 通过 |

主要产物：

- `runtime_outputs/paper_retail/postgres-acceptance.json`
- `runtime_outputs/paper_retail/browser-acceptance.json`
- `runtime_outputs/paper_retail/browser/paper-retail-desktop.png`
- `runtime_outputs/paper_retail/browser/paper-retail-mobile.png`
- `docs/api/paper-v1-openapi.json`

### 23.6 尚不能用本地测试替代的结论

以下项目不是 fixture 或增加断言可以诚实完成的：

1. 将真实官方钱包和对应 Paper strategy 绑定后，完成非空 `WHOLE_ACCOUNT` 的 `/positions`、`/closed-positions` 和 Accounting Snapshot 逐字段收敛；当前 `CALIBRATION_DELTA` 只能证明已验收操作，不能证明整条官网 PnL 曲线等价。
2. 扩大非重叠日期、event、类别、价格和流动性域的 Taker/Maker 真实 holdout；Maker 概率模型仍不能宣称精确 FIFO。
3. 等待真实 maker rebate、reward/yield 和 resolved redeem 到账，验证真实 source event、transaction hash 与金额误差。
4. 修复完成后真实运行 24 小时，再运行 7 天，验证长期 freshness、outbox age、unknown terminal、资源和漂移 SLO。
5. 在目标主机安装并启用 systemd unit、生产 WSGI/TLS/WAF/配额和监控告警后，再做生产安全验收。

可选的 ABIDES 式多代理 impact 沙盒仍保持可选研究项；它不能提供 Polymarket 的真实反事实，因此不阻塞普通用户模拟盘，也不能用来替代真实执行校准。

## 24. 2026-09-03 官方账户真值闭环进展

### 24.1 本轮完成的能力

新增只读 `ChainActivityMirror`，从已持久化的 Polymarket 官方 activity 出发，获取对应 Polygon receipt，独立重放 pUSD 和 outcome token 的资产变化。该镜像不写入 Paper 权威 ledger，不覆盖官方数据，也不提交真实订单。

本轮同时修复三个会伪造 PnL 差异的本地实现问题：

1. 账户 equity 不再混用不同时点的 `/positions` mark 和 Accounting Snapshot mark。
2. 同一 asset 在 `/positions` 和 `/closed-positions` 出现完全相同的 realized PnL 时，不再重复计入账户总额。
3. `MERGE` 返回的 collateral 按各腿 gross basis 比例分配，不再把一次无损 Split/Merge 回路伪造成一条盈利腿和一条亏损腿。

`WHOLE_ACCOUNT` 后台任务现在依次执行：官方经济数据同步、Paper strategy 直接对账、链上 activity/receipt 独立镜像。只有直接账户真值和独立镜像都通过才能返回 `PASS`；资产与经济守恒通过、但官方展示口径尚未相等时只能返回 `DEGRADED/PARTIAL`。

### 24.2 真实钱包只读验收

2026-09-03 的非交易验收使用真实官方账户数据和 Polygon receipt，结果为：

| Gate | 结果 | 证据 |
|---|---|---|
| Activity receipt coverage | `PASS` | `70/70` 个官方 activity 对应 receipt 可读，manifest SHA256 校验 `70/70` 一致 |
| Terminal cash | `MATCH` | 链上重放与官方均为 `104.254072 pUSD` |
| Outcome-token surface | `PASS` | `28/28` 个正余额 asset 数量一致；其中 5 个小于 `0.01` 的 dust 被 Data API 省略 |
| Economic conservation | `PASS` | `cash + open gross basis = external capital + received income + realized PnL`，残差约 `2.6e-11` |
| Modern directly comparable fields | `PASS` | 20 个可直接比较的现代 asset，129 个字段，`0` mismatch |
| Official account-truth exact gate | `FAIL_ACCOUNT_TRUTH` | 252 个比较项中仍有 45 个实质差异和 6 个跨 endpoint 时点差异 |
| Official PnL curve equivalence | `NOT_ESTABLISHED` | 官网展示口径与链上经济成本口径尚未全部相等 |

`full_wallet_transfer_enumeration=false`：本轮 `PASS` 限定于“已持久化的官方 Data API activity 及其 70 份 receipt”，尚不代表已从创世区块独立枚举该钱包的所有 ERC-20/ERC-1155 转账。

45 个实质差异均已有证据类别，当前 `unexplained_material_mismatch_count=0`，但“可归因”不等于“相等”：

- 旧 V1 到 V2 迁移前的 Data API 成本与 realized PnL 展示口径。
- Neg-risk conversion 目标 token 的官方展示基价与链上守恒基价口径。
- 已 resolution 但尚未 redeem 的仓位，官方提前展示 realized PnL，而 receipt 镜像等待真实 redeem 现金流。
- 上述组件差异向账户 realized PnL 和 total PnL 的汇总传导。

因此当前必须同时保留两套可审计口径：

```text
CHAIN_ECONOMIC_PNL
  = 根据 receipt 和资产守恒重放的经济 PnL

OFFICIAL_DISPLAY_PNL
  = /positions + /closed-positions + Accounting Snapshot 的官方展示口径
```

在两者还未逐字段收敛前，不得宣称“与 Polymarket 官网 PnL 曲线完全一致”。

### 24.3 当前准确定位

- 新的链上 activity 镜像已证明：资产重放、终端现金、Split/Merge/Trade/Redeem 的经济守恒可以独立验证。
- 现代直接可比较 cohort 已通过，未发现新的本地 PnL 代码差异。
- 这不是权威 Paper ledger 本身与实盘同意图订单的校准；因此 `authoritative_paper_ledger_source=false`，总 PnL truth contract 仍为 `FAIL/NOT_ESTABLISHED`。
- Taker/Maker 成交准度、Maker rebate/reward 到账和 24h/7d soak 仍是独立 Gate，不能由本次账户镜像代替。

### 24.4 下一步

1. 对一个全新的 post-V2 Paper strategy，从初始资金开始保留每个 Paper intent/fill/finality，并与同意图真实操作的 User WS、REST trade、receipt 和 Accounting Snapshot 按 as-of 时间对账。
2. 将“账户镜像”保持为只读参考，不用官方结果倒灌 Paper ledger；只修复能够由原始事件稳定复现的代码错误。
3. 如官方发布 V1/V2 迁移成本、Neg-risk conversion basis 或 resolution 预计入的权威规则，将其作为版本化 accounting policy；在此之前保留双口径并显示差异。
4. 并行继续 Taker/Maker 非重叠 holdout、真实 reward/rebate/redeem 样本和 24h/7d soak，但不将它们与确定性账本验收混为一个“准确率”。

## 25. 2026-09-03 post-CLOB-V2 clean cohort 真实闭环

### 25.1 验收设计

`pnl-20260903-c` 使用一个从零 Paper 经济增量开始的独立 strategy，并将真实钱包中基线时不存在的 asset 绑定为不可变 cohort。每笔操作在真实提交前冻结同一份 Paper 预测，提交后分别保存：

```text
Paper intent/fill/finality
  ↕
真实签名订单 + User WS + authenticated REST order/trade
  ↕
Polygon OrderFilled receipt
  ↕
/positions + /closed-positions + Accounting Snapshot
```

旧钱包的 V1/V2 历史成本不进入该 cohort；比较的是 baseline 之后该 asset 的现金、仓位、成本、费用和 PnL 增量。官方结果只用于 reconciliation，不倒灌或覆盖 Paper fill。

### 25.2 两笔真实同意图操作

市场：`Will the lowest temperature in Shanghai be 26°C on September 5?`，YES token。

| 操作 | Paper 与真实结果 | 官方最终证据 |
|---|---|---|
| FOK BUY | `5 @ 0.48`，quote `2.40`，fee `0.06240`，均 FULL | order `0x2a043b...a22a`；tx `0x536a59...1aefb` |
| FOK SELL | `5 @ 0.41`，quote `2.05`，均 FULL | order `0xc7b07f...93c84`；tx `0x196b4c...e7fea`；链上 fee `0.06047` |

两笔合计真实 gross notional 为 `4.45 pUSD`。最终仓位为 0，计算结果为：

```text
realized PnL
  = SELL quote - SELL fee - BUY quote - BUY fee
  = 2.05 - 0.06047 - 2.40 - 0.06240
  = -0.47287 pUSD
```

最终 checkpoint 的独立官方结果：

- Accounting cash delta：`-0.472870`。
- Paper cash delta：`-0.47287`。
- Accounting 推导的经济 realized PnL：`-0.472870`。
- Paper realized PnL：`-0.47287`。
- `/closed-positions` 展示 realized PnL：`-0.4728`，按官方源的四位显示精度单独保存，不代替六位经济真值。
- current position、marked value 和 provisional fill 均为 0。
- 所有 cohort 外资产在窗口内无变化。

最终状态：`account_truth_status=PASS`、`pnl_truth_contract=PASS`、checkpoint `PASS`，claim 为 `OFFICIAL_POST_V2_COHORT_PNL_CURVE_EQUIVALENCE`。

### 25.3 真实验收发现并修复的问题

1. Authenticated REST 的 condition 查询可能返回同 asset 的后续订单。现在必须按精确 `order_id` 过滤 trade、transaction hash 和 receipt，不能把同一 token 的 SELL 归入先前 BUY。
2. 已经 `CALIBRATABLE` 且账户对账完成的 terminal probe 现在冻结；重复同步只补 receipt/索引，不再用当前余额改写历史 `account_after`。
3. 官方文档只声明费用保留五位。两个真实 V2 `OrderFilled` 半单位样本显示 operator 写入合约前采用向下截断；模型改为 `POLYMARKET_V2_TRUNCATE_5DP_MIN_0.00001`，同时每笔仍以链上 `fee_raw` 做 finality。发现尾差时通过幂等 ledger adjustment 修正 cash、cost basis 或 realized PnL，不改写原始 Paper fill。
4. 平仓后 asset 不再出现在 `/positions`。PnL contract 现在用 cohort 内 open/closed 计数判断字段要求，并从 `/closed-positions` 验证 realized PnL，不再错误要求已关闭资产提供 open-position realized 字段。
5. operation evidence 改为 content-addressed 不可变文件；已终态 operation 禁止被后续同步降级。

上述缺陷都已进入单元测试与 PostgreSQL 验收。`run_clean_v2_cohort_postgres_acceptance.py` 当前 `PASS`，覆盖 fee finality 首次应用、重启幂等、冲突证据 fail-closed 和 BUY/SELL 账本结果；该验收不发网络请求或真实订单。

### 25.4 证据索引

- 最终 checkpoint：`runtime_outputs/clean_v2_cohort/pnl-20260903-c/20260903T090301Z/clean-v2-cohort-checkpoint.json`
- checkpoint content SHA256：`81e0d53577a1f9bf795bdc2b568bd8428d96db8c06d307ddaacc2e8a03af8858`
- Account Truth content SHA256：`3102e52c5562a56d25511d0658dcb492d0ac188c90be604e0055aa0a0e566c6f`
- Accounting ZIP SHA256：`04a79d891f7e09523ec24971d8da66ae81d9d85ae52f72399a5ea356920d8479`
- BUY terminal recovery receipt：`runtime_outputs/clean_v2_cohort/pnl-20260903-c/repairs/buy-terminal-truth-recovery-43e879e3effd0d98374da355f919e6b04348a5124f0bbe6a06236c671ea93db6.json`
- PostgreSQL acceptance：`runtime_outputs/clean_v2_cohort/postgres-acceptance-latest.json`

### 25.5 结论边界

该 cohort 已直接证明：在这一笔动态费率 Taker BUY→SELL 完整闭环中，Paper 的成交状态、价格、数量、费用 finality、现金、成本释放和 realized PnL 与真实 V2 订单流和官方账户终态一致。

它不能单独证明：所有市场类别的 Taker 统计误差、Maker FIFO/PARTIAL/FULL、reward/rebate 到账、Split/Merge/Convert、Settlement/Redeem 或 24h/7d 稳定性。那些继续保留为独立 Gate，不能借本次 `PASS` 自动晋级。

### 25.6 最终复跑

2026-09-03 的收尾复跑没有提交新订单，也没有改变生产钱包：

- clean cohort、REST 精确订单关联、fee finality、Paper ledger 和 Account Truth 定向回归：`89 passed`。
- Ruff 与 Python bytecode compile：`PASS`。
- PostgreSQL 故障注入：`27/27` 检查通过；`live_submission_performed=false`、`production_wallet_changed=false`，content SHA256 为 `c2bff157ce916a6eae10b65ceaba10d0b89fd76fab1557f9cb9595763087913b`。
- 权威数据库状态：`PASS`；真实 committed gross notional 为 `4.45 pUSD`；两笔 operation 为 `EVIDENCE_READY`，未提交的 Houston 候选保持 `ABORTED_NO_SUBMIT`；最终 checkpoint 仍为 `PASS`。
- 新增旧版 clean-cohort summary 的兼容性测试；缺少后续新增的 open/closed scope 计数字段时，重新分类不再因 `int(None)` 崩溃。

这些复跑证明当前实现和不可变证据可以重启、重放并保持同一结果；它们不增加本节 25.5 之外的实盘统计结论。

## 26. 2026-09-03 文档与本地功能闭环

### 26.1 统一状态

当前闭环审计将文档中的要求分为“本地可开发验收”与“必须由外部事实完成”两类，当前结果为：

```text
status                = LOCAL_CLOSURE_PASS_EXTERNAL_PENDING
local_code_closed     = true
documentation_closed  = true
production_accepted   = false
live_submission       = false
```

`documentation_closed=true` 的准确含义是：已经没有未分类、无证据或被旧报告隐藏的功能 TODO。它不会把统计样本、官方到账、真实经过时间和生产基础设施伪装成代码 PASS。

### 26.2 本轮发现并修复的本地问题

1. Maker 真值采集器原先使用已失效的独立代理端口，已改为当前可用的独立校准路由；不切换全局 Clash。
2. 只读/no-submit 采集器仍需要签名身份调用 authenticated order/trade REST，原 systemd 单元未加载私钥 credential。现已通过 `LoadCredential` 与安全 wrapper 加载，不写日志、不落入仓库。
3. 新增可重复的 stdin secret installer，仅允许白名单 credential，目录权限 `0700`、文件权限 `0600`。
4. 局部 watch refresh 后，第一路 snapshot 曾会过早清除 token 的 resync gate。现在要求 refresh 发生时所有已连接 route 都建立新 baseline，同时保留单路真实断线时的可用性降级语义。

Maker collector 当前为 `PASS`：可重启恢复 pending order，User WS 窗口完整，`errors=[]`，且 `exchange_submit_called=false`。

### 26.3 当前能力矩阵

最新 capability audit 共 33 项：

| 状态 | 数量 | 含义 |
|---|---:|---|
| `IMPLEMENTED_AND_ACCEPTED` | 31 | 代码与当前可重复验收均存在 |
| `PARTIAL` | 1 | Maker 概率模型功能存在，但真实时间外样本不足以 promotion |
| `IMPLEMENTED_NOT_ACCEPTED` | 1 | 7 天 soak 能力存在，但尚未真实经过该时间窗口 |
| `MISSING` / `UNKNOWN` | 0 | 无本地未开发或未分类能力 |

该矩阵不含“隐藏 FIFO 已精确恢复”或“每笔实盘必然与 Paper 一致”这类无法由公开 L2 证明的声明。

### 26.4 仍需真实外部证据的 Gate

1. Taker 在更多独立 event、UTC 日期、类别和流动性域的 holdout promotion。
2. Maker 概率模型的真实时间外 promotion；strict Maker 的保守权威语义不因此失效。
3. 整个历史钱包的旧版官方展示口径等价；post-CLOB-V2 clean cohort 已经单独 `PASS`。
4. 真实 maker rebate/reward 到账的金额误差校准。
5. 修正构建真实经过 24 小时和 7 天的 soak。
6. 目标生产环境的 WAF、IAM、TLS、配额、告警和真实多用户 UAT。

这些 Gate 可以继续积累证据，但不应继续阻塞已通过的本地功能闭环，也不能由 fixture、旧样本重复或文档改字代替。

### 26.5 权威证据

- 文档闭环：`runtime_outputs/simulator_closure/current/closure.{json,md}`
- 能力矩阵：`reports/simulator_capability_audit.{json,md}`
- 产品 PostgreSQL：`runtime_outputs/paper_retail/postgres-acceptance.json`
- 产品浏览器：`runtime_outputs/paper_retail/browser-acceptance.json`
- Clean-V2 PnL：`runtime_outputs/clean_v2_cohort/pnl-20260903-c/20260903T090301Z/clean-v2-cohort-checkpoint.json`
- Maker collector：`runtime_outputs/maker_calibration/collector-status.json`

本轮没有提交真实订单、没有改变生产钱包，也没有修改全量 LOB collector。
