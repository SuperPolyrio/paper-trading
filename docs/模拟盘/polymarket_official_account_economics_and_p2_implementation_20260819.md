# Polymarket 官方账户经济与 P2 行为实施记录

实施日期：2026-08-19

## 1. 官方 Reward / Activity 同步

入口：

- `quant/simulator/rewards/official_reward_client.py`
- `quant/simulator/rewards/official_account_sync.py`
- `quant/simulator/rewards/official_sync_cli.py`
- `scripts/run_official_account_sync.sh`
- `deploy/systemd/poly-quant-official-account-sync.service`

同步内容：

- Data API activity：trade、split、merge、redeem、conversion、deposit、withdrawal、reward、yield、maker rebate、taker rebate、referral reward。
- CLOB：maker rebate 与 authenticated user earnings。
- CLOB user earnings 同时拉取 detail 与 `/rewards/user/total`，按 reward asset 对账；二者不一致时该日标记 `SOURCE_INCOMPLETE`，不允许生成金额校准 PASS。
- Bridge API：按 opaque cursor 完整遍历已配置 bridge address 的状态历史。
- 每条官方 activity 保存 source event id、event date、rule version、raw payload、SHA256、交易哈希和 observed time。
- API offset 达到上限时自动拆分时间窗，不允许静默截断；GET 对 429/5xx/连接错误做有限重试。
- 同步 run 与每个 stream 的 checkpoint 持久化；进程中断后的 RUNNING run 会转为 FAILED，下一轮继续。

现金规则：

- ESTIMATED、ACCRUED、PAYABLE 不改变可用现金。
- Reward 只有 RECEIVED 且具有真实 transaction hash 才增加现金。
- Deposit、withdrawal、bridge 只有 CONFIRMED/COMPLETED 才影响现金。
- 幂等键、source event id 和 `cash_applied` 三层防止重复入账。
- 现金流重放同时校验 account、strategy、source、source event、condition/asset 和已知 transaction hash；相互冲突的终态 fail-closed，非终态不允许倒退。
- 模型估算与官方 payout 分别保留；maker/liquidity/sponsor/dispute 按 condition-day，taker/holding/referral 按 account-day 聚合对账，避免错误地要求单条模型 event id 匹配官方 payout id。
- 对账状态区分 `UNMODELED_OFFICIAL`、`WAITING_FOR_OFFICIAL_EVIDENCE`、`BELOW_MINIMUM_PAYOUT`、`PASS` 和 `MISMATCH`。
- CLOB 的 `asset_address` 按官方 schema 保存为 reward asset，不再误当成 market outcome `asset_id`。
- taker rebate 与 referral 规则从本地官方文档快照生成带 SHA256 的版本化 rule；holding reward 官方文档中 3.25%/4% 冲突被同时保留，未擅自选值。

常驻服务：

```text
poly-quant-official-account-sync.service
interval: 900 seconds
lookback: 72 hours
proxy: explicit process-local 127.0.0.1:17893
```

服务不会修改 Clash 全局状态，也没有下单入口。

## 2. 真实到账校准

已对 Rabby 授权对应的 Polymarket proxy/funder address 做完整只读历史回填：

- activity：56 条。
- TRADE：47 条，全部有交易哈希。
- REDEEM：5 条，全部有交易哈希。
- DEPOSIT：4 条，全部有交易哈希。
- confirmed deposit cash：337.743765。
- maker/taker rebate、reward、yield、referral 的真实 payout 样本：0。

因此最新 reward calibration 的正式状态是 `NO_ELIGIBLE_ACTIVITY`。这不是失败，也不是 PASS；它表示当前钱包没有对应的模型 accrual，也没有真实官方 payout。如果已有模型 accrual 但官方尚未到账，状态会是 `WAITING_FOR_OFFICIAL_EVIDENCE` 或 `BELOW_MINIMUM_PAYOUT`；隔离 fixture 只验证代码和数据库通路，不作为真实 payout 证据。

每轮同步会刷新：

- `runtime_outputs/official_account_economics/account-return-YYYY-MM-DD.json`
- `runtime_outputs/official_account_economics/reward-calibration-YYYY-MM-DD.json`

## 3. Augmented Negative Risk

入口：`quant/simulator/operations/augmented_neg_risk.py`

已实现：

- 识别 Gamma `enableNegRisk + negRiskAugmented`。
- 将 named、placeholder、Other outcome 标准化为不可变版本。
- placeholder clarification 创建新版本，不覆写历史。
- placeholder 与 Other 默认不可交易；只有 named outcome 可进入交易。
- 转换矩阵燃烧源 outcome 的 NO，并为其他 active outcome 生成 YES。
- 对每个可能 winner 检查 token/payoff 守恒。
- 生成标准 `PositionOperationIntent(NEG_RISK_CONVERT)`，复用已有原子 reservation、失败回滚、nonce、重启恢复与 finality。
- 保存 event version、outcome、conversion matrix 与官方合约事件 reconciliation。
- 旧 settlement planner 不再无条件拒绝 augmented；必须显式提供 source YES asset 后才允许规划。

未获得真实 augmented conversion 链上样本时，reconciliation 保持 `NO_OFFICIAL_EVIDENCE`，不会冒充真实通过。

## 4. P2 账户行为

入口：

- `quant/simulator/economics/account_cashflows.py`
- `quant/simulator/economics/account_programs.py`
- `quant/simulator/economics/account_return.py`

已实现：

- Deposit/Withdrawal：confirmed capital flow，只改变现金，不计为投资收益。
- Bridge：DEPOSIT_DETECTED、PROCESSING、ORIGIN_TX_CONFIRMED、SUBMITTED、COMPLETED、FAILED 单向状态；COMPLETED 必须有 destination transaction hash；本金和 fee 分开。
- Referral：官方 `REFERRAL_REWARD` activity 进入 reward ledger，只有 RECEIVED 才进 confirmed return；模型侧实现 owner 累计交易量门槛、30 日窗口、Platinum 截止、被邀请人 rebate 后净 fee 以及直接/间接比例。
- Sponsor：每个账户/market 只允许一个 active commitment；每日 distribution 单独记录；取消先进入 CANCEL_PENDING；下一 UTC 日且真实确认后才退款；commitment/refund 是 capital flow，distribution 是账户成本。
- Dispute：bond principal、won/lost terminal outcome、principal return 和 bounty 分离；终态不可改写。
- 未识别账户现金流继续进入 `unmodeled_cashflows`，不计入 confirmed return。
- 所有负向账户现金流检查可用 paper cash，不能使账户因模拟操作无意透支。
- Bridge 一旦观测到 destination transaction hash 就不可被另一 hash 覆盖；Sponsor 大额日度 distribution 和次日退款支持重启重放；Dispute 的 WON/LOST 终态不可改写。
- Account Return 按官方项目的结算粒度（reward date 加 account/condition scope）匹配 modeled accrual 与 `RECEIVED` payout；已由官方到账覆盖的部分从 estimated return 扣除，`PAYABLE` 不参与现金或 confirmed return。

## 5. 数据库与验收

迁移：`0023-official-reward-completeness-v1`

新增主要表：

- `paper_official_account_activities`
- `paper_official_reward_rules`
- `paper_official_bridge_transactions`
- `paper_official_account_sync_runs`
- `paper_official_account_sync_checkpoints`
- `paper_official_reward_source_checks`
- `paper_reward_aggregate_reconciliations`
- `paper_reward_calibration_reports`
- `paper_referral_fee_evidence`
- `paper_referral_reward_daily_estimates`
- `paper_account_cashflow_operations`
- `paper_bridge_transfer_states`
- `paper_sponsor_commitments`
- `paper_sponsor_distributions`
- `paper_dispute_bonds`
- `simulator_augmented_neg_risk_events`
- `simulator_augmented_neg_risk_outcomes`
- `simulator_augmented_conversion_matrices`
- `simulator_augmented_conversion_reconciliations`

验收结果：

- 新增 reward/referral/migration 回归：42 passed，`ruff` PASS。
- 相关 reward/economics/augmented 单测：70 passed，`ruff` PASS。
- 数据库 schema migration：PASS。
- 隔离 runtime acceptance 连续执行两次：PASS，现金与 report id 不变；每轮结束自动清理 `acceptance:*` 夹具，控制库残留为 0。
- 2026-08-19 真实只读同步：PASS，12 个 stream checkpoint 全部推进，errors 为空。
- 最近 4 个 UTC 日的 earnings detail/total 都是 0/0，每日 `PASS_NO_ACTIVITY`。
- 已保存 22,595 条 CLOB 当前 reward config，以及版本化 taker/referral/holding 官方规则快照。
- P2 故障注入 acceptance 连续两次 PASS：bridge hash/终态冲突、Sponsor 重放/早退/退款 hash 冲突、Dispute 终态改写、Referral evidence/estimate 冲突均被拒绝；现金只应用一次，清理后残留为 0。
- Augmented negative-risk 故障注入 acceptance 连续两次 PASS：holder version 不可变、placeholder clarification 产生新版本、所有 winner 下 token 守恒，并严格区分 `NO_OFFICIAL_EVIDENCE` / `PASS` / `MISMATCH`。
- Position operation acceptance PASS：Split/Merge/Neg-risk Convert/Redeem 的 reservation、nonce、cost basis、失败回滚和重启幂等均通过。
- 本轮没有提交订单、bridge、sponsor、dispute 或其他真实资金操作。

2026-08-19 真实账户只读盘点：

- 官方 activity 共 `56` 条：`TRADE=47`、`REDEEM=5`、`DEPOSIT=4`，全部带 transaction hash。
- `4` 条真实 DEPOSIT 已作为 confirmed capital flow 幂等入账，合计 `337.743765 USDC`；它们增加现金但不增加投资收益。
- 真实 reward payout/accrual、referral evidence/estimate 均为 `0`；最新 calibration 为 `NO_ELIGIBLE_ACTIVITY`，不是金额校准 PASS。
- Bridge/Sponsor/Dispute/Augmented conversion 当前没有真实参与记录；验收夹具已与真实数据隔离并在运行后清理。

## 6. 证据边界

代码开发和数据库通路已经完成。以下结论仍只能等待未来真实事件后自动升级，不能由测试构造：

- maker rebate、taker rebate、liquidity/holding/referral reward 的真实到账金额误差。
- augmented negative-risk conversion 的真实合约事件差异。
- 当前账户未实际参与的 sponsor、bridge 与 dispute 的真实终态金额。

这些缺少的是官方事实样本，不是未实现代码。系统会保留 `NO_OFFICIAL_EVIDENCE`，直到常驻同步捕获真实证据并自动生成 reconciliation。
