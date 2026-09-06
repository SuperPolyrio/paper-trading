# Polymarket CTF 与账户经济层实施记录

对应指导文档：

- `docs/模拟盘/polymarket_ctf_operations_and_account_economics_codex_guide.md`
- 官方快照：`docs/reference/polymarket_official/snapshots/2026-08-18T033128Z/`

## 实施状态

| 阶段 | 实现 | 数据库迁移 | 验收 |
| --- | --- | --- | --- |
| PR-1 | Split/Merge/Redeem lifecycle、CTF match settlement audit | 既有 operation/finality 表 | `paper-position-operations`、`paper-ctf-settlement` PASS |
| PR-2 | Complete-set lot、来源与成本基础消费 | `0013-complete-set-lots-v1` | `paper-complete-set-cost-basis` PASS |
| PR-3 | point-in-time platform/builder fee、逐 fill rounding/reconciliation | `0014-authoritative-fee-engine-v1` | `paper-fee-engine` PASS |
| PR-4 | reward schedule/accrual/payout/reconciliation/clawback ledger | `0015-reward-ledger-v1` | `paper-reward-ledger` PASS |
| PR-5 | maker fee-equivalent、market pool、minimum payout carry | `0016-maker-rebate-v1` | `paper-maker-rebate` PASS |
| PR-6 | rolling 30d weighted volume、tier、forward activation、bonus | `0017-taker-rebate-v1` | `paper-taker-rebate` PASS |
| PR-7 | sampled liquidity score 与 expected holding reward 分离建模 | `0018-liquidity-holding-rewards-v1` | `paper-liquidity-holding` PASS |
| PR-8 | confirmed/estimated account return 与 reward coverage | `0019-account-return-report-v1` | `paper-account-return` PASS |

## 关键不变量

- Split 不实现交易 PnL；Merge 和 Redeem 只消费一次成本基础。
- L2-only fill 的 CTF settlement 保持 `UNKNOWN/NOT_PROVABLE`，没有链上或配对证据时不推断 MINT/MERGE。
- Maker platform fee 为零；taker 和 builder fee 逐 fill 记录并单独汇总。
- `ESTIMATED/ACCRUED/PAYABLE` reward 不改变现金，只有 `RECEIVED` 改变现金。
- Reward clawback 使用追加补偿事件，不删除原 payout。
- Liquidity reward 对 paper quote 标记 `COUNTERFACTUAL_ESTIMATE`，不冒充官方 earned。
- Holding reward 使用带来源哈希的 effective schedule；当前年化率不写死。
- Account return 把 confirmed/provisional/voided trade 分开，且未知现金流进入 `unmodeled_cashflows`。
- 相同输入和相同 `as_of` 生成相同 report hash；逻辑快照发生内容碰撞时 fail closed。

## 真实数据边界

以下文件在验收中只读使用，SHA256 在执行前后保持不变：

`livevspaper/Putin_out_as_President_of_Russia_by_December_31_2026_2026-08-18_10-56-32_CST.json`

它提供真实 taker MATCH 的 market、asset、condition、trade、order 与 transaction hash 关联。验收没有提交新订单，也没有修改该文件。

Maker 的真实证据目前是 `reports/maker/holdout/current/evaluation.json` 中的 NO_FILL。它能证明没有 maker fill 时不会生成 rebate，但不能证明真实 PARTIAL/FULL maker rebate 的金额精度。

Liquidity/Holding 的 payout 验收使用明确标记的 official-format fixture，用于验证 ingest、cash、reconciliation 和 account-return plumbing；当前没有把 fixture 声称为真实官网到账。

## 验收入口

```bash
conda run -n prediction-market-quant python -m pytest -q \
  tests/simulator tests/execution/test_paper_db_migration.py \
  tests/execution/test_paper_tenant_platform.py \
  tests/execution/test_ctf_operation_schema_contract.py

conda run -n prediction-market-quant python -m quant.simulator.audit_capabilities \
  --output reports/simulator_capability_audit.md
```

所有 `20260818` 专项 acceptance 报告均为 `PASS`，并且临时数据库行清理为零。
