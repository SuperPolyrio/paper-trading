> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 判定

> 市场如何判定以及如何兑换获胜持仓

当事件的结果明确后，市场进入**判定**阶段。判定确定哪个结果获胜，获胜代币的持有者可以按每个 \$1 进行兑换。失败的代币变得一文不值。

Polymarket 使用 **UMA Optimistic Oracle** 进行去中心化、无许可的判定。任何人都可以提议一个结果，任何人也可以在认为结果有误时发起争议。

<Frame>
  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/core-concepts/resolution-lifecycle.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=6726569af3efd6f4fda54528c8eb0d0a" alt="" className="dark:hidden" width="1722" height="952" data-path="images/core-concepts/resolution-lifecycle.png" />

  <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/dark/core-concepts/resolution-lifecycle.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=36e91c655f7f50b18dea3a23b44f8c23" alt="" className="hidden dark:block" width="1722" height="952" data-path="images/dark/core-concepts/resolution-lifecycle.png" />
</Frame>

## 判定规则

每个市场都有预设的判定规则，规定了以下内容：

* **判定来源** — 结果的认定依据（例如，官方公告、特定网站）
* **截止日期** — 市场可以进行判定的时间
* **边界情况** — 模糊情况的处理方式

<Warning>
  交易前请务必阅读判定规则。市场标题描述了问题，但**规则**才决定如何判定。
</Warning>

<Steps>
  <Step title="提议">
    任何人都可以通过以下步骤提议判定结果：

    1. 选择获胜的结果
    2. 缴纳保证金（通常为 \$750 pUSD）
    3. 向 UMA Oracle 提交提议

    如果提议正确且无人争议，提议者可取回保证金并获得奖励。

    <Warning>
      如果提议结果不正确或过早提交，你将失去全部保证金。只有在你确信结果并了解流程的情况下才进行提议。
    </Warning>
  </Step>

  <Step title="质疑期">
    提议提交后，有一个 **2 小时的质疑期**，任何人都可以对结果发起争议。

    * **如果无争议**：提议被接受，市场完成判定
    * **如果有争议**：进入新一轮提议。如果第二次提议也被争议，判定将升级至 UMA 的 DVM（数据验证机制）进行代币持有者投票

    判定有三种可能的流程：

    1. **无争议** — 提议后直接判定（最快，约 2 小时）
    2. **一次争议** — 提议、质疑、二次提议、判定（第二次提议被接受）
    3. **两次争议** — 提议、质疑、二次提议、二次质疑、通过 DVM 投票判定
  </Step>

  <Step title="争议 - 如被质疑">
    发起争议的步骤：

    1. 缴纳反对保证金（与提议者金额相同，通常 \$750）
    2. 争议触发新一轮提议，若已在第二轮则触发辩论期

    在 **24-48 小时的辩论期**内，参与者可以在 UMA 的 Discord 频道（`#evidence-rationale` 和 `#voting-discussion`）提交证据。
  </Step>

  <Step title="UMA 投票">
    辩论期结束后，UMA 代币持有者对正确结果进行投票。投票过程大约需要 48 小时。

    | 结果           | 处理方式         | 保证金分配                                             |
    | ------------ | ------------ | ------------------------------------------------- |
    | **提议者胜出**    | 接受原始提议       | 提议者取回保证金 + 争议方保证金的一半                              |
    | **争议方胜出**    | 提议被否决，需要新的提议 | 争议方取回保证金 + 提议者保证金的一半                              |
    | **为时过早**     | 事件尚未结束       | 争议方取回保证金 + 提议者保证金的一半                              |
    | **未知/50-50** | 两个结果均不适用（罕见） | 市场按 50/50 判定——每个代币可兑换 \$0.50；争议方取回保证金 + 提议者保证金的一半 |
  </Step>
</Steps>

## 判定之后

市场判定完成后：

* **交易停止** — 该市场的代币不再可买卖
* **获胜代币**可按每个 \$1.00 兑换
* **失败代币**变得一文不值（\$0.00）

### 兑换代币

判定完成后，通过 CTF 抵押品适配器将获胜代币兑换为 pUSD。适配器会通过 CTF 合约销毁你的 ERC1155 结果代币，接收释放出的 USDC.e 抵押品，将其包装为 pUSD，并把 pUSD 返回到你的钱包。

```
100 winning tokens → $100 pUSD
```

## 补充说明

在少数情况下，交易开始后出现未预见的情况，需要对规则进行补充说明。Polymarket 可能会发布\*\*"补充说明"\*\*更新，提议者和投票者在判定时应将其纳入考量。

补充说明的特点：

* 不能改变问题的根本意图
* 通过公告板合约在链上发布
* UMA 投票者在处理争议时应参考这些说明

<Tip>
  如果你认为需要补充说明，请在 [Polymarket
  Discord](https://discord.com/invite/polymarket) 的 `#market-review`
  频道提出请求。
</Tip>

## 判定时间线

| 阶段           | 时长       |
| ------------ | -------- |
| 质疑期          | 2 小时     |
| 辩论期（如有争议）    | 24-48 小时 |
| UMA 投票（如有争议） | 约 48 小时  |

**无争议判定**：提议后约 2 小时

**有争议判定**：总计 4-6 天

## 合约地址

| 合约                     | 地址                                           | 网络              |
| ---------------------- | -------------------------------------------- | --------------- |
| **UmaCtfAdapter v3.0** | `0x157Ce2d672854c848c9b79C49a8Cc6cc89176a49` | Polygon Mainnet |
| **UmaCtfAdapter v2.0** | `0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74` | Polygon Mainnet |
| **UmaCtfAdapter v1.0** | `0xCB1822859cEF82Cd2Eb4E6276C7916e692995130` | Polygon Mainnet |

## 相关资源

* [UMA Oracle 门户](https://oracle.uma.xyz/) — 查看并参与提议
* [UMA 文档](https://docs.uma.xyz/) — 了解 Optimistic Oracle 的更多信息
* [Polymarket Discord](https://discord.com/invite/polymarket) — 讨论判定结果和请求补充说明
* [UmaCtfAdapter 源代码](https://github.com/Polymarket/uma-ctf-adapter) — 智能合约源码
* [UmaCtfAdapter 审计报告](https://github.com/Polymarket/uma-ctf-adapter/blob/main/audit/Polymarket_UMA_Optimistic_Oracle_Adapter_Audit.pdf) — 安全审计报告

## 下一步

<CardGroup cols={2}>
  <Card title="持仓与代币" icon="coins" href="/cn/concepts/positions-tokens">
    了解判定后如何兑换获胜代币。
  </Card>

  <Card title="市场与事件" icon="calendar" href="/cn/concepts/markets-events">
    了解市场的组织结构。
  </Card>
</CardGroup>
