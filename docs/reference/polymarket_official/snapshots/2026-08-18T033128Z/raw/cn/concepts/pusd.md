> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Polymarket USD

> pUSD — Polymarket 上用于所有交易的抵押代币

**pUSD**（Polymarket USD）是 Polymarket 上用于所有交易的抵押代币。它是 Polygon 上的标准 ERC-20 代币，由 USDC 背书。背书由支持提现功能的智能合约在链上强制执行。无算法锚定，无部分准备金。

<Note>
  **日常使用没有变化。** 你充值、查看余额、交易、提现。pUSD
  是你熟悉的同一体验下的技术结算层。
</Note>

***

## 为什么是 pUSD

协议使用原生 USDC 结算所有交易活动，随着平台持续增长，提供一个更具资本效率、可扩展性和机构化对齐的结算标准。

pUSD 是一个代表 USDC 债权的标准 ERC-20 包装代币。包装与解包通过 `CollateralOnramp` 和 `CollateralOfframp` 合约在链上强制执行。

***

## 基本信息

|      |                |
| ---- | -------------- |
| 代币标准 | ERC-20         |
| 网络   | Polygon 主网     |
| 小数位  | 6              |
| 背书   | USDC（链上强制执行）   |
| 可转账  | 可以 — 标准 ERC-20 |

pUSD 的设计目的是在 Polymarket 内部使用。目前没有在外部交易所上架的计划。

所有抵押品相关的合约地址详见[合约](/cn/resources/contracts)页面。

***

## 包装 — USDC.e → pUSD

使用 **CollateralOnramp** 将 USDC.e 包装为 pUSD。

```solidity theme={null}
function wrap(address _asset, address _to, uint256 _amount) external
```

**参数**

* `_asset` — 要包装的资产地址。必须是 USDC.e。
* `_to` — 铸造的 pUSD 的接收者。不必是 `msg.sender`。
* `_amount` — 包装数量，使用 USDC.e 的最小单位（6 小数位）。

**要求**

* 调用方必须先授权 **CollateralOnramp** 合约（而不是 pUSD 代币）转移 USDC.e。
* 如果管理员暂停了 USDC.e，调用会以 `OnlyUnpaused()` 回滚。

### 示例

<CodeGroup>
  ```typescript TypeScript theme={null}
  import {
    createWalletClient,
    createPublicClient,
    http,
    parseAbi,
    parseUnits,
  } from "viem";
  import { polygon } from "viem/chains";
  import { privateKeyToAccount } from "viem/accounts";

  const account = privateKeyToAccount(process.env.PRIVATE_KEY as `0x${string}`);
  const walletClient = createWalletClient({ account, chain: polygon, transport: http() });
  const publicClient = createPublicClient({ chain: polygon, transport: http() });

  const ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee" as const;
  const USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174" as const; // USDC.e on Polygon

  const amount = parseUnits("100", 6); // 100 USDC.e

  // 1. Approve the Onramp to spend your USDC.e
  const approveHash = await walletClient.writeContract({
    address: USDCE,
    abi: parseAbi(["function approve(address spender, uint256 amount) returns (bool)"]),
    functionName: "approve",
    args: [ONRAMP, amount],
  });
  await publicClient.waitForTransactionReceipt({ hash: approveHash });

  // 2. Wrap USDC.e → pUSD
  const wrapHash = await walletClient.writeContract({
    address: ONRAMP,
    abi: parseAbi(["function wrap(address _asset, address _to, uint256 _amount)"]),
    functionName: "wrap",
    args: [USDCE, account.address, amount],
  });
  await publicClient.waitForTransactionReceipt({ hash: wrapHash });
  ```

  ```python Python theme={null}
  from web3 import Web3

  ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
  USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

  amount = 100 * 10**6  # 100 USDC.e

  # 1. Approve the Onramp to spend your USDC.e
  usdce = w3.eth.contract(address=USDCE, abi=[{
      "name": "approve", "type": "function",
      "inputs": [{"name": "spender", "type": "address"},
                 {"name": "amount", "type": "uint256"}],
      "outputs": [{"type": "bool"}],
  }])
  usdce.functions.approve(ONRAMP, amount).transact({"from": address})

  # 2. Wrap USDC.e → pUSD
  onramp = w3.eth.contract(address=ONRAMP, abi=[{
      "name": "wrap", "type": "function",
      "inputs": [{"name": "_asset", "type": "address"},
                 {"name": "_to", "type": "address"},
                 {"name": "_amount", "type": "uint256"}],
      "outputs": [],
  }])
  onramp.functions.wrap(USDCE, address, amount).transact({"from": address})
  ```
</CodeGroup>

***

## 解包 — pUSD → USDC.e

使用 **CollateralOfframp** 将 pUSD 解包回 USDC.e。

```solidity theme={null}
function unwrap(address _asset, address _to, uint256 _amount) external
```

**参数**

* `_asset` — 你希望接收的资产。必须是 USDC.e。
* `_to` — 底层资产的接收者。
* `_amount` — 要解包的 pUSD 数量（6 小数位）。

**要求**

* 调用方必须先授权 **CollateralOfframp** 合约转移其 pUSD。
* 与 Onramp 使用相同的暂停机制。

***

## 下一步

<CardGroup cols={2}>
  <Card title="合约" icon="file-contract" href="/cn/resources/contracts">
    所有 Polymarket 合约地址和审计
  </Card>

  <Card title="Bridge" icon="arrow-right-arrow-left" href="/cn/trading/bridge/deposit">
    从其他链充值 — 自动包装为 pUSD
  </Card>
</CardGroup>
