# Polymarket CTF 操作与账户经济开发指导

> **历史需求基线，不是当前缺口清单。** 本文保留 CTF、fee、rebate/reward 的原始官方语义与开发指导。其中“尚未实现”描述属于写作时快照；当前状态以 `docs/模拟盘/polymarket_retail_paper_trading_product_idea.md` 第 26 节、`reports/simulator_capability_audit.{json,md}` 和 `runtime_outputs/simulator_closure/current/closure.{json,md}` 为准。

我重新核对了当前 Polymarket 官方文档、官方合约仓库、CTF Exchange V2、Conditional Tokens 合约，以及公开的预测市场模拟器。研究后的判断是：

> **需要考虑，而且 BUY/SELL、Split/Merge/Redeem、Fee、Rebate/Reward 分别属于三种不同的模拟对象，不能全部塞进撮合引擎，也不能全部混成一个 PnL。**

Polymarket 官方把 Split、Merge、Redeem 定义为独立于订单簿的持仓操作；官方 `ctf-exchange-v2` 又表明，交易撮合底层本身存在 `COMPLEMENTARY`、`MINT`、`MERGE` 三种结算路径，其中两个 BUY 可以通过拆分抵押品生成互补 token，两个 SELL 可以通过合并互补 token 释放抵押品。因此，完整模拟 Polymarket 不能只停留在“用户发出 BUY/SELL，然后修改现金和单个 token 数量”。([Polymarket Documentation][1])

公开的 PredictionMarketBench 也把订单簿、成交、费用和结算放在同一确定性回放 episode 中，说明专业预测市场模拟器至少需要同时覆盖执行、费用和最终结算，而不是只计算买卖价格。([arXiv][2])

---

# 一、先明确：模拟盘实际上有三个层级

## 层级一：订单执行模拟器

回答：

```text
订单会不会成交？
成交多少？
成交价格是多少？
费用是多少？
订单最终状态是什么？
```

必须包括：

```text
BUY / SELL
GTC / GTD / FAK / FOK
maker / taker
platform fee
builder fee（存在 builder code 时）
订单 finality
```

## 层级二：持仓与抵押品模拟器

回答：

```text
pUSD 如何变成 YES/NO？
完整 token 组合如何变回 pUSD？
市场结算后如何获得 payout？
资金什么时候真正可重新使用？
```

必须包括：

```text
SPLIT
MERGE
REDEEM
NEG_RISK_CONVERT
operation finality
allowance / approval
pending collateral
```

## 层级三：完整账户收益模拟器

回答：

```text
除了交易盈亏，账户最后实际增加了多少钱？
```

必须包括：

```text
taker fee
builder fee
maker rebate
taker rebate
liquidity rewards
holding rewards
referral / sponsor rewards（产品范围包含时）
dispute bond / bounty（参与 resolution 时）
bridge / withdrawal costs（模拟完整资金账户时）
```

因此，**Fee 是成交经济的一部分；Split/Merge/Redeem 是持仓和抵押品生命周期的一部分；Rebate/Reward 是延迟发生的账户现金流。三者都应该考虑，但应由不同模块负责。**

---

# 二、根据你上传的审计，你当前真实缺口在哪里

你当前并不是完全没有 Split/Merge/Redeem。

上传的审计显示：

* Taker fee 正式路径已经实现；
* Maker 平台交易费为零已经实现；
* Split 已实现；
* Merge 已实现；
* Redeem 已实现并有真实到账证据；
* standard negative-risk conversion 已实现；
* augmented negative-risk 尚未实现；
* Maker rebate、Taker rebate、Liquidity Rewards、Holding Rewards 尚未实现。 

审计对当前状态的直接结论也是：交易账本和平台 taker fee 已经比较完整，但 Maker rebate、Taker rebate 和其他 reward 没有进入账户总收益，所以当前还不能声称模拟账户总收益与 Polymarket 官网一致。

所以你下一步不是简单地“再添加一个 `split()` 和 `redeem()` 函数”，而是要完成：

```text
1. 把 Split/Merge/Redeem 变成完整的可提交、可等待、可失败、
   可恢复、可对账的 Position Operation。

2. 在 BUY/SELL 撮合内部记录真实 CTF settlement path：
   COMPLEMENTARY / MINT / MERGE。

3. 建立独立的 Fee + Rebate + Reward 经济账本。

4. 将 trading PnL 与 account total return 分开。
```

---

# 三、Split 为什么必须模拟

官方定义：

```text
1 pUSD
→ 1 YES token
+ 1 NO token
```

Split 的主要用途是为做市准备双边 inventory。Polymarket 官方 Market Making 文档也明确建议做市商在 token 不足时 Split 更多 pUSD，以便继续报价。([Polymarket Documentation][1])

## Split 不是交易

Split 不应该：

```text
读取 LOB
产生 maker/taker fill
产生 spread PnL
收取 taker fee
```

它是资产转换：

```text
cash -= amount
YES quantity += amount
NO quantity += amount
```

通常 Split 当刻不产生 realized PnL，只改变资产形式。

## 但 Split 会直接影响策略能力

假设做市策略有 100 pUSD，但没有 YES/NO token。

它想同时挂：

```text
SELL YES
SELL NO
```

如果模拟盘没有 Split，只能错误地认为：

```text
没有 token → 无法挂 SELL
```

但真实做市商可以：

```text
Split 100 pUSD
→ 获得 100 YES + 100 NO
→ 同时向两边报价
```

因此，对 maker、complete-set、套利策略来说，Split 是核心交易基础设施，不是附属功能。

## Split 应有自己的状态机

当前官方客户端通过 Relayer 或 Builder 接口执行这些持仓操作，提交后先获得 transaction ID，再等待广播、链上交易哈希和最终状态，因此不能永远模拟为一个瞬时数据库更新。([Polymarket Documentation][1])

建议状态：

```text
CREATED
PREFLIGHT_OK
COLLATERAL_RESERVED
SUBMITTED
BROADCAST_PENDING
MINED
CONFIRMED
FAILED
REVERSED
```

处理规则：

```text
CREATED:
    检查 market type、condition、pUSD、allowance。

COLLATERAL_RESERVED:
    amount 不再允许被其他订单使用。

SUBMITTED:
    产生 operation_id / transaction_id。

CONFIRMED:
    扣除 pUSD；
    增加 YES 和 NO；
    写完整集 lot。

FAILED:
    释放 pUSD reservation；
    不修改 token balance。
```

Conditional Tokens 合约本身具有原子性：抵押品转移、token burn/mint 任意一步失败，整个交易都会回滚。模拟盘也应保持同样的原子语义。([GitHub][3])

---

# 四、Merge 为什么必须模拟

官方定义：

```text
1 YES + 1 NO
→ 1 pUSD
```

Merge 用来释放被完整 token 组合占用的抵押品。([Polymarket Documentation][1])

## Merge 不只是“反向 Split”

Merge 会影响真实 realized PnL。

例如：

```text
买入 YES：0.48
买入 NO： 0.49

总成本：0.97
Merge 后获得：1.00 pUSD
```

则：

```text
Merge realized PnL = 1.00 - 0.97 = 0.03
```

所以 Merge 需要读取 YES 和 NO 各自的 cost basis，而不能只做：

```text
YES -= 1
NO  -= 1
cash += 1
```

否则账本现金正确，但 realized PnL 归因可能错误。

## Merge 必须处理 reservation

当 YES 或 NO 正被 SELL order 占用时：

```text
available_yes = position_yes - reserved_yes
available_no  = position_no  - reserved_no
mergeable_qty = min(available_yes, available_no)
```

不能把已经承诺给 resting order 的 token 同时拿去 Merge。

## 建议保存完整集来源

新增：

```text
complete_set_lots
    lot_id
    condition_id
    quantity
    yes_cost_basis
    no_cost_basis
    joint_cost_basis
    provenance
    created_by
    created_at
```

`provenance` 至少包括：

```text
SPLIT
SEPARATE_MARKET_BUYS
CTF_MINT_MATCH
NEG_RISK_CONVERSION
TRANSFER_IN
```

这样才能正确解释不同来源 token 的 Merge PnL。

---

# 五、Redeem 为什么是所有策略都必须有的

官方定义是：市场 final resolution 后，winning token 按 payout 兑换为 pUSD；失败 token payout 为零。([Polymarket Documentation][1])

Redeem 与 SELL 完全不同：

```text
SELL：
    在订单簿中将 token 卖给其他交易者。

REDEEM：
    市场结算后向 Conditional Tokens 合约领取 collateral。
```

## Redeem 应分两阶段

建议：

```text
RESOLUTION_FINAL
    ↓
REDEEM_RECEIVABLE
    ↓
REDEEM_SUBMITTED
    ↓
REDEEM_CONFIRMED
    ↓
cash available
```

在 resolution final 时：

```text
payout_receivable =
    Σ token_quantity_i × payout_vector_i
```

但真正 redeem confirmed 前，不一定应当把它作为可下单现金。

## 防止 PnL 重复计算

必须选择一种一致的会计政策。

### 方案 A：Resolution 时实现 PnL

```text
RESOLUTION_FINAL:
    unrealized → realized
    生成 payout receivable

REDEEM_CONFIRMED:
    receivable → cash
    不再产生第二次 PnL
```

### 方案 B：Redeem 时实现 PnL

```text
RESOLUTION_FINAL:
    只确定 payout truth

REDEEM_CONFIRMED:
    payout - cost basis → realized PnL
```

不能两次都记 realized PnL。

当前官方 activity 对 REDEEM 已按 outcome 分行记录；失败 outcome 的 `usdcSize=0`，同一交易所有行的 `usdcSize` 合计才是总 payout。因此你的 reconciliation 也应按 transaction hash 聚合，而不是只读取一行。([Polymarket Documentation][4])

---

# 六、还要区分“用户 Split/Merge”和“撮合内部 MINT/MERGE”

这是最容易漏掉的一层。

官方 CTF Exchange V2 的撮合结算有三条路径：

```text
COMPLEMENTARY
    BUY 对 SELL
    token 与 collateral 直接交换

MINT
    两个互补 outcome 的 BUY 订单相互匹配
    合约 Split collateral
    向双方分别发放 outcome token

MERGE
    两个互补 outcome 的 SELL 订单相互匹配
    合约 Merge token
    向双方释放 collateral
```

([GitHub][5])

用户看到的订单仍然只是：

```text
BUY YES
BUY NO
SELL YES
SELL NO
```

所以不应把 `MINT` 和 `MERGE` 暴露成新的 CLOB order type；但每笔模拟 fill 应增加：

```text
settlement_match_type:
    COMPLEMENTARY
    MINT
    MERGE
    UNKNOWN
```

这有三个作用：

1. 与真实 `OrderFilled / OrdersMatched / FeeCharged` 链上事件对账；
2. 验证 collateral/token 守恒；
3. 解释为什么两个 BUY 或两个 SELL 也能形成有效的链上结算。

建议新增：

```text
quant/simulator/ctf/
  match_settlement_classifier.py
  complementary_settlement.py
  mint_settlement.py
  merge_settlement.py
  settlement_conservation.py
```

---

# 七、Fee 是撮合层必须实现的，而不是事后统一扣一次

官方当前规则是：

```text
fee = shares × feeRate × price × (1 - price)
```

Fee 在 match 时计算；Maker 不支付平台交易费，Taker 支付；费用保留 5 位小数，小于最小单位的费用可能归零。不同 market category 使用不同参数，且这些参数会变化。([Polymarket Documentation][6])

因此费用必须：

```text
按每个 fill level 计算
而不是按 order 最终 VWAP 粗略计算
```

例如：

```text
50 shares @ 0.40
50 shares @ 0.42
```

应计算：

```text
fee_1 = fee(50, 0.40)
fee_2 = fee(50, 0.42)

total_fee = rounded(fee_1) + rounded(fee_2)
```

不能只用：

```text
fee(100, avg_price=0.41)
```

除非真实 SDK/合约确认两者舍入语义完全等价。

## Fee 模块应包含

```text
quant/simulator/economics/
  fee_engine.py
  fee_schedule_registry.py
  fee_rounding.py
  fee_reconciler.py
  builder_fee_engine.py
```

每条 fee 记录：

```text
fee_charge_id
fill_id
asset_id
condition_id
liquidity_role
price
shares
platform_fee_rate
platform_fee
builder_fee_rate
builder_fee
rounding_policy
economics_regime_id
source
```

## Builder fee 也需要考虑

CLOB V2 允许 builder 在订单中附加 builder code，并收取基于 trade notional 的 builder fee。它与 platform fee 是叠加关系，不是替代关系。([Polymarket Documentation][7])

所以 BUY reservation 应按：

```text
all_in_max_spend =
    trade_notional
    + maximum_platform_fee
    + maximum_builder_fee
```

否则 paper 账户可能接受一笔真实账户资金不足的订单。

---

# 八、Rebate 需要模拟，但绝不能在 fill 时直接冲减 fee

这是最关键的会计区别。

## 错误实现

```text
taker fee = 1.00
预计 rebate = 0.25

fill 时直接只扣 0.75
```

这是错误的，因为 rebate：

* 不一定即时产生；
* 不一定达到最低支付额；
* 可能按日支付；
* 可能依赖全市场其他人的成交；
* 可能被调整、取消或 clawback；
* 在收到之前不能作为可用现金。

正确做法：

```text
fill:
    立即扣完整 fee

later:
    记录 rebate accrual

payout received:
    再增加 confirmed cash
```

---

# 九、Maker Rebate 应怎样模拟

当前官方 Maker Rebates：

* 只奖励真正被 taker 成交的 maker liquidity；
* 每天以 pUSD 分配；
* 最低累计 $1 才支付；
* 每个 market 单独计算；
* 按自己的 fee-equivalent 占整个 market fee-equivalent 的比例获得 rebate pool。([Polymarket Documentation][8])

公式是：

```text
your_fee_equivalent =
    Σ filled_shares × feeRate × p × (1-p)

maker_rebate =
    your_fee_equivalent
    / total_market_fee_equivalent
    × market_rebate_pool
```

## 为什么 paper 模拟无法天然精确计算

你知道：

```text
自己的模拟 maker fill
```

但未必知道：

```text
当天该 market 所有真实 maker 的 total_fee_equivalent
```

更重要的是，你的模拟 maker order 现实中并不存在，它如果真实存在，也可能改变其他 maker 的成交份额。

所以 Maker rebate 应同时输出：

```text
maker_rebate_estimated
maker_rebate_lower_bound
maker_rebate_upper_bound
maker_rebate_official_received
```

正式现金只认：

```text
maker_rebate_official_received
```

模拟策略主报告建议默认：

```text
conservative_pnl_ex_rebate
```

而不是依赖乐观的 rebate 预测。

---

# 十、Taker Rebate 应怎样模拟

当前官方 Taker Rebate Program 使用：

```text
rolling 30-day weighted volume
category weight
entry price
tier threshold
daily tier update
daily pUSD payout
```

Tier 达到后，只对之后的成交生效，不会对之前成交追溯补发；最低 $1 才支付。([Polymarket Documentation][9])

因此需要：

```text
quant/simulator/economics/taker_rebate/
  weighted_volume.py
  rolling_30d_window.py
  tier_state_machine.py
  rebate_accrual.py
  level_up_bonus.py
```

状态必须按 point-in-time 计算：

```text
2026-08-01 之前的成交
不能使用 2026-08-05 才达到的 tier
```

每个 fill 绑定：

```text
tier_at_fill
category_weight_at_fill
rule_version_at_fill
weighted_volume_delta
rebate_rate_at_fill
```

---

# 十一、Liquidity Rewards 与 Maker Rebate 不是同一种东西

Maker Rebate 奖励的是：

```text
真正成交的 maker liquidity
```

Liquidity Rewards 奖励的是：

```text
持续挂在盘口上的合格流动性
```

当前官方 Liquidity Rewards 根据：

* order size；
* 距离 midpoint 的 spread；
* 双边报价；
* market-specific minimum size；
* maximum spread；
* 所有 maker 之间的 normalized score；
* 周期内多次采样；

计算日度奖励。([Polymarket Documentation][10])

所以 maker queue 模型不能替代 liquidity reward 模型。

需要保存：

```text
reward_order_samples
    sample_ts
    order_id
    condition_id
    midpoint
    side
    price
    size
    qualifying_size
    max_spread
    raw_score
    normalized_score
    reward_regime_id
```

对于 hypothetical paper maker，因为模拟订单没有真实进入官方 reward sampling，必须标记：

```text
COUNTERFACTUAL_ESTIMATE
```

不能标记为官方 earned。

---

# 十二、Holding Rewards 也应该进入账户总收益，但不能硬编码

Holding Reward 属于持仓现金流，不是交易 PnL。

官方 Help Center 当前写的是 3.25% 年化、每小时随机采样、每日支付；但当前核心 Positions 文档仍写 4.00%。这两个官方页面在当前检索时存在不一致。([Polymarket Help Center][11])

这正好证明：

```text
绝不能把 reward rate 写死在代码中
```

必须建立：

```text
reward_schedule_registry
    program_type
    eligible_markets
    annual_rate
    effective_from
    effective_to
    source_url_hash
    fetched_at
    status
```

并区分：

```text
EXPECTED_HOLDING_REWARD
OFFICIAL_ACCRUED
OFFICIAL_RECEIVED
```

对于历史 hypothetical 策略，因为官方使用随机小时采样，你通常只能：

```text
计算期望值
或者
使用固定 seed 做可重复研究采样
```

不能声称逐日精确等于官网。

---

# 十三、官方 API 已经提供了部分奖励真相源

当前 Polymarket API 已经公开：

```text
maker 当前 rebated fees
active reward configurations
market raw rewards
user earnings by date
user total earnings
user reward percentages
user earnings + market configuration
```

([Polymarket Documentation][12])

用户 activity 还明确包含：

```text
REDEEM
REWARD
CONVERSION
DEPOSIT
WITHDRAWAL
YIELD
MAKER_REBATE
TAKER_REBATE
REFERRAL_REWARD
```

([Polymarket Documentation][13])

因此建议建立三层 truth：

```text
第一层：官方账户 activity / earnings API
第二层：链上 transaction / CTF events
第三层：本地经济模型估算
```

优先级：

```text
OFFICIAL_RECEIVED
    > ONCHAIN_CONFIRMED
    > OFFICIAL_ACCRUAL
    > MODEL_ESTIMATED
```

---

# 十四、建议新增统一的 Economic Ledger

不要继续把所有东西都放在：

```text
paper_fills.fee
paper_positions.realized_pnl
```

建议新增：

```text
account_economic_events
    event_id
    account_id
    strategy_id
    condition_id
    asset_id

    event_type
    amount
    currency
    quantity

    status
    effective_ts
    confirmed_ts

    source
    source_event_id
    source_tx_hash

    economics_regime_id
    model_version
    idempotency_key
```

`event_type` 包括：

```text
TRADE_BUY_CASHFLOW
TRADE_SELL_CASHFLOW

PLATFORM_TAKER_FEE
BUILDER_FEE

SPLIT_COLLATERAL_DEBIT
SPLIT_TOKEN_CREDIT
MERGE_TOKEN_DEBIT
MERGE_COLLATERAL_CREDIT

REDEEM_RECEIVABLE
REDEEM_CASH_RECEIVED

MAKER_REBATE_ESTIMATED
MAKER_REBATE_RECEIVED

TAKER_REBATE_ESTIMATED
TAKER_REBATE_RECEIVED

LIQUIDITY_REWARD_ESTIMATED
LIQUIDITY_REWARD_RECEIVED

HOLDING_REWARD_ESTIMATED
HOLDING_REWARD_RECEIVED

REWARD_CLAWBACK
OPERATION_COST
```

状态：

```text
ESTIMATED
ACCRUED
PAYABLE
RECEIVED
FAILED
VOIDED
CLAWED_BACK
```

---

# 十五、PnL 最终应该拆成哪些口径

至少输出以下口径：

```text
gross_execution_pnl
platform_taker_fee_paid
builder_fee_paid
net_trading_pnl

split_merge_realized_pnl
settlement_realized_pnl

maker_rebate_estimated
maker_rebate_received

taker_rebate_estimated
taker_rebate_received

liquidity_reward_estimated
liquidity_reward_received

holding_reward_estimated
holding_reward_received

confirmed_account_return
estimated_account_return
```

公式建议：

```text
net_trading_pnl =
    gross_execution_pnl
    - platform_taker_fee_paid
    - builder_fee_paid
```

```text
confirmed_account_return =
    net_trading_pnl
    + split_merge_realized_pnl
    + settlement_realized_pnl
    + confirmed_rebates
    + confirmed_rewards
    - confirmed_operation_costs
```

```text
estimated_account_return =
    confirmed_account_return
    + unconfirmed_reward_estimates
```

UI 和报告不能只显示一个模糊的：

```text
paper_pnl
```

---

# 十六、你现在最应该交给 Codex 的任务顺序

## PR-1：CTF Operation 与撮合结算审计

重点不是重写已有 Split/Merge/Redeem，而是检查：

```text
SPLIT/MERGE/REDEEM 是否都进入统一风险、reservation、
finality、ledger、replay 和 reconciliation。

BUY/SELL fill 是否保存：
    COMPLEMENTARY / MINT / MERGE settlement type。

standard / neg-risk 是否使用正确 adapter。

operation pending 时资金/token 是否被冻结。

失败、重复、重启是否幂等。
```

## PR-2：Complete-set Cost Basis

实现：

```text
complete_set_lots
joint cost basis
paired token provenance
merge realized PnL
split inventory attribution
redeem basis removal
```

## PR-3：Fee Engine 最终统一

检查并消除所有旧线性 fee 路径：

```text
每 fill 计算
point-in-time schedule
5-decimal rounding
fee-free market
builder fee
all-in reservation
official fill reconciliation
```

你的审计已经指出正式路径使用官方动态曲线，但旧模块中还存在不同的线性公式，因此必须保证旧模块无法进入权威执行路径。

## PR-4：Reward Ledger 与官方数据摄取

先实现通用框架：

```text
reward_schedule_registry
reward_accruals
reward_payouts
official_reward_ingest
reward_reconciliation
reward_clawback
```

先不要急着写复杂估算公式。

## PR-5：Maker Rebate

实现：

```text
fee-equivalent
per-market pool
estimated share
official maker rebate ingest
daily minimum payout
estimate vs received
```

## PR-6：Taker Tier Rebate

实现：

```text
rolling 30d weighted volume
point-in-time tier
category weights
forward-only activation
daily payout
level-up bonus
```

## PR-7：Liquidity 与 Holding Rewards

两者分别建模，不能复用 maker rebate。

## PR-8：Account Return Report

输出：

```text
trading-only
confirmed account return
estimated full account return
reward coverage
unmodeled cashflows
```

---

# 十七、必须通过的验收不变量

## Split

```text
cash_delta = -q
yes_delta = +q
no_delta = +q
total collateral-backed value conserved
realized_pnl = 0，除非存在明确 operation cost
```

## Merge

```text
yes_delta = -q
no_delta = -q
cash_delta = +q
realized_pnl = q - removed_joint_cost_basis
```

## Redeem

```text
token_delta_i = -redeemed_qty_i
receivable = Σ qty_i × payout_i
confirmed_cash_delta = receivable
不能重复实现 PnL
```

## Fee

```text
maker platform fee = 0
taker fee 按 fill price/size 计算
fee-free market = 0
重复 fill event 不得重复扣费
FOK rollback 不得残留 fee
```

## Rebate

```text
ESTIMATED 不得增加 cash_available
ACCRUED 不得增加 confirmed cash
只有 RECEIVED 增加 cash
最低支付额以下继续累计
clawback 使用补偿分录，不能删除旧记录
```

## Replay

```text
相同 market rules snapshot
+ 相同 fills
+ 相同 operations
+ 相同 reward inputs
→ 相同 final ledger hash
```

---

# 最终判断

从专业角度看：

| 行为                             |             是否应进入模拟盘 |   优先级 |
| ------------------------------ | -------------------: | ----: |
| Taker fee                      |                   必须 |    P0 |
| Builder fee                    |  使用 builder code 时必须 | P0/P1 |
| Redeem                         |             所有持仓策略必须 |    P0 |
| Split                          |        做市、完整集、库存策略必须 |    P0 |
| Merge                          |         完整集套利、资本释放必须 |    P0 |
| CTF MINT/MERGE settlement path |          做链上等价和审计时必须 |    P0 |
| Maker rebate                   |       评价 maker 净收益必须 |    P1 |
| Taker rebate                   |          评价账户真实净费用必须 |    P1 |
| Liquidity Rewards              |            评价做市总收益必须 |    P1 |
| Holding Rewards                |          评价长期持仓总收益必须 |    P1 |
| Referral/Sponsor               |         复制官网账户总收益时需要 |    P2 |
| Dispute bond/bounty            | 模拟 resolution 参与者时需要 |    P2 |
| Bridge/withdrawal              |          模拟完整资金账户时需要 |    P2 |

你原先的 Live LOB、OrderFilled-only 和 micro-live 三轨结构不需要改变；这些新的持仓操作和账户经济现金流应作为独立层叠加在其上。

最重要的架构原则是：

> **BUY/SELL 决定成交；Split/Merge/Redeem 决定 token 和 collateral 如何转换；Fee 决定成交即时成本；Rebate/Reward 决定之后收到的账户现金流。只有把这四件事分别建模、分别记账、最后再汇总，模拟盘的账户收益才可能接近 Polymarket 官网。**

[1]: https://docs.polymarket.com/trading/positions/manage "Manage Positions - Polymarket Documentation"
[2]: https://arxiv.org/abs/2602.00133?utm_source=chatgpt.com "PredictionMarketBench: A SWE-bench-Style Framework for Backtesting Trading Agents on Prediction Markets"
[3]: https://github.com/gnosis/conditional-tokens-contracts/blob/master/contracts/ConditionalTokens.sol?utm_source=chatgpt.com "ConditionalTokens.sol"
[4]: https://docs.polymarket.com/changelog/predictions?utm_source=chatgpt.com "Predictions Changelog"
[5]: https://github.com/Polymarket/ctf-exchange-v2/blob/main/CLAUDE.md?utm_source=chatgpt.com "CLAUDE.md - Polymarket/ctf-exchange-v2"
[6]: https://docs.polymarket.com/trading/fees "Fees - Polymarket Documentation"
[7]: https://docs.polymarket.com/programs/builders/fees?utm_source=chatgpt.com "Builder Fees"
[8]: https://docs.polymarket.com/programs/maker-rebates "Maker Rebates Program - Polymarket Documentation"
[9]: https://docs.polymarket.com/programs/taker-rebates?utm_source=chatgpt.com "Taker Rebate Program"
[10]: https://docs.polymarket.com/programs/liquidity-rewards?utm_source=chatgpt.com "Liquidity Rewards - Polymarket Documentation"
[11]: https://help.polymarket.com/en/articles/13364459-holding-rewards?utm_source=chatgpt.com "Holding Rewards"
[12]: https://docs.polymarket.com/api-reference/rebates/get-current-rebated-fees-for-a-maker "Get current rebated fees for a maker - Polymarket Documentation"
[13]: https://docs.polymarket.com/api-reference/core/get-user-activity?utm_source=chatgpt.com "Get user activity"
