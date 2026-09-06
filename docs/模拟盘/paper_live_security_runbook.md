# Paper / Live 安全隔离运行手册

## 目标状态

```text
paper worker VM identity
  -> paper-runtime.env（无 secret）
  -> systemd credential: paper-db-password
  -> poly_quant_paper_app / poly_quant_paper_runtime
  -> read-only CLOB capability wrapper
  -> paper security hash-chain audit

live calibration VM identity
  -> live-calibration-runtime.env（无 secret）
  -> systemd credentials：DB、private key、API key/secret/passphrase
  -> 独立 poly_quant_live_calibration database/login
  -> quant.calibration.run_plan --live
  -> 只能运行已 prepare 的 run_id
```

同一台 Compute Engine VM 上的所有进程共享 VM service account。因此，要实现真正的
service-account 隔离，paper worker 和 live calibration probe 必须运行在不同 VM、容器
workload identity 或不同 GCP project 中。不能只创建两个账号后仍把两个进程放在同一 VM。

## 本机验收

```bash
python scripts/run_paper_security_acceptance.py
/home/jiahuaiyu/.conda/envs/prediction-market-quant/bin/python \
  -m pytest -q tests/execution
```

安全验收必须同时证明：

- `quant/paper` 不导入真实交易 SDK/adapter；
- paper CLOB client 没有 create/post/submit/cancel order 能力；
- 继承到任意 live key 时，worker 在 DB 和网络初始化前拒绝启动；
- 异常和 audit log 只记录变量名，不记录 secret value；
- credential rotation 改变 checksum，文件仍为 owner-only；
- audit hash chain 可验证，任意内容修改都能被发现。

本地 PASS 仅证明代码边界；不证明云端 IAM、VPC 和 Secret Manager 已应用。

## 生产配置

1. 将 `deploy/systemd/paper-runtime.env.example` 安装为
   `~/.config/prediction-market-quant/paper-runtime.env`，权限 `0600`。
2. 不把 DB password 写进任何 env 文件。通过 Secret Manager 取值后，管道传给：

   ```bash
   gcloud secrets versions access latest \
     --secret poly-quant-paper-db-password \
     | scripts/install_paper_secret_from_stdin.sh \
         paper-db-password \
         ~/.config/prediction-market-quant/secrets/paper-db-password
   ```

   Live calibration 的五个凭据同样不得写进 runtime env。分别通过 Secret
   Manager 管道传给 `scripts/install_live_calibration_secret_from_stdin.sh`，目标目录为
   `~/.config/prediction-market-quant/live-secrets/`。例如：

   ```bash
   gcloud secrets versions access latest \
     --secret poly-quant-live-private-key \
     | scripts/install_live_calibration_secret_from_stdin.sh \
         live-private-key \
         ~/.config/prediction-market-quant/live-secrets/private-key
   ```

   Maker calibration collector 是 no-submit 服务，但 authenticated REST recovery
   仍需使用完整签名身份查询自己的订单，因此它也必须加载 `live-private-key`；
   `exchange_submit_called` 必须始终为 `false`。

3. 以数据库 owner 执行 `deploy/postgres/paper_runtime_role.sql`，再单独设置
   `poly_quant_paper_app` 密码。用 `deploy/postgres/verify_paper_runtime_role.sql`
   验证它不属于任何 live/calibration/order/trader role。
   Live calibration 必须连接独立 `poly_quant_live_calibration` database，并在该库执行
   `deploy/postgres/live_calibration_runtime_role.sql`；不要把 live role 建进 paper DB。
4. 先 dry-run，再由管理员应用 service account 和 Secret Manager ACL：

   ```bash
   deploy/gcp/provision_paper_live_isolation.sh \
     --project PROJECT_ID --dry-run
   ```

   将 `deploy/gcp/secret-manager-audit-config.fragment.yaml` 合并进现有 project IAM
   policy，以记录 Secret Manager `DATA_READ`/`DATA_WRITE`；不要把该 fragment 当作完整
   policy 直接覆盖项目权限。

5. 在独立 paper VM identity 上应用 egress policy：

   ```bash
   deploy/gcp/provision_paper_egress_policy.sh \
     --project PROJECT_ID --network NETWORK --db-cidr DB_PRIVATE_CIDR --dry-run
   ```

VPC firewall 不能识别 HTTPS 内的 `/book` 与 `/order` 路径。`tcp:443` 仍必须开放给
公开 CLOB 读接口；真实下单防线由“paper identity 无 live secret”与
`PolymarketPaperClobClient` 无写方法共同构成。

## Secret Rotation

1. 创建新的 DB password，先更新数据库 login role。
2. 向 `poly-quant-paper-db-password` 添加新版本，不覆盖或删除旧版本。
3. 物化到新的临时 credential 文件，记录 before/after credential manifest。
4. 重启 paper worker，检查 `/health/deep`、authority、ledger/NAV 连续性和 audit chain。
5. 验证通过后禁用旧 Secret Manager version；失败则恢复旧数据库密码与旧版本。
6. 保存 rotation report，只保存版本号、SHA256、时间和操作者，不保存 secret value。

轮换完成标准：新版本能连接、旧版本已禁用、worker 无中断状态损失、audit chain PASS、
Secret Manager Data Access log 能定位本次访问。

## 生产验收门禁

- paper 与 live calibration 使用不同 VM service account；
- paper SA 只能读取 paper DB secret，无法读取任何 live secret；
- paper unit 不加载 `polydata.env`、`gcp-l2.env` 或 calibration env；
- paper DB login 为最小权限且无 live role membership；
- egress firewall 绑定 paper SA，规则和 dry-run 预期一致；
- 注入 fake live key 时服务拒绝启动，日志无值泄漏；
- 完成一次真实 Secret Manager + DB password rotation drill；
- Cloud Audit Logs 中存在 secret access 与 IAM 变更证据；
- 最终 24h/7d soak 在新 identity/credential/network 下重跑。
