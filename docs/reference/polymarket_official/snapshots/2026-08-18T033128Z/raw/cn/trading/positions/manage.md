> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 管理仓位

> 了解如何管理结果代币库存，从创建直到赎回。

在订单簿之外管理仓位：将抵押品拆分为完整的结果代币集、将配对代币合并回
抵押品，或在结算后赎回获胜代币。

根据你希望如何改变仓位来选择操作：

| 操作 | 适用场景                   |
| -- | ---------------------- |
| 拆分 | 需要将抵押品转换为一套完整的结果代币。    |
| 合并 | 持有配对的结果代币，并希望将其转换回抵押品。 |
| 赎回 | 市场已经结算，希望为获胜仓位领取抵押品。   |

选择用于提交仓位操作的集成界面。

<Tabs>
  <Tab title="TypeScript">
    本页面的示例假设你已有一个配置了 Relayer API key 或 Builder API key 的
    `SecureClient`。

    <CodeGroup>
      ```ts Relayer API Key theme={null}
      import { createSecureClient, relayerApiKey } from "@polymarket/client";
      import { privateKey } from "@polymarket/client/viem";

      const client = await createSecureClient({
        signer: privateKey(process.env.SIGNER_PRIVATE_KEY),
        wallet: process.env.POLYMARKET_WALLET_ADDRESS,
        apiKey: relayerApiKey({
          key: process.env.POLYMARKET_RELAYER_API_KEY!,
          address: process.env.POLYMARKET_RELAYER_API_KEY_ADDRESS!,
        }),
      });
      ```

      ```ts Builder API Key theme={null}
      import { createSecureClient } from "@polymarket/client";
      import { builderApiKey } from "@polymarket/client/node";
      import { privateKey } from "@polymarket/client/viem";

      const client = await createSecureClient({
        signer: privateKey(process.env.SIGNER_PRIVATE_KEY),
        wallet: process.env.POLYMARKET_WALLET_ADDRESS,
        apiKey: builderApiKey({
          key: process.env.POLYMARKET_BUILDER_API_KEY!,
          secret: process.env.POLYMARKET_BUILDER_SECRET!,
          passphrase: process.env.POLYMARKET_BUILDER_PASSPHRASE!,
        }),
      });
      ```
    </CodeGroup>

    有关完整的钱包设置和 Relayer 授权流程，请参阅
    [钱包与身份验证](/cn/trading/wallets-auth#执行免-gas-交易)。
  </Tab>

  <Tab title="Python">
    本页面的示例假设你已有一个配置了 Relayer API key 或 Builder API key 的
    `AsyncSecureClient`。

    <CodeGroup>
      ```python Relayer API Key theme={null}
      import os

      from polymarket import AsyncSecureClient, RelayerApiKey

      client = await AsyncSecureClient.create(
          private_key=os.environ["SIGNER_PRIVATE_KEY"],
          wallet=os.environ["POLYMARKET_WALLET_ADDRESS"],
          api_key=RelayerApiKey(
              key=os.environ["POLYMARKET_RELAYER_API_KEY"],
              address=os.environ["POLYMARKET_RELAYER_API_KEY_ADDRESS"],
          ),
      )
      ```

      ```python Builder API Key theme={null}
      import os

      from polymarket import AsyncSecureClient, BuilderApiKey

      client = await AsyncSecureClient.create(
          private_key=os.environ["SIGNER_PRIVATE_KEY"],
          wallet=os.environ["POLYMARKET_WALLET_ADDRESS"],
          api_key=BuilderApiKey(
              key=os.environ["POLYMARKET_BUILDER_API_KEY"],
              secret=os.environ["POLYMARKET_BUILDER_SECRET"],
              passphrase=os.environ["POLYMARKET_BUILDER_PASSPHRASE"],
          ),
      )
      ```
    </CodeGroup>

    同步 `SecureClient` 提供相同方法，并支持这两种 API key 类型。

    有关完整的钱包设置和 Relayer 授权流程，请参阅
    [钱包与身份验证](/cn/trading/wallets-auth#执行免-gas-交易)。
  </Tab>

  <Tab title="API">
    本页面的示例假设你可以使用 Relayer API key 或 Builder API key 对
    Relayer API 请求进行身份验证。以下示例使用 Relayer API key：

    ```bash theme={null}
    RELAYER_API_KEY="<relayer_api_key>"
    RELAYER_API_KEY_ADDRESS="<signer_address>"
    ```

    有关完整的钱包设置和 Relayer 授权流程，请参阅
    [执行免 Gas 交易](/cn/trading/wallets-auth#执行免-gas-交易)。
  </Tab>

  <Tab title="Solidity">
    本页面的示例使用以下 Polygon 合约：

    | 合约                            | 地址                                                                                                                         |
    | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
    | pUSD                          | [`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`](https://polygonscan.com/address/0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB) |
    | Conditional Tokens            | [`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`](https://polygonscan.com/address/0x4D97DCd97eC945f40cF65F87097ACe5EA0476045) |
    | `CtfCollateralAdapter`        | [`0xAdA100Db00Ca00073811820692005400218FcE1f`](https://polygonscan.com/address/0xAdA100Db00Ca00073811820692005400218FcE1f) |
    | `NegRiskCtfCollateralAdapter` | [`0xadA2005600Dec949baf300f4C6120000bDB6eAab`](https://polygonscan.com/address/0xadA2005600Dec949baf300f4C6120000bDB6eAab) |
  </Tab>
</Tabs>

## 拆分仓位

拆分会将 pUSD 转换为一套完整的结果代币。每 1 pUSD 会产生 1 个 YES 代币和
1 个 NO 代币。

```text theme={null}
100 pUSD → 100 YES tokens + 100 NO tokens
```

拆分前，请确保：

1. 钱包中有足够的 pUSD 支付要拆分的金额。
2. 已授权与市场类型对应的抵押品适配器使用钱包中的 pUSD。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `splitPosition()`。客户端会根据条件 ID 识别
    市场类型，并选择正确的抵押品适配器。

    ```ts theme={null}
    const transaction = await client.splitPosition({
      conditionId: market.conditionId,
      amount: 1_000_000n,
    });

    const outcome = await transaction.wait();

    // outcome.transactionHash: TxHash
    // outcome.transactionId: TransactionId | null
    ```

    `amount` 以 pUSD 最小单位计价，因此 `1_000_000n` 会将 1 pUSD 拆分为
    1 个 YES 代币和 1 个 NO 代币。交易结算后，`wait()` 会返回
    `TransactionOutcome`。
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `split_position()`。同步 `SecureClient`
    提供相同方法。两个客户端都会根据条件 ID 识别市场类型，并选择正确的
    抵押品适配器。

    ```python theme={null}
    transaction = await client.split_position(
        condition_id=market.condition_id,
        amount=1_000_000,
    )

    outcome = await transaction.wait()

    # outcome.transaction_hash: TransactionHash
    # outcome.transaction_id: str | None
    ```

    `amount` 以 pUSD 最小单位计价，因此 `1_000_000` 会将 1 pUSD 拆分为
    1 个 YES 代币和 1 个 NO 代币。交易结算后，`wait()` 会返回
    `TransactionOutcome`。
  </Tab>

  <Tab title="API">
    构建 `splitPosition` 调用，然后将其作为免 Gas 钱包交易执行。

    <Steps>
      <Step title="构建拆分调用">
        根据市场响应中的 `negRisk` 值选择调用目标：

        | `negRisk` | 调用目标                          | 地址                                           |
        | --------- | ----------------------------- | -------------------------------------------- |
        | `false`   | `CtfCollateralAdapter`        | `0xAdA100Db00Ca00073811820692005400218FcE1f` |
        | `true`    | `NegRiskCtfCollateralAdapter` | `0xadA2005600Dec949baf300f4C6120000bDB6eAab` |

        对以下函数调用进行 ABI 编码：

        ```solidity theme={null}
        function splitPosition(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] partition,
            uint256 amount
        );
        ```

        | 参数                   | 值                                            |
        | -------------------- | -------------------------------------------- |
        | `collateralToken`    | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
        | `parentCollectionId` | 32 个零字节                                      |
        | `conditionId`        | `<market_condition_id>`                      |
        | `partition`          | `[1, 2]`                                     |
        | `amount`             | `<amount_in_pusd_base_units>`                |

        将编码后的 calldata 放入钱包调用：

        ```json theme={null}
        {
          "target": "<collateral_adapter_address>",
          "value": "0",
          "data": "<encoded_split_position_calldata>"
        }
        ```
      </Step>

      <Step title="执行拆分">
        对于 Deposit Wallet，将拆分调用包含在已签名的钱包批次中，并使用
        Relayer API key 提交：

        ```bash theme={null}
        curl -X POST "https://relayer-v2.polymarket.com/submit" \
          -H "Content-Type: application/json" \
          -H "RELAYER_API_KEY: $RELAYER_API_KEY" \
          -H "RELAYER_API_KEY_ADDRESS: $RELAYER_API_KEY_ADDRESS" \
          --data '{
            "type": "WALLET",
            "from": "<signer_address>",
            "to": "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07",
            "nonce": "<wallet_nonce>",
            "signature": "<wallet_batch_signature>",
            "metadata": "Split position",
            "depositWalletParams": {
              "depositWallet": "<deposit_wallet_address>",
              "deadline": "<unix_seconds>",
              "calls": [
                {
                  "target": "<collateral_adapter_address>",
                  "value": "0",
                  "data": "<encoded_split_position_calldata>"
                }
              ]
            }
          }'
        ```

        有关 nonce 创建、钱包批次签名和确认，请参阅
        [执行免 Gas 交易](/cn/trading/wallets-auth#执行免-gas-交易)。在使用
        新余额前，请等待 `STATE_CONFIRMED`。
      </Step>
    </Steps>
  </Tab>

  <Tab title="Solidity">
    通过抵押品适配器拆分 pUSD 的流程如下：

    * 授权抵押品适配器使用要拆分的 pUSD 金额。
    * 使用市场条件 ID 和金额调用适配器。
    * 适配器执行底层 CTF 操作，并以原子方式铸造等量的 YES 和 NO 余额。

    <Steps>
      <Step title="授权 pUSD">
        首先，在 pUSD 合约上调用 `approve()`。标准市场授权
        `CtfCollateralAdapter`，负风险市场授权
        `NegRiskCtfCollateralAdapter`：

        ```solidity theme={null}
        function approve(address spender, uint256 amount) external returns (bool);
        ```

        | 参数        | 值                   |
        | --------- | ------------------- |
        | `spender` | 与市场类型对应的抵押品适配器      |
        | `amount`  | 要拆分的 pUSD 金额，以最小单位计 |
      </Step>

      <Step title="拆分仓位">
        然后，在刚才授权的同一合约上调用 `splitPosition()`：标准市场使用
        `CtfCollateralAdapter`，负风险市场使用
        `NegRiskCtfCollateralAdapter`：

        ```solidity theme={null}
        function splitPosition(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] partition,
            uint256 amount
        );
        ```

        | 参数                   | 值                                            |
        | -------------------- | -------------------------------------------- |
        | `collateralToken`    | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
        | `parentCollectionId` | 32 个零字节                                      |
        | `conditionId`        | 市场的条件 ID                                     |
        | `partition`          | `[1, 2]`                                     |
        | `amount`             | 要拆分的 pUSD 金额，以最小单位计                          |

        此调用会向调用者铸造等量的 YES 和 NO 代币。
      </Step>
    </Steps>
  </Tab>
</Tabs>

## 合并仓位

合并会将一套完整的结果代币转换回 pUSD。每 1 个 YES 代币和 1 个 NO 代币
会返还 1 pUSD。

```text theme={null}
100 YES tokens + 100 NO tokens → 100 pUSD
```

合并前，请确保：

1. 持有等量的 YES 和 NO 代币。
2. 已授权与市场类型对应的抵押品适配器转移钱包的结果代币。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `mergePositions()`。客户端会识别市场类型、
    选择正确的抵押品适配器，并检查钱包可合并的余额。

    ```ts theme={null}
    const transaction = await client.mergePositions({
      conditionId: market.conditionId,
      amount: "max",
    });

    const outcome = await transaction.wait();

    // outcome.transactionHash: TxHash
    // outcome.transactionId: TransactionId | null
    ```

    传入以最小单位计的 `bigint` 以合并指定金额。`"max"` 会合并钱包 YES 和
    NO 余额中较小的一个。交易结算后，`wait()` 会返回
    `TransactionOutcome`。
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `merge_positions()`。同步 `SecureClient`
    提供相同方法。两个客户端都会识别市场类型、选择正确的抵押品适配器，
    并检查钱包可合并的余额。

    ```python theme={null}
    transaction = await client.merge_positions(
        condition_id=market.condition_id,
        amount="max",
    )

    outcome = await transaction.wait()

    # outcome.transaction_hash: TransactionHash
    # outcome.transaction_id: str | None
    ```

    传入以最小单位计的 `int` 以合并指定金额。`"max"` 会合并钱包 YES 和
    NO 余额中较小的一个。交易结算后，`wait()` 会返回
    `TransactionOutcome`。
  </Tab>

  <Tab title="API">
    构建 `mergePositions` 调用，然后将其作为免 Gas 钱包交易执行。

    <Steps>
      <Step title="构建合并调用">
        当 `negRisk` 为 `false` 时，使用地址为
        `0xAdA100Db00Ca00073811820692005400218FcE1f` 的
        `CtfCollateralAdapter`；为 `true` 时，使用地址为
        `0xadA2005600Dec949baf300f4C6120000bDB6eAab` 的
        `NegRiskCtfCollateralAdapter`。

        对以下函数调用进行 ABI 编码：

        ```solidity theme={null}
        function mergePositions(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] partition,
            uint256 amount
        );
        ```

        | 参数                   | 值                                            |
        | -------------------- | -------------------------------------------- |
        | `collateralToken`    | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
        | `parentCollectionId` | 32 个零字节                                      |
        | `conditionId`        | `<market_condition_id>`                      |
        | `partition`          | `[1, 2]`                                     |
        | `amount`             | `<amount_in_pusd_base_units>`                |

        将编码后的 calldata 放入钱包调用：

        ```json theme={null}
        {
          "target": "<collateral_adapter_address>",
          "value": "0",
          "data": "<encoded_merge_positions_calldata>"
        }
        ```
      </Step>

      <Step title="执行合并">
        对于 Deposit Wallet，将合并调用包含在已签名的钱包批次中，并使用
        Relayer API key 提交：

        ```bash theme={null}
        curl -X POST "https://relayer-v2.polymarket.com/submit" \
          -H "Content-Type: application/json" \
          -H "RELAYER_API_KEY: $RELAYER_API_KEY" \
          -H "RELAYER_API_KEY_ADDRESS: $RELAYER_API_KEY_ADDRESS" \
          --data '{
            "type": "WALLET",
            "from": "<signer_address>",
            "to": "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07",
            "nonce": "<wallet_nonce>",
            "signature": "<wallet_batch_signature>",
            "metadata": "Merge positions",
            "depositWalletParams": {
              "depositWallet": "<deposit_wallet_address>",
              "deadline": "<unix_seconds>",
              "calls": [
                {
                  "target": "<collateral_adapter_address>",
                  "value": "0",
                  "data": "<encoded_merge_positions_calldata>"
                }
              ]
            }
          }'
        ```

        有关 nonce 创建、钱包批次签名和确认，请参阅
        [执行免 Gas 交易](/cn/trading/wallets-auth#执行免-gas-交易)。在使用
        更新后的余额前，请等待 `STATE_CONFIRMED`。
      </Step>
    </Steps>
  </Tab>

  <Tab title="Solidity">
    通过抵押品适配器合并结果代币的流程如下：

    * 授权抵押品适配器转移调用者的结果代币。
    * 使用市场条件 ID 和要合并的金额调用适配器。
    * 适配器销毁等量的 YES 和 NO 余额，并以原子方式返还 pUSD。

    <Steps>
      <Step title="授权结果代币">
        首先，在 Conditional Tokens 合约上调用 `setApprovalForAll()`。
        标准市场授权 `CtfCollateralAdapter`，负风险市场授权
        `NegRiskCtfCollateralAdapter`：

        ```solidity theme={null}
        function setApprovalForAll(address operator, bool approved) external;
        ```

        | 参数         | 值              |
        | ---------- | -------------- |
        | `operator` | 与市场类型对应的抵押品适配器 |
        | `approved` | `true`         |
      </Step>

      <Step title="合并仓位">
        然后，在刚才授权的同一合约上调用 `mergePositions()`：标准市场使用
        `CtfCollateralAdapter`，负风险市场使用
        `NegRiskCtfCollateralAdapter`：

        ```solidity theme={null}
        function mergePositions(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] partition,
            uint256 amount
        );
        ```

        | 参数                   | 值                                            |
        | -------------------- | -------------------------------------------- |
        | `collateralToken`    | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
        | `parentCollectionId` | 32 个零字节                                      |
        | `conditionId`        | 市场的条件 ID                                     |
        | `partition`          | `[1, 2]`                                     |
        | `amount`             | 要合并的每种结果代币数量                                 |

        `amount` 不能超过任何一种结果代币的余额。此调用会向调用者返还等量的
        pUSD。
      </Step>
    </Steps>
  </Tab>
</Tabs>

## 赎回已结算仓位

市场结算后，赎回会将结果代币转换为 pUSD。每个获胜代币返还 1 pUSD，
失败代币返还 0。

```text theme={null}
Market resolves YES:
100 YES tokens → 100 pUSD
100 NO tokens  → 0 pUSD
```

<Note>赎回没有截止日期。结算后，获胜代币可随时赎回。</Note>

赎回前，请确保：

1. 市场已经结算。
2. 持有该市场的结果代币。
3. 已授权与市场类型对应的抵押品适配器转移钱包的结果代币。

<Tabs>
  <Tab title="TypeScript">
    在 `SecureClient` 上调用 `redeemPositions()`。客户端使用条件 ID 为
    市场类型选择正确的抵押品适配器。

    ```ts theme={null}
    const transaction = await client.redeemPositions({
      conditionId: market.conditionId,
    });

    const outcome = await transaction.wait();

    // outcome.transactionHash: TxHash
    // outcome.transactionId: TransactionId | null
    ```

    赎回没有金额参数：它会赎回钱包中两种结果的全部余额。交易结算后，
    `wait()` 会返回 `TransactionOutcome`。
  </Tab>

  <Tab title="Python">
    在 `AsyncSecureClient` 上调用 `redeem_positions()`。同步 `SecureClient`
    提供相同方法。两个客户端都使用条件 ID 为市场类型选择正确的抵押品
    适配器。

    ```python theme={null}
    transaction = await client.redeem_positions(
        condition_id=market.condition_id,
    )

    outcome = await transaction.wait()

    # outcome.transaction_hash: TransactionHash
    # outcome.transaction_id: str | None
    ```

    赎回没有金额参数：它会赎回钱包中两种结果的全部余额。交易结算后，
    `wait()` 会返回 `TransactionOutcome`。
  </Tab>

  <Tab title="API">
    为已结算市场构建 `redeemPositions` 调用，然后将其作为免 Gas 钱包
    交易执行。

    <Steps>
      <Step title="构建赎回调用">
        当 `negRisk` 为 `false` 时，使用地址为
        `0xAdA100Db00Ca00073811820692005400218FcE1f` 的
        `CtfCollateralAdapter`；为 `true` 时，使用地址为
        `0xadA2005600Dec949baf300f4C6120000bDB6eAab` 的
        `NegRiskCtfCollateralAdapter`。

        对以下函数调用进行 ABI 编码：

        ```solidity theme={null}
        function redeemPositions(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] indexSets
        );
        ```

        | 参数                   | 值                                            |
        | -------------------- | -------------------------------------------- |
        | `collateralToken`    | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
        | `parentCollectionId` | 32 个零字节                                      |
        | `conditionId`        | `<market_condition_id>`                      |
        | `indexSets`          | `[1, 2]`                                     |

        将编码后的 calldata 放入钱包调用：

        ```json theme={null}
        {
          "target": "<collateral_adapter_address>",
          "value": "0",
          "data": "<encoded_redeem_positions_calldata>"
        }
        ```
      </Step>

      <Step title="执行赎回">
        对于 Deposit Wallet，将赎回调用包含在已签名的钱包批次中，并使用
        Relayer API key 提交：

        ```bash theme={null}
        curl -X POST "https://relayer-v2.polymarket.com/submit" \
          -H "Content-Type: application/json" \
          -H "RELAYER_API_KEY: $RELAYER_API_KEY" \
          -H "RELAYER_API_KEY_ADDRESS: $RELAYER_API_KEY_ADDRESS" \
          --data '{
            "type": "WALLET",
            "from": "<signer_address>",
            "to": "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07",
            "nonce": "<wallet_nonce>",
            "signature": "<wallet_batch_signature>",
            "metadata": "Redeem positions",
            "depositWalletParams": {
              "depositWallet": "<deposit_wallet_address>",
              "deadline": "<unix_seconds>",
              "calls": [
                {
                  "target": "<collateral_adapter_address>",
                  "value": "0",
                  "data": "<encoded_redeem_positions_calldata>"
                }
              ]
            }
          }'
        ```

        有关 nonce 创建、钱包批次签名和确认，请参阅
        [执行免 Gas 交易](/cn/trading/wallets-auth#执行免-gas-交易)。在使用
        收益前，请等待 `STATE_CONFIRMED`。
      </Step>
    </Steps>
  </Tab>

  <Tab title="Solidity">
    通过抵押品适配器赎回结果代币的流程如下：

    * 授权抵押品适配器转移调用者的结果代币。
    * 结算后，使用市场条件 ID 调用适配器。
    * 适配器销毁两种结果的余额，并以原子方式以 pUSD 返还获胜收益。

    <Steps>
      <Step title="授权结果代币">
        首先，在 Conditional Tokens 合约上调用 `setApprovalForAll()`。
        标准市场授权 `CtfCollateralAdapter`，负风险市场授权
        `NegRiskCtfCollateralAdapter`：

        ```solidity theme={null}
        function setApprovalForAll(address operator, bool approved) external;
        ```

        | 参数         | 值              |
        | ---------- | -------------- |
        | `operator` | 与市场类型对应的抵押品适配器 |
        | `approved` | `true`         |
      </Step>

      <Step title="赎回仓位">
        然后，在刚才授权的同一合约上调用 `redeemPositions()`：标准市场使用
        `CtfCollateralAdapter`，负风险市场使用
        `NegRiskCtfCollateralAdapter`：

        ```solidity theme={null}
        function redeemPositions(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] indexSets
        );
        ```

        | 参数                   | 值                                            |
        | -------------------- | -------------------------------------------- |
        | `collateralToken`    | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
        | `parentCollectionId` | 32 个零字节                                      |
        | `conditionId`        | 市场的条件 ID                                     |
        | `indexSets`          | `[1, 2]`                                     |

        此调用会赎回调用者在两个 index set 中的全部余额，并只返还获胜收益。
      </Step>
    </Steps>
  </Tab>
</Tabs>
