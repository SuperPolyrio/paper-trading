# 预测市场量化模拟盘参考调研与开发要点

> 调研日期：2026-07-06  
> 目标读者：准备实现 Polymarket / Kalshi 预测市场模拟盘、回测、实盘前 shadow trading 的工程团队。  
> 说明：本文基于公开 GitHub、官方 API 文档、技术博客和论文摘要整理。对 GitHub 项目的描述主要来自 README / docs / issue，不等价于完整代码审计；落地前应逐项本地复现。

---

## 0. 先给结论

公开资料里**确实已经有预测市场专用的模拟盘 / 回测 / paper trading 项目**，但成熟度参差不齐。它们大致分成四类：

1. **Polymarket 专用 paper trader / simulator**  
   例如 `agent-next/polymarket-paper-trader`、`sonnyfully/polymarket-bot`、`polymarket-trade-engine`。这些项目已经把“用真实 order book 模拟成交、跟踪手续费/滑点、limit order 生命周期”作为核心卖点。

2. **预测市场 event-driven backtest / benchmark**  
   例如 `evan-kolberg/prediction-market-backtesting`、`Oddpool/PredictionMarketBench`、`homerun`。这些更强调历史 order book / trade replay、agent interface、可复现评估、图表和 metrics。

3. **全栈 Polymarket/Kalshi 交易平台 / bot 框架**  
   例如 `ent0n29/polybot`、`0xrsydn/polymarket-crypto-toolkit`、若干 Kalshi bot。它们不一定是高保真模拟盘，但在服务拆分、数据管道、策略插件、paper/live 模式隔离、监控上有借鉴意义。

4. **通用量化模拟 / HFT 回测框架**  
   例如 NautilusTrader、HftBacktest、Freqtrade、Hummingbot、Backtrader、QuantConnect LEAN。它们不是预测市场专用，但在“同一套策略跑 backtest / paper / live”、“事件驱动”、“order book replay”、“latency/queue/fill model”、“dry-run 后再 live”方面非常值得参考。

本文最终建议的架构是：

```text
Dynamic Market Registry
    维护市场生命周期和 tradable token universe

Live LOB Collector
    维护 current BookState + raw L2 event log + book quality

Paper Execution Engine
    taker book-walk + maker queue model + latency + residual book

Ledger / Portfolio / Settlement
    cash / reservation / positions / realized PnL / resolution payout

OrderFilled / Trade Tape Layer
    历史 fallback + 后验成交证据 + 校准

Calibration / Validation
    Live LOB paper prediction
    vs micro-live actual result
    vs delayed OrderFilled-only ex-self prediction
```

一句话：**不要做一个只会“按当前中间价成交”的玩具模拟盘；要做一个事件驱动、动态市场、order book aware、带账本和结算、能被实盘小单校准的模拟交易系统。**

---

## 1. 参考项目总览

### 1.1 预测市场专用 GitHub / 工程项目

| 项目 | 链接 | 类型 | 做了什么 | 对我们有用的点 | 注意事项 |
|---|---|---:|---|---|---|
| Polymarket Paper Trader | https://github.com/agent-next/polymarket-paper-trader | Polymarket paper trading simulator | MCP server、CLI、$10k paper wallet、真实 order book level-by-level execution、手续费、滑点、GTC/GTD limit order state machine、历史 price snapshot 回放、多 outcome market | 很适合参考 CLI / MCP tool / portfolio / order book walk / stats 结构 | README 里“P&L within spread”的说法要用真实微单校准；需检查 maker fill 是否过于乐观 |
| prediction-market-backtesting | https://github.com/evan-kolberg/prediction-market-backtesting | Polymarket backtesting framework | 基于 NautilusTrader，custom exchange adapter，强调 Polymarket backtest、多市场 charting、Kalshi 依赖 L2 historical book data | 适合参考 event-driven adapter、Nautilus 对接、图表、Brier advantage、portfolio replay | 项目仍在 active development；Kalshi 可运行性有限 |
| Homerun | https://github.com/braedonsaunders/homerun | full-stack prediction market trading platform | Polymarket & Kalshi，Python strategy/data source，backtest、paper/shadow、live，L2 history、microstructure-aware fill simulator、dashboard | 适合参考“策略/数据源/回测/paper/live/仪表盘”产品结构 | AGPL-3.0；要注意许可证影响 |
| Polybot | https://github.com/ent0n29/polybot | Polymarket HFT infrastructure | Java 21 microservices，paper/live execution，strategy runtime，market/user trade ingestion into ClickHouse，Redpanda，Grafana/Prometheus | 适合参考服务拆分：executor、strategy、ingestor、analytics、orchestrator、monitoring | 更像基础设施/研究平台，不是单纯模拟盘；需要验证 paper execution 细节 |
| Polymarket Trading Toolkit | https://github.com/0xrsydn/polymarket-crypto-toolkit | Polymarket Python toolkit | packages/core/data/indicators/strategies/backtest/executor，WebSocket DataFeed，plugin strategy，paper mode | 适合参考 Python package layout、DataFeed protocol、plugin registry、walk-forward/parameter sweep | 作者标注 experimental；不是完整高保真交易所模拟器 |
| polymarket-toolkit | https://github.com/runesleo/polymarket-toolkit | 钱包执行与校验工具 | executor、maker/taker mix、markout、BUY/SELL/SPLIT/MERGE/REDEEM/REBATE 现金流分类和 PnL 分页 | 适合校验钱包现金流、执行质量与 dry-run/notional 安全边界 | 没有历史事件循环、订单生命周期或 matching engine；markout 不是持有至结算 PnL |
| sonnyfully/polymarket-bot | https://github.com/sonnyfully/polymarket-bot | Polymarket simulator/bot | Gamma + CLOB REST/WS ingestion，derived features，strategy engine + deterministic paper execution，risk gate，signals/fills/PnL 持久化 | 很适合参考“多策略共享同一套市场状态与执行模型，结果可比较”的设计 | README 明确指出 paper trading 会在 passive fill/adverse selection 上撒谎，要把局限显式化 |
| polymarket-trade-engine | https://github.com/KaustubhPatange/polymarket-trade-engine | Polymarket trading engine | lifecycle、paper wallet、order book WS、network delay/failure、partial fill、callbacks、simulation-first | 适合参考 MarketLifecycle 和“等待 book ready 再策略运行”的流程 | callback 恢复/重启连续性需谨慎 |
| Polymarket Agents | https://github.com/Polymarket/agents | official agent examples | 面向 AI agents 的 Polymarket autonomous trading | 可参考官方 agent 工具接口和 SDK 用法 | 不是模拟盘实现 |
| PredictionMarketBench | https://github.com/Oddpool/PredictionMarketBench | Kalshi replay benchmark | 用真实 Kalshi market replay data 评估 trading agents；episode 包含 orderbook、trades、settlement；输出 equity/PnL/Sharpe/drawdown | 预测市场专用、最贴近“标准化验收”的参考；agent interface 很值得学习 | 以 Kalshi episode 为主；不是 Polymarket live paper |
| Kalshi Trading Bot | https://github.com/Viprasol-Tech/kalshi-trading-bot | Kalshi bot framework | Backtest、paper-trade、deploy，dry-run by default | 参考 Kalshi bot 项目的 dry-run/risk control 表述 | 需验证实现深度 |
| Kalshi AI Trading Bot | https://github.com/ryanfrigo/kalshi-ai-trading-bot | Kalshi toolkit | API client、market-data ingestion、position tracking、SQLite telemetry、Streamlit dashboard、LLM hooks | 参考 dashboard、SQLite telemetry、API auth | 不是高保真 LOB simulator |
| Prediction Market Challenge | https://github.com/danrobinson/prediction-market-challenge | local FIFO LOB challenge | 本地二元 YES 合约 FIFO order book，latent fair value/informed arb/retail flow/hidden liquidity | 适合做本地单元测试和策略训练 sandbox | 不是接真实 Polymarket/Kalshi |
| prediction-market-maker | https://github.com/octavi42/prediction-market-maker | market making case study | prediction market making 的库存、报价、风险 | 适合策略层学习 | 不是完整模拟盘 |

---

### 1.2 通用量化 / HFT / Paper Trading 框架

| 框架 | 链接 | 相关能力 | 对预测市场模拟盘的启发 |
|---|---|---|---|
| NautilusTrader | https://github.com/nautechsystems/nautilus_trader | Rust-native、多资产多 venue、research / deterministic simulation / live execution 同架构、nanosecond resolution、order book data、advanced order types | 策略不应区分 backtest/paper/live；用 adapter 隔离 venue；同一套 execution semantics |
| HftBacktest | https://github.com/nkaz001/hftbacktest | market-data replay-based HFT backtest；feed/order latency；queue position；full L2/L3 order book reconstruction | maker fill 不能靠“盘口减少”；必须做 queue model、latency model、live calibration |
| Freqtrade | https://www.freqtrade.io/ | crypto bot；backtesting、hyperopt、dry-run/live；强调 backtest 假设与 dry-run 差异 | 先 backtest，再 dry-run/forward test；dry-run 更接近实时但仍不是实盘 |
| Hummingbot | https://github.com/hummingbot/hummingbot | crypto market making bot；paper trading mode、paper balances、balance limits、kill switch、status/monitoring | paper/live 隔离、余额限制、kill switch、实时报表很关键 |
| Backtrader | https://github.com/mementum/backtrader | Python backtesting/live trading framework | 策略、broker、data feed、analyzer 分层；适合早期研究，但 LOB/HFT 不够 |
| QuantConnect LEAN | https://github.com/QuantConnect/Lean | 开源引擎，research/backtest/live，模块化 datafeed、transaction handler、realtime handler | 模块边界清晰：datafeed、transaction、portfolio、results、realtime 都要可替换 |
| ABIDES | https://github.com/abides-sim/abides | agent-based interactive discrete-event simulation | 适合研究市场微结构、代理行为、模拟市场冲击 | 不是 prediction-market 专用，也不是直接接 Polymarket |

---

### 1.3 官方 API / 技术文档

| 来源 | 链接 | 和模拟盘的关系 |
|---|---|---|
| Polymarket Fetching Markets | https://docs.polymarket.com/market-data/fetching-markets | active market discovery；events endpoint；pagination；`active=true&closed=false` |
| Polymarket Market WebSocket Channel | https://docs.polymarket.com/market-data/websocket/market-channel | L2 book、price_change、last_trade_price、best_bid_ask、new_market、market_resolved |
| Polymarket Get Order Book | https://docs.polymarket.com/api-reference/market-data/get-order-book | REST `/book` snapshot；market/asset_id/hash/bids/asks/min_order_size/tick_size/neg_risk/last_trade_price |
| Polymarket Orders Overview | https://docs.polymarket.com/trading/orders/overview | limit-order primitive、GTC/GTD/FOK/FAK、tick size、negative risk、OrderFilled、heartbeat、error codes |
| Kalshi Get Market Orderbook | https://docs.kalshi.com/api-reference/market/get-market-orderbook | binary order book returns YES/NO bids only；YES bid X 等价 NO ask 1-X |
| Kalshi Orderbook Responses | https://docs.kalshi.com/getting_started/orderbook_responses | fixed-point orderbook arrays；`yes_dollars` / `no_dollars` |
| Kalshi Orderbook WebSocket Updates | https://docs.kalshi.com/websockets/orderbook-updates | orderbook_snapshot + orderbook_delta；seq；动态 add/delete markets |
| Kalshi Fixed-Point Migration | https://docs.kalshi.com/getting_started/fixed_point_migration | subpenny pricing、fractional contracts、fixed-point strings、tick tiers |
| Kalshi Demo Environment | https://docs.kalshi.com/getting_started/demo_env | 真实 venue demo 环境，和本地模拟盘不同；可用于 integration tests |

---

### 1.4 论文 / 博客 / Issue

| 来源 | 链接 | 技术价值 |
|---|---|---|
| PredictionMarketBench paper | https://arxiv.org/abs/2602.00133 | 预测市场回测 benchmark；强调 binary payoff、microstructure、fees、settlement risk、deterministic event-driven replay |
| Limit Order Book Simulations: A Review | https://arxiv.org/html/2402.17359v1 | LOB simulation 分类、stylized facts、price impact；做 maker/market impact 模型前值得读 |
| Reconstructing Full Limit Order Books for Kalshi | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6583921 | 从 Kalshi public API 收集并重建 full-depth LOB 的方法 |
| Comparing Prediction Market Mechanisms | https://www.jasss.org/21/1/7.html | continuous double auction vs LMSR 等预测市场机制对比 |
| Comparing Prediction Market Structures | https://people.cs.vt.edu/~sanmay/papers/predmarkets.pdf | prediction market maker 机制在 simulation/live trading 里的比较 |
| Fix MM Paper Trader Issue | https://github.com/suislanchez/polymarket-kalshi-weather-bot/issues/65 | 非常重要的反例：把 orderbook depth 消失当成 maker fill 会严重虚高；必须用真实 trade evidence / queue model / cash / resolution |
| Hacking the Markets Kalshi order book watcher | https://hackingthemarkets.com/kalshi-live-order-book-watcher-with-python/ | 从零构建 Kalshi live orderbook watcher 的教程思路 |
| PolyTest backtesting guide | https://www.polytest.io/docs/guides/how-to-backtest-prediction-markets | 预测市场回测不同于 spot crypto；必须用 depth 模拟 fill，不要按 mid-price |
| Paradigm prediction market AMM | https://www.paradigm.xyz/2024/11/pm-amm | AMM/MSR 与 CLOB 的机制区别；可帮助理解 prediction market liquidity |

---

## 2. 别人模拟盘都有哪些共性模块？

整理上述项目和通用框架后，可以抽象出一个合格模拟盘至少需要 12 个模块。

### 2.1 Dynamic Market Registry

预测市场不是固定 symbol universe。每天都有新 market、关闭 market、resolved market、archived market。

必须维护：

```text
markets
market_tokens
market_lifecycle_events
subscription_universe
execution_universe
```

关键点：

- 新 market 不是 `created_at` 出现就能交易。
- metadata 完整但没有 book snapshot 时，只能进入 `TRADABLE_PENDING_BOOK`。
- `active=false` / `closed=true` 只说明不能交易，不等于已经结算。
- `end_date` 只能是计划结束时间，不能当成 resolution truth。
- settlement 必须来自 winning outcome / winning asset / oracle / official resolution。

推荐状态机：

```text
DISCOVERED
    只知道 market 存在，metadata 未验证完整。

TRADABLE_PENDING_BOOK
    metadata 和 token 映射完整，但还没有可用 book snapshot。

LIVE
    有新鲜 BookState，book_quality 合格，允许模拟成交。

STALE
    market 仍然 active，但 book 断线、gap、过期或质量不合格。

CLOSING
    停止接受交易，但还未 resolution。

RESOLVED
    有 winning outcome / winning asset，进行 settlement。

ARCHIVED
    不再更新，只保留历史数据。
```

### 2.2 Market Data Collector

模拟盘必须至少有三层 market data：

```text
raw_event_log
    WebSocket / REST / trade tape 原始事件，不做破坏性处理。

current_book_state
    内存中的实时 L2 book，用于即时模拟。

summary_tables
    BBO、spread、top-N depth、imbalance、book_age、quality，用于 UI / scanner。
```

推荐存储：

```text
raw_l2_events
book_checkpoints
live_book_state_status
bbo_summary_1s
trade_prints
market_data_gap_events
```

不要每次盘口变化都存完整 bids/asks。更合理的是：

```text
raw event log + periodic checkpoint + derived summary
```

### 2.3 Book Builder / 本地订单簿

本地 LOB 构建规则：

```text
snapshot/book:
    清空旧 book，重建完整 price -> size map。

delta/price_change:
    side=BUY 更新 bids
    side=SELL 更新 asks
    size=0 删除 price level
    size>0 设置 price level size

last_trade_price / trade:
    作为成交证据，不单独改 depth，depth 由 book/price_change 维护。
```

关键质量状态：

```text
READY_HIGH
READY_MEDIUM
STALE
GAP
NO_SNAPSHOT
DISCONNECTED
HASH_MISMATCH
```

任何 `STALE/GAP/NO_SNAPSHOT` 都不能让新模拟订单成交，应该返回：

```text
DATA_NOT_READY
BOOK_STALE
BOOK_GAP
WAITING_FOR_SNAPSHOT
```

### 2.4 Execution Simulator

#### Taker 模型

Taker 是相对容易的：

```text
BUY:
    walk asks from low to high
    price <= limit_price

SELL:
    walk bids from high to low
    price >= limit_price

FOK:
    不足全量则整单 reject

FAK/IOC:
    可成交部分成交，剩余 cancel

GTC/GTD:
    先吃可成交部分，剩余 resting

post_only:
    如果会 cross spread，则 reject
```

必须记录：

```text
book_at_decision
book_at_arrival
latency_ms
filled_size
avg_price
slippage_vs_mid
fees
residual_book_consumed
```

#### Maker 模型

Maker 是最容易虚高 PnL 的地方。L2 只能看到每个价位总 size，看不到真实 FIFO 队列和撤单身份。

推荐用保守规则：

```text
queue_ahead = visible_size_at_price * queue_ahead_fraction + safety_buffer
```

队列推进只允许来自：

```text
1. real trade print / last_trade_price at our price or through our price
2. confirmed OrderFilled / venue trade event
3. probabilistic queue model calibrated by micro-live
```

不能把普通的 price level size decrease 全部当成成交。它可能是：

```text
cancel
modify/refresh
maker quote pull
trade
book rebuild artifact
data gap
```

公开 issue 里已经有非常典型的反例：某 Polymarket/Kalshi weather bot 的 paper trader 因为把 `depth_consumed` 当成 fill，96% fill 来自噪声路径，导致 PnL 和成交数严重虚高。这是我们必须避免的坑。

### 2.5 Residual Book / Self Impact

同一个时间点内，多个模拟订单不能重复吃同一份盘口。必须有 residual book：

```text
public_book_snapshot
    ↓
simulated_order_1 consumes depth
    ↓
residual_book_after_order_1
    ↓
simulated_order_2 consumes remaining depth
```

如果策略订单规模很小，可以声明“no market impact, residual within simulator only”；如果订单规模接近盘口深度，则必须有更保守的 market impact / slippage haircut。

### 2.6 Latency Model

模拟盘至少要区分这些时间：

```text
signal_ts
decision_ts
submit_ts
simulated_arrival_ts
exchange_ack_ts
fill_ts
cancel_ts
settlement_ts
```

最低实现：

```text
arrival_ts = decision_ts + configured_order_latency_ms
book_at_arrival = latest book snapshot <= arrival_ts
```

进阶实现：

```text
feed_latency_model
order_latency_model
ack_latency_model
cancel_latency_model
jitter_distribution
per-venue latency profile
```

### 2.7 Order State Machine

必须有完整订单生命周期：

```text
CREATED
VALIDATED
REJECTED
ACCEPTED
LIVE
PARTIALLY_FILLED
FILLED
CANCEL_REQUESTED
CANCELED
EXPIRED
SUSPENDED_BY_DATA_QUALITY
SUSPENDED_BY_MARKET_STATE
SETTLED
```

每次状态变化要写：

```text
paper_order_events
    order_id
    old_state
    new_state
    event_ts
    reason
    source
    related_book_event_id
    related_trade_event_id
    model_version
```

### 2.8 Ledger / Portfolio

模拟盘必须像真实账户一样有账本，不能只有 “PnL = 当前估值 - 成本”。

至少维护：

```text
cash_available
cash_reserved
positions_by_asset
position_cost_basis
realized_pnl
unrealized_pnl
fees_paid
settlement_receivable
```

核心规则：

- 下 buy order 要 reserve cash。
- 下 sell order 要 reserve shares。
- partial fill 要释放剩余 reserve。
- cash 不足直接 reject。
- 不允许隐式无限杠杆。
- market resolved 后 winner $1/share，loser $0/share。
- unresolved inventory 要按 conservative mark 或 book mark，不可直接当利润。

### 2.9 Fees / Tick / Min Size

必须在下单前做和 venue 一致的校验：

```text
tick_size
minimum_tick_size
min_order_size
max_order_size
price_precision
size_precision
fee_rate_bps
maker_rebate_rate
negative_risk / neg_risk
```

关键注意：

- Polymarket tick size 不是固定 0.01，有 `tick_size_change`，也有特殊 sports tick size。
- Kalshi 已经引入 fixed-point dollar strings 和 fractional contract fields。
- 不要用 float 做价格/数量，统一 Decimal 或整数 tick/atom。

### 2.10 Resolution / Settlement

预测市场模拟盘和普通 crypto paper 最大差异之一是 settlement。

必须区分：

```text
Trading stopped:
    不接受新 order，open order cancel/expire。

Resolved:
    有 winning outcome，positions 可以结算。

Archived:
    历史态，不再交易/更新。
```

结算流程：

```text
freeze market
cancel open orders
determine winning asset/outcome
credit winner positions at $1/share
write off loser positions at $0/share
write realized pnl
write settlement audit
```

不要把 `end_date` 直接当 settlement。

### 2.11 Calibration / Validation

模拟盘不能靠自我感觉验收。推荐三轨校准：

```text
A. Live LOB Paper Prediction
    用下单当刻 BookState 预测成交。

B. Micro-live Actual Result
    极小实盘订单，记录 ack/fill/cancel/reject。

C. Delayed OrderFilled-only ex-self Prediction
    事后用真实 trade tape / OrderFilled，排除自己的成交后重算。
```

指标：

```text
filled_size_error
avg_price_error_ticks
slippage_error_bps
false_positive_fill_rate
false_negative_fill_rate
overfill_rate
underfill_rate
maker_fill_probability_calibration
time_to_fill_error
PnL_error
settlement_pnl_error
```

### 2.12 Observability / Audit

产品级模拟盘要能回答：

```text
为什么这个单成交了？
为什么这个单没成交？
当时 book 是什么？
数据质量是否合格？
是否有 gap/reconnect？
该 fill 有没有真实 trade evidence？
cash 是否足够？
哪个模型版本给出的结论？
```

推荐日志/表：

```text
paper_order_audit
paper_order_events
book_snapshot_at_decision
book_snapshot_at_arrival
source_l2_event_ids
source_trade_event_ids
execution_model_version
config_hash
data_quality_flags
```

---

## 3. Polymarket 模拟盘开发重点

### 3.1 Market Discovery

Polymarket active market discovery 应使用 Gamma events/markets，并且分页拉取：

```text
GET /events?active=true&closed=false&limit=100&offset=0
GET /events?active=true&closed=false&limit=100&offset=100
...
```

重点字段：

```text
market_id
event_id
condition_id
question_id
slug
active
closed
archived
clob_token_ids
outcomes
enable_order_book
accepting_orders
minimum_tick_size
current_tick_size
min_order_size
neg_risk
fee_schedule
end_date
closed_time
resolved_time
```

### 3.2 WebSocket Market Channel

Polymarket market channel 用 asset IDs 订阅：

```json
{
  "assets_ids": ["<token_id_1>", "<token_id_2>"],
  "type": "market",
  "custom_feature_enabled": true
}
```

需要处理：

```text
book
price_change
tick_size_change
last_trade_price
best_bid_ask
new_market
market_resolved
```

关键点：

- `book` 是 snapshot / book-affecting trade 后的 book。
- `price_change` 可来自新订单或取消订单；`size="0"` 表示价位删除。
- `last_trade_price` 是 maker/taker matched 的成交事件。
- `new_market` 是快信号，不是唯一 market discovery 来源。
- `market_resolved` 包含 winning_asset_id / winning_outcome，可作为 settlement 快信号，但需要兜底校验。

### 3.3 REST `/book`

用于：

```text
新 token book readiness probe
WebSocket reconnect 后重建
hash mismatch 后重置
periodic consistency check
```

返回字段应写入 snapshot：

```text
market
asset_id
timestamp
hash
bids
asks
min_order_size
tick_size
neg_risk
last_trade_price
```

### 3.4 Order Types

Polymarket 所有 order 本质是 limit order：

```text
FOK / FAK:
    marketable order，立即 against resting liquidity。

GTC / GTD:
    resting limit order。

post-only:
    如果会 cross spread，应该 reject。
```

模拟盘必须完全复现这些行为。

### 3.5 OrderFilled 和 On-chain Truth

OrderFilled 是事后真相层的一部分：

```text
orderHash
maker
taker
makerAssetId
takerAssetId
makerAmountFilled
takerAmountFilled
fee
```

用途：

```text
历史 fallback
settlement/audit
校准 maker queue
验证真实成交方向/金额
```

但不要把 OrderFilled 当成实时唯一输入。实时成交预测主要靠 BookState；OrderFilled 用于后验校准。

---

## 4. Kalshi 模拟盘开发重点

Kalshi 和 Polymarket 的差异值得单独记录，因为 Kalshi 是预测市场回测的很好参考对象。

### 4.1 Binary Order Book 表示

Kalshi orderbook 返回的是 YES 和 NO 两边的 bids，不直接返回 asks。因为：

```text
YES bid at X
    等价于
NO ask at 1 - X
```

模拟 taker 时要显式转换：

```text
BUY YES:
    hit YES asks = derived from NO bids

SELL YES:
    hit YES bids

BUY NO:
    hit NO asks = derived from YES bids

SELL NO:
    hit NO bids
```

### 4.2 Fixed-Point

Kalshi 使用 fixed-point dollar strings 和 fixed-point contract counts：

```text
price_dollars = "0.4200"
count_fp = "13.00"
```

不要用 float；统一用 Decimal / scaled integer。

### 4.3 WebSocket Orderbook

Kalshi WebSocket orderbook update 模式：

```text
orderbook_snapshot
orderbook_delta
seq
update_subscription add_markets/delete_markets/get_snapshot
```

这比 Polymarket 的 hash/timestamp 更容易做连续性校验。对 Polymarket，可参考 Kalshi 的 seq 思路，但实际实现要用 hash/heartbeat/reconnect/REST consistency check 组合判断 gap。

### 4.4 Kalshi Demo Environment

Kalshi 提供 demo environment/mock funds。它是 integration test 好工具，但注意：

```text
demo != 本地模拟盘
demo liquidity/behavior may not reflect real market
```

---

## 5. 通用框架给我们的工程启发

### 5.1 NautilusTrader：同一策略跨 research / sim / live

NautilusTrader 最值得借鉴的是：

```text
strategy code 不要知道自己跑在 backtest/paper/live。
venue adapter 决定数据和执行。
同一套时间模型和执行语义跨环境。
```

我们的项目应定义统一接口：

```python
class Strategy:
    def on_market_data(self, ctx): ...
    def on_order_event(self, ctx): ...
    def on_timer(self, ctx): ...

class ExecutionAdapter:
    def place_order(intent): ...
    def cancel_order(order_id): ...
```

然后实现：

```text
HistoricalOrderFilledBacktestAdapter
HistoricalL2BacktestAdapter
LiveLOBPaperAdapter
MicroLiveAdapter
RealLiveAdapter
```

### 5.2 HftBacktest：maker queue / latency / no market impact

HftBacktest 的核心警告：

```text
market-data replay 不能改变历史市场；
如果你的订单足够大，会造成真实 market impact；
所以 replay 假设只适用于小单。
```

对我们意味着：

- taker 可以 book-walk，但不能假设无限 depth。
- maker fill 必须有 queue model。
- paper orders 对 public book 的影响要明确采用 residual book / self-impact convention。
- 最终必须用 micro-live 订单调参。

### 5.3 Freqtrade：backtest 后必须 dry-run

Freqtrade 的经验是：

```text
backtest 有假设；
dry-run 使用实时市场数据但不真下单；
forward testing 比历史回测更接近现实；
但 dry-run 仍然不能替代小额实盘。
```

对我们来说：

```text
historical OrderFilled/L2 backtest
    ↓
live paper/shadow mode
    ↓
micro-live probe
    ↓
limited live
```

这四阶段缺一不可。

### 5.4 Hummingbot：paper/live 安全隔离

Hummingbot 给我们的启发：

```text
paper balances 独立
paper exchange connector 独立
balance limit
kill switch
status dashboard
monitoring
```

对预测市场尤其重要，因为 resolved 后的 $1/$0 payoff 会让 inventory 风险非常非线性。

### 5.5 LEAN：模块化 engine

LEAN 的模块边界可以直接借鉴：

```text
DataFeed
TransactionHandler / FillModel
Portfolio
RealtimeHandler
ResultHandler
SetupHandler
```

预测市场版本：

```text
MarketRegistry
LOBFeed
ExecutionSimulator
Ledger
SettlementEngine
MetricsReporter
```

---

## 6. 预测市场模拟盘的特殊坑

### 6.1 新市场动态加入

不能启动时拉一次 markets 就结束。必须有常驻服务：

```text
Gamma full sync
Gamma delta polling
WS new_market
book probe
state transition
universe diff
LOB subscribe/unsubscribe
```

### 6.2 已关闭不等于已结算

```text
closed / inactive:
    停止交易

resolved:
    有 winning outcome，才能结算

archived:
    仅历史保留
```

### 6.3 YES/NO 互补价格

在 binary market 中：

```text
YES price + NO price ≈ 1
```

但注意：

- bid/ask 不一定精确互补。
- merge/redeem/negative risk 会影响组合约束。
- 不要把 NO token 按 YES midpoint 记账；这是公开 issue 里出现过的严重 bug，会把 merge profit 放大很多倍。

### 6.4 Passive Fill 幻觉

最危险的错误：

```text
我挂在 bid，后来 bid size 减少，所以我成交了。
```

这通常是错的。size 减少可能是撤单、改价、刷新、数据修正或其他人成交。

maker fill 必须保守：

```text
真实 trade evidence
queue ahead estimate
trade-through
or calibrated probabilistic model
```

### 6.5 账本必须真实

公开 issue 里另一个大坑是没有 cash tracking，导致 $3k bankroll 下出了 $175k+ 名义订单。模拟盘必须 reject insufficient funds，并跟踪 reserved cash/shares。

### 6.6 Resolution PnL 必须建模

未平 inventory 不是消失了，而是：

```text
winner -> $1
loser  -> $0
```

特别是市场做市 / parity / merge 策略，很多真实风险都在 leftover inventory 和 resolution。

### 6.7 数据质量要能阻止成交

任何这些情况，都不允许产生新 fill：

```text
NO_SNAPSHOT
STALE
GAP
DISCONNECTED
HASH_MISMATCH
TOKEN_MAPPING_UNKNOWN
MARKET_NOT_LIVE
MARKET_CLOSING
MARKET_RESOLVED
```

### 6.8 流动性稀疏

预测市场大量长尾市场盘口非常薄。回测里“用 midprice 成交”几乎一定虚高。

### 6.9 Fee / rebate / tick size 不可硬编码

Polymarket 和 Kalshi 都有 per-market tick/fee/precision 变化。所有下单前校验都应来自 metadata 或 venue API。

### 6.10 多 outcome / negative risk

不能永远假设 binary YES/NO。表结构要支持：

```text
market -> many outcome tokens
outcome_index
is_winning
neg_risk_market_id
grouped event
```

---

## 7. 推荐架构

```text
                    ┌────────────────────────┐
                    │ Dynamic Market Registry │
                    │ Gamma + WS lifecycle    │
                    └───────────┬────────────┘
                                │ universe diff
                                ▼
                    ┌────────────────────────┐
                    │ LOB Collector           │
                    │ WS + REST /book probe   │
                    └───────────┬────────────┘
                                │ BookState
                                ▼
┌──────────────┐      ┌────────────────────────┐      ┌─────────────────┐
│ Strategy     │─────▶│ Paper Execution Engine │─────▶│ Paper Ledger     │
│ OrderIntent  │      │ taker/maker/latency    │      │ cash/pos/PnL     │
└──────────────┘      └───────────┬────────────┘      └────────┬────────┘
                                  │ audit                       │
                                  ▼                             ▼
                    ┌────────────────────────┐      ┌─────────────────┐
                    │ OrderFilled / Trades    │─────▶│ Calibration      │
                    │ truth / fallback        │      │ actual vs paper  │
                    └────────────────────────┘      └─────────────────┘
```

### 7.1 目录建议

```text
quant/
  market/
    registry_daemon.py
    gamma_full_sync.py
    gamma_delta_poll.py
    ws_lifecycle_feed.py
    market_state_machine.py
    token_universe_publisher.py

  orderbook/
    polymarket_ws_feed.py
    kalshi_ws_feed.py
    local_book.py
    book_quality.py
    rest_book_probe.py
    raw_event_store.py
    checkpoint_store.py
    bbo_summary.py

  execution/
    order_intent.py
    paper_execution_engine.py
    taker_book_walk.py
    maker_queue_model.py
    residual_book.py
    latency_model.py
    fee_model.py
    validators.py

  ledger/
    account.py
    portfolio.py
    cash_reservation.py
    position_book.py
    pnl.py
    settlement_engine.py

  orderfilled/
    ingest.py
    normalize.py
    trade_prints.py
    self_trade_filter.py
    delayed_validator.py

  calibration/
    paired_probe_recorder.py
    comparator.py
    calibration_report.py

  adapters/
    live_lob_paper.py
    historical_l2_backtest.py
    orderfilled_only_backtest.py
    micro_live.py
    real_live.py

  reporting/
    dashboard_api.py
    metrics.py
    charts.py
```

---

## 8. 数据表建议

### 8.1 markets

```sql
CREATE TABLE markets (
    market_id TEXT PRIMARY KEY,
    condition_id TEXT,
    question_id TEXT,
    event_id TEXT,
    slug TEXT,
    question TEXT,

    active BOOLEAN,
    closed BOOLEAN,
    archived BOOLEAN,
    accepting_orders BOOLEAN,
    enable_order_book BOOLEAN,

    lifecycle_state TEXT,
    is_resolved BOOLEAN,
    winning_asset_id TEXT,
    winning_outcome TEXT,

    current_tick_size TEXT,
    minimum_tick_size TEXT,
    min_order_size TEXT,
    neg_risk BOOLEAN,
    fee_schedule_json TEXT,

    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    end_date TIMESTAMP,
    closed_time TIMESTAMP,
    resolved_time TIMESTAMP,

    metadata_source TEXT,
    metadata_hash TEXT,
    metadata_last_seen_at TIMESTAMP,

    book_seen_first_at TIMESTAMP,
    book_seen_last_at TIMESTAMP,
    last_l2_event_at TIMESTAMP,
    last_trade_at TIMESTAMP,

    l2_coverage_quality TEXT,
    orderfilled_coverage_quality TEXT
);
```

### 8.2 market_tokens

```sql
CREATE TABLE market_tokens (
    asset_id TEXT PRIMARY KEY,
    market_id TEXT,
    condition_id TEXT,
    outcome_name TEXT,
    outcome_index INTEGER,
    is_yes BOOLEAN,
    is_no BOOLEAN,
    is_winning BOOLEAN,
    token_decimals INTEGER,
    clob_enabled BOOLEAN,
    first_seen_at TIMESTAMP,
    last_seen_at TIMESTAMP
);
```

### 8.3 market_lifecycle_events

```sql
CREATE TABLE market_lifecycle_events (
    id BIGSERIAL PRIMARY KEY,
    market_id TEXT,
    condition_id TEXT,
    event_type TEXT,
    source TEXT,
    source_ts TIMESTAMP,
    local_receive_ts TIMESTAMP,
    old_state TEXT,
    new_state TEXT,
    reason TEXT,
    raw_payload_hash TEXT,
    raw_payload_json TEXT
);
```

### 8.4 live_l2_events_raw

```sql
CREATE TABLE live_l2_events_raw (
    id BIGSERIAL PRIMARY KEY,
    source TEXT,
    connection_id TEXT,
    market_id TEXT,
    condition_id TEXT,
    asset_id TEXT,
    event_type TEXT,
    exchange_ts TIMESTAMP,
    local_receive_ts TIMESTAMP,
    book_hash TEXT,
    side TEXT,
    price TEXT,
    size TEXT,
    best_bid TEXT,
    best_ask TEXT,
    bids_json TEXT,
    asks_json TEXT,
    raw_payload_json TEXT
);
```

### 8.5 live_book_state_status

```sql
CREATE TABLE live_book_state_status (
    asset_id TEXT PRIMARY KEY,
    market_id TEXT,
    ready BOOLEAN,
    book_quality TEXT,
    last_snapshot_ts TIMESTAMP,
    last_update_ts TIMESTAMP,
    book_age_ms INTEGER,
    has_gap BOOLEAN,
    last_book_hash TEXT,
    best_bid TEXT,
    best_ask TEXT,
    spread TEXT,
    mid TEXT,
    top5_bid_depth TEXT,
    top5_ask_depth TEXT,
    updated_at TIMESTAMP
);
```

### 8.6 paper_orders

```sql
CREATE TABLE paper_orders (
    paper_order_id TEXT PRIMARY KEY,
    strategy_id TEXT,
    market_id TEXT,
    asset_id TEXT,
    side TEXT,
    limit_price TEXT,
    size TEXT,
    tif TEXT,
    post_only BOOLEAN,

    decision_ts TIMESTAMP,
    simulated_arrival_ts TIMESTAMP,

    state TEXT,
    filled_size TEXT,
    avg_price TEXT,
    reject_reason TEXT,

    book_quality_at_arrival TEXT,
    model_version TEXT,
    config_hash TEXT
);
```

### 8.7 paper_order_audit

```sql
CREATE TABLE paper_order_audit (
    id BIGSERIAL PRIMARY KEY,
    paper_order_id TEXT,
    audit_ts TIMESTAMP,
    decision_book_event_id BIGINT,
    arrival_book_event_id BIGINT,
    source_trade_event_ids TEXT,
    best_bid TEXT,
    best_ask TEXT,
    spread TEXT,
    top_depth_snapshot_json TEXT,
    fill_explanation_json TEXT,
    data_quality_flags TEXT
);
```

### 8.8 ledger_entries

```sql
CREATE TABLE ledger_entries (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT,
    ts TIMESTAMP,
    entry_type TEXT,
    market_id TEXT,
    asset_id TEXT,
    order_id TEXT,
    cash_delta TEXT,
    reserved_cash_delta TEXT,
    position_delta TEXT,
    realized_pnl_delta TEXT,
    fee_delta TEXT,
    reason TEXT
);
```

---

## 9. 开发优先级

### Phase 1：Dynamic Market Registry

目标：

```text
持续维护 subscription_universe 和 execution_universe。
```

交付：

- Gamma full sync / delta poller。
- WS lifecycle feed。
- market_state_machine。
- clob_book_probe。
- universe diff publisher。
- registry outbox。

验收：

- 空 DB 启动后能拉取 active markets。
- 新 market 进入 `TRADABLE_PENDING_BOOK`。
- book ready 后进入 `LIVE`。
- closed 进入 `CLOSING`，不 settlement。
- resolved 进入 `RESOLVED`，写 winning outcome。
- daemon 重启幂等恢复，不重复插入。

### Phase 2：LOB Collector + Book Quality

目标：

```text
所有 execution_universe token 有 current BookState 和 book_quality。
```

交付：

- Polymarket WS feed。
- REST `/book` probe。
- LocalBook。
- raw_event_log。
- book_checkpoint。
- bbo_summary。
- gap/stale/hash mismatch detection。

验收：

- snapshot + price_change 能重建 book。
- disconnect 后不允许成交。
- REST snapshot 恢复后 book_quality 回到 READY。
- tick_size_change 能更新 validator。
- size=0 能删除价位。

### Phase 3：Taker Paper Execution

目标：

```text
实现 marketable order 的 book-walk。
```

交付：

- OrderIntent。
- validators。
- fee_model。
- taker_book_walk。
- residual_book。
- paper_orders / paper_order_events / audit。
- ledger cash reservation。

验收：

- BUY 正确吃 asks，SELL 正确吃 bids。
- FOK 不足全量整单 reject。
- FAK 部分成交后 cancel 剩余。
- tick/min size 错误 reject。
- cash 不足 reject。
- 同一时间多单不能重复吃同一 depth。

### Phase 4：Maker Paper Execution

目标：

```text
保守模拟 resting limit order 的排队和成交。
```

交付：

- maker_queue_model。
- queue_ahead estimate。
- trade evidence matching。
- cancel/expire。
- maker fill audit。

验收：

- 挂单后不会立即因为 depth 下降而 fill。
- 真实 trade print 可推进队列。
- queue_ahead 未消耗完时不 fill。
- partial fill 正确更新剩余。
- price move away 后 order 继续 resting 或按 TIF/策略 cancel。
- maker false positive fill rate 在 micro-live 校准内低于阈值。

### Phase 5：Settlement / Resolution

目标：

```text
market resolved 后完整处理 positions 和 PnL。
```

交付：

- settlement_engine。
- winning outcome ingest。
- open order cancel。
- winner/loser payout。
- realized PnL finalization。

验收：

- closed 未 resolved 不结算。
- resolved 后 winner $1、loser $0。
- settlement 重跑幂等。
- portfolio cash/positions 与 ledger entries 可对账。

### Phase 6：Calibration

目标：

```text
用真实小单校准 paper model。
```

交付：

- micro-live order recorder。
- self_trade_filter。
- delayed OrderFilled-only validator。
- live_vs_paper_comparator。
- calibration_report。

验收：

- 每个 probe 有 A/B/C 三轨结果。
- 能排除自己的成交。
- 输出 fill false positive/negative。
- 输出 price/slippage error。
- 能按 market category / liquidity / order type 分组统计。

---

## 10. 最终验收矩阵

| Case | 场景 | 必须结果 |
|---|---|---|
| 1 | 空数据库启动 | 全量 sync active markets，生成 subscription universe |
| 2 | 新 market 出现 | DISCOVERED → TRADABLE_PENDING_BOOK → book ready → LIVE |
| 3 | 新 market 没有 book | 保持 TRADABLE_PENDING_BOOK，下单返回 DATA_NOT_READY |
| 4 | WebSocket 断线 | book_quality=DISCONNECTED，不允许新成交 |
| 5 | Book stale | LIVE → STALE，execution_universe 移除，subscription 保留 |
| 6 | REST snapshot 恢复 | STALE/GAP → LIVE |
| 7 | Market closed | LIVE/STALE → CLOSING，open orders cancel/expire，不 settlement |
| 8 | Market resolved | 写 winning outcome，positions settlement，realized PnL 固化 |
| 9 | Tick size 变化 | validator 更新，旧 tick 不合规 order reject |
| 10 | Taker FOK | depth 不足整单 reject |
| 11 | Taker FAK | 部分成交，剩余 cancel |
| 12 | GTC/GTD | 可成交部分先 fill，剩余 resting |
| 13 | Post-only cross | reject |
| 14 | Maker queue | 无 trade evidence / queue 未耗尽，不得 fill |
| 15 | Cash 不足 | reject，不允许无限杠杆 |
| 16 | YES/NO 记账 | NO token 不得用 YES midpoint 估值 |
| 17 | Multi-outcome | market_tokens 支持多 outcome，不写死 YES/NO |
| 18 | Daemon 重启 | 从 DB 恢复状态，不重复创建 token/order |
| 19 | Micro-live 校准 | 输出 actual vs paper 差异报告 |
| 20 | OrderFilled-only fallback | 历史 LOB 缺失时可保守回测，并标记 evidence-constrained |

---

## 11. Go / No-Go 标准

### 可以进入 micro-live 的最低标准

```text
1. Dynamic Registry 稳定运行 24h。
2. Live BookState 覆盖目标市场，book_quality READY 占比 > 99%。
3. Taker FOK/FAK 和 GTC/GTD 单元测试全部通过。
4. Maker 模型默认保守，不能用 depth decrease 直接 fill。
5. Ledger 可以逐笔对账。
6. Resolution 能正确处理 winner/loser。
7. 所有 fill 都有 audit explanation。
8. paper/live adapter 有硬隔离，默认 paper。
```

### 可以进入小额实盘 probe 的最低标准

```text
1. 至少 100 个 paper order audit 完整。
2. 至少 20 个 micro-live taker probe。
3. taker filled_size_error / avg_price_error 在可接受范围。
4. maker false positive fill rate 明显低于阈值。
5. 没有 DATA_READY 错误仍成交的案例。
6. 没有 cash 不足仍成交的案例。
7. resolved market PnL 与 ledger 对账为 0 差异。
```

### 不允许 go-live 的红线

```text
1. 用 midprice 成交。
2. 用 depth decrease 直接判定 maker fill。
3. 没有 cash/reserved cash 账本。
4. market closed 就直接 settlement。
5. 没有 book_quality gating。
6. 没有 audit trail。
7. live/paper 凭一个配置字符串切换，缺少安全锁。
8. tick/min size/fee 硬编码。
```

---

## 12. 推荐阅读顺序

1. `agent-next/polymarket-paper-trader`  
   先看它怎么定义用户视角的 paper trader：CLI、balance、portfolio、book、limit order。

2. `sonnyfully/polymarket-bot`  
   重点看它对“paper trading 会撒谎”的承认，以及 shared infrastructure / deterministic execution / persistence。

3. `suislanchez/polymarket-kalshi-weather-bot/issues/65`  
   必看反例：为什么 maker fill 不能靠 `depth_consumed`。

4. `Oddpool/PredictionMarketBench` 和论文  
   看预测市场 benchmark 如何把 orderbook/trades/settlement 包成 episode，并给 agent 提供工具接口。

5. HftBacktest docs  
   学习 queue model、latency、market-data replay 的边界。

6. NautilusTrader docs  
   学习同一策略跨 research / sim / live 的 event-driven architecture。

7. Polymarket 官方 docs  
   实现前必须逐页确认：Market Channel、/book、Orders、Resolution、Fees、Error Codes。

8. Kalshi docs  
   学习 binary orderbook YES/NO bids、fixed-point、snapshot+delta+seq 这种更标准的 orderbook 模型。

---

## 13. 对我们项目的最终建议

我们不要照搬任一项目。推荐组合：

```text
产品/CLI 体验：
    借鉴 polymarket-paper-trader

事件驱动和 adapter：
    借鉴 NautilusTrader + prediction-market-backtesting

HFT 盘口和 maker queue：
    借鉴 HftBacktest

全栈服务化：
    借鉴 Polybot / Homerun

预测市场 benchmark/验收：
    借鉴 PredictionMarketBench

反例防坑：
    借鉴 suislanchez issue #65

venue 事实：
    以 Polymarket / Kalshi 官方 docs 为准
```

最终原则：

```text
1. 没有可用 book，就不模拟成交。
2. 没有真实 trade evidence 或保守 queue model，就不乐观模拟 maker fill。
3. 没有账本，就没有可信 PnL。
4. 没有 resolution，就没有最终 realized PnL。
5. 没有 micro-live 校准，就不能相信 paper。
6. 没有 audit，就不能用模拟盘做 go-live 决策。
```

---

## 附录 A：可直接给 Codex 的任务摘要

```text
Build a prediction-market paper trading system inspired by public Polymarket/Kalshi simulators and HFT backtesting frameworks.

Core requirements:
1. Dynamic market registry with lifecycle states.
2. Subscription universe and execution universe separation.
3. Live L2 order book collector with raw event log, checkpoints, summaries, and book quality gating.
4. Taker execution via book walk at simulated arrival time.
5. Maker execution via conservative queue model and trade evidence; never fill solely from depth decrease.
6. Full order state machine and audit trail.
7. Full ledger: cash, reserved cash, positions, realized/unrealized PnL, fees, settlement.
8. Prediction-market settlement: closed != resolved; winner pays $1/share, loser pays $0/share.
9. OrderFilled/trade tape layer for historical fallback and calibration.
10. Micro-live paired probe validation.
11. Same strategy interface across backtest, live paper, micro-live, and live adapters.
12. No hard-coded tick size, fee, min order size, or binary-only assumptions.
```

---

## 附录 B：最小指标面板

```text
Market Registry:
    active markets
    live tokens
    pending book tokens
    stale/gap tokens
    resolved today

Book Collector:
    ws connected
    messages/sec
    book_age_p50/p95/p99
    gap count
    snapshot reset count

Paper Execution:
    orders created
    orders rejected
    fill rate
    avg slippage
    false positive fill estimate
    stale-data rejection count

Ledger:
    cash
    reserved cash
    gross exposure
    realized pnl
    unrealized pnl
    fees
    settlement pnl

Calibration:
    micro-live probes
    actual vs paper fill error
    taker price error ticks
    maker Brier score
    overfill/underfill rate
```

---

## 附录 C：来源链接清单

### GitHub / 工程项目

- https://github.com/agent-next/polymarket-paper-trader
- https://github.com/evan-kolberg/prediction-market-backtesting
- https://github.com/braedonsaunders/homerun
- https://github.com/ent0n29/polybot
- https://github.com/0xrsydn/polymarket-crypto-toolkit
- https://github.com/runesleo/polymarket-toolkit
- https://github.com/sonnyfully/polymarket-bot
- https://github.com/KaustubhPatange/polymarket-trade-engine
- https://github.com/Polymarket/agents
- https://github.com/Oddpool/PredictionMarketBench
- https://github.com/Viprasol-Tech/kalshi-trading-bot
- https://github.com/ryanfrigo/kalshi-ai-trading-bot
- https://github.com/danrobinson/prediction-market-challenge
- https://github.com/octavi42/prediction-market-maker
- https://github.com/nautechsystems/nautilus_trader
- https://github.com/nkaz001/hftbacktest
- https://github.com/freqtrade/freqtrade
- https://github.com/hummingbot/hummingbot
- https://github.com/mementum/backtrader
- https://github.com/QuantConnect/Lean

### 官方文档

- https://docs.polymarket.com/market-data/fetching-markets
- https://docs.polymarket.com/market-data/websocket/market-channel
- https://docs.polymarket.com/api-reference/market-data/get-order-book
- https://docs.polymarket.com/trading/orders/overview
- https://docs.kalshi.com/api-reference/market/get-market-orderbook
- https://docs.kalshi.com/getting_started/orderbook_responses
- https://docs.kalshi.com/websockets/orderbook-updates
- https://docs.kalshi.com/getting_started/fixed_point_migration
- https://docs.kalshi.com/getting_started/demo_env

### Papers / Blog / Issues

- https://arxiv.org/abs/2602.00133
- https://arxiv.org/html/2402.17359v1
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6583921
- https://www.jasss.org/21/1/7.html
- https://people.cs.vt.edu/~sanmay/papers/predmarkets.pdf
- https://github.com/suislanchez/polymarket-kalshi-weather-bot/issues/65
- https://hackingthemarkets.com/kalshi-live-order-book-watcher-with-python/
- https://www.polytest.io/docs/guides/how-to-backtest-prediction-markets
- https://www.paradigm.xyz/2024/11/pm-amm
