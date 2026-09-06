# Feature Ownership Matrix

This matrix distinguishes shipped simulator behavior from external evidence
gates. A passing unit or replay test does not promote a live execution model.

| Capability | Authoritative implementation | Representative verification | Migration state |
| --- | --- | --- | --- |
| Public API and tenant isolation | `quant/paper/public_api.py`, `quant/paper/tenant_platform.py`, `scripts/api/routes/paper_v1.py` | `tests/execution/test_paper_public_api.py`, `test_paper_tenant_platform.py` | Included and passing |
| Retail/professional clients | `webpage/paper-retail.*`, `webpage/paper.*` | `tests/execution/test_paper_account_ui.py` | Included and passing |
| Taker L2 depth, FOK/FAK, BUY/SELL | `quant/paper/taker_execution.py`, `quant/execution/` | `test_professional_models.py`, `test_professional_paper_hot_path.py`, `test_venue_contract.py` | Included and passing |
| Maker queue and post-only lifecycle | `quant/execution/models/maker_queue.py`, `quant/paper/live_shadow_store.py` | `test_live_paper_maker_flow.py`, `test_maker_research_l2_queue.py`, `test_maker_model_domain.py` | Included and passing |
| Persistent causal event ordering | `quant/paper/persistent_event_kernel.py`, `quant/simulator/kernel/` | `test_paper_worker_scheduler.py`, `test_lifecycle_scheduler_shadow.py`, `test_execution_profile_immutability.py` | Included and passing |
| OMS, self-trade prevention, and liquidity reservation | `quant/simulator/oms/`, `quant/simulator/liquidity/` | `test_own_order_oms.py`, `test_overlay_store_contract.py` | Included and passing |
| Risk, capacity, and geoblock admission | `quant/risk/`, `quant/simulator/admission/` | `test_capacity_gate.py`, `test_event_risk.py`, `test_unified_admission.py` | Included and passing |
| Cash, cost basis, fees, positions, PnL, and NAV | `quant/paper/paper_ledger.py`, `quant/simulator/accounting/`, `quant/simulator/economics/` | `test_professional_pnl.py`, `test_position_economics.py`, `test_fee_engine.py`, `test_account_return.py` | Included and passing |
| Fill finality and recovery | `quant/simulator/finality/`, `quant/paper/live_shadow_store.py` | `test_fill_finality.py`, `test_live_paper_batch_recovery.py` | Included and passing |
| Split, merge, convert, settlement, and redeem | `quant/simulator/operations/`, `quant/settlement/` | `test_position_operations.py`, `test_neg_risk_convert.py`, `test_settlement_redeemer.py` | Included and passing |
| Rewards, maker/taker rebate, referral | `quant/simulator/rewards/` | `test_reward_ledger.py`, `test_maker_rebate.py`, `test_taker_rebate.py`, `test_referral_reward.py` | Included and passing |
| Official account truth | `quant/simulator/account_truth/` | `test_account_truth.py`, `test_execution_closure.py`, `test_chain_activity_mirror.py` | Included and passing |
| Combo/RFQ, bridge, dispute, and integrity | `quant/simulator/combo/`, `quant/simulator/economics/`, `quant/settlement/`, `quant/simulator/integrity/` | `test_official_combo_contracts.py`, `test_bridge_official_client.py`, `test_dispute_policy.py`, `test_surveillance.py` | Included and passing |
| Offline/live calibration and evidence gates | `quant/calibration/`, `quant/maker/` | `tests/calibration/`, `tests/maker/`, maker tests under `tests/execution/` | Included and passing |
| Production health, backup, soak, and observability | `quant/paper/production_runtime.py`, `quant/paper/operations.py`, `deploy/` | `test_paper_production_runtime.py`, `test_paper_operations.py`, `tests/validation/` | Included and passing |
| LOB input boundary | `quant/orderbook/local_event_bus.py`, `local_book.py`, `service.py`, replay readers | feed consumer, replay, and maker fidelity tests | Consumer only; collector ownership excluded |

## Evidence Boundary

The migration preserves model status honestly:

- deterministic accounting and offline replay tests remain code-level evidence;
- real taker/maker holdout promotion still requires authenticated external data;
- official account, reward, rebate, and redeem claims still require raw official
  payloads or chain receipts;
- 24-hour and 7-day gates still require elapsed runtime;
- no source report, fixture, or historical JSON was copied as a fresh live pass.
