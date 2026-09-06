> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 合约

> Polymarket 所有智能合约地址、审计与安全资源

所有 Polymarket 合约部署在 **Polygon 主网**（Chain ID: 137）上。本页是平台所有合约地址的唯一权威来源。

***

## 核心交易合约

| 合约                            | 地址                                                                                                                         |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| CTF Exchange                  | [`0xE111180000d2663C0091e4f400237545B87B996B`](https://polygonscan.com/address/0xE111180000d2663C0091e4f400237545B87B996B) |
| Neg Risk CTF Exchange         | [`0xe2222d279d744050d28e00520010520000310F59`](https://polygonscan.com/address/0xe2222d279d744050d28e00520010520000310F59) |
| Neg Risk Adapter（CLOB v1，已弃用） | [`0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`](https://polygonscan.com/address/0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296) |
| Conditional Tokens (CTF)      | [`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`](https://polygonscan.com/address/0x4D97DCd97eC945f40cF65F87097ACe5EA0476045) |

***

## Combos 合约

| 合约                          | 地址                                                                                                                         |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| PositionManager (proxy)     | [`0x006F54F7f9A22e0000CC2AB60031000000ae9fEF`](https://polygonscan.com/address/0x006F54F7f9A22e0000CC2AB60031000000ae9fEF) |
| PositionManager (impl)      | [`0x30c038F0Dae8dcC3E6AD51D016F50821D32Cb87e`](https://polygonscan.com/address/0x30c038F0Dae8dcC3E6AD51D016F50821D32Cb87e) |
| BinaryModule (proxy)        | [`0x1000008dD9001B968442c1000017eaE6E0dA00Ba`](https://polygonscan.com/address/0x1000008dD9001B968442c1000017eaE6E0dA00Ba) |
| BinaryModule (impl)         | [`0x492FEc596eC347459E1Ebe30b9245EB3B49B1BBa`](https://polygonscan.com/address/0x492FEc596eC347459E1Ebe30b9245EB3B49B1BBa) |
| NegRiskModule (proxy)       | [`0x200000900045e3B6259600682756002200028933`](https://polygonscan.com/address/0x200000900045e3B6259600682756002200028933) |
| NegRiskModule (impl)        | [`0xA61e7ca374F721D5b9FD5b0FEe6Fb90f27d448d7`](https://polygonscan.com/address/0xA61e7ca374F721D5b9FD5b0FEe6Fb90f27d448d7) |
| CombinatorialModule (proxy) | [`0x30000034706C7d8e12009DAB006Be20000c031A8`](https://polygonscan.com/address/0x30000034706C7d8e12009DAB006Be20000c031A8) |
| CombinatorialModule (impl)  | [`0xb529b2430d78868422C47934d9d61cC9D0C53dBb`](https://polygonscan.com/address/0xb529b2430d78868422C47934d9d61cC9D0C53dBb) |
| Exchange (proxy)            | [`0xe3333700cA9d93003F00f0F71f8515005F6c00Aa`](https://polygonscan.com/address/0xe3333700cA9d93003F00f0F71f8515005F6c00Aa) |
| Exchange (impl)             | [`0x7345C6842b244926125ed4054905cAc49620B5dc`](https://polygonscan.com/address/0x7345C6842b244926125ed4054905cAc49620B5dc) |
| AutoRedeemer (proxy)        | [`0xa1200000d0002264C9a1698e001292D00E1b00af`](https://polygonscan.com/address/0xa1200000d0002264C9a1698e001292D00E1b00af) |
| AutoRedeemer (impl)         | [`0x64860bFD14fCcaAc09cd36f347784a9616AfB66C`](https://polygonscan.com/address/0x64860bFD14fCcaAc09cd36f347784a9616AfB66C) |

***

## 抵押品合约

| 合约                             | 地址                                                                                                                         |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| pUSD — CollateralToken (proxy) | [`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`](https://polygonscan.com/address/0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB) |
| pUSD — CollateralToken (impl)  | [`0x6bBCef9f7ef3B6C592c99e0f206a0DE94Ad0925f`](https://polygonscan.com/address/0x6bBCef9f7ef3B6C592c99e0f206a0DE94Ad0925f) |
| CollateralOnramp               | [`0x93070a847efEf7F70739046A929D47a521F5B8ee`](https://polygonscan.com/address/0x93070a847efEf7F70739046A929D47a521F5B8ee) |
| CollateralOfframp              | [`0x2957922Eb93258b93368531d39fAcCA3B4dC5854`](https://polygonscan.com/address/0x2957922Eb93258b93368531d39fAcCA3B4dC5854) |
| PermissionedRamp               | [`0xebC2459Ec962869ca4c0bd1E06368272732BCb08`](https://polygonscan.com/address/0xebC2459Ec962869ca4c0bd1E06368272732BCb08) |
| CtfCollateralAdapter           | [`0xAdA100Db00Ca00073811820692005400218FcE1f`](https://polygonscan.com/address/0xAdA100Db00Ca00073811820692005400218FcE1f) |
| NegRiskCtfCollateralAdapter    | [`0xadA2005600Dec949baf300f4C6120000bDB6eAab`](https://polygonscan.com/address/0xadA2005600Dec949baf300f4C6120000bDB6eAab) |

***

## 钱包工厂合约

| 合约                       | 地址                                                                                                                         |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| Deposit Wallet Factory   | [`0x00000000000Fb5C9ADea0298D729A0CB3823Cc07`](https://polygonscan.com/address/0x00000000000Fb5C9ADea0298D729A0CB3823Cc07) |
| Deposit Wallet Beacon    | [`0x7A18EDfe055488A3128f01F563e5B479D92ffc3a`](https://polygonscan.com/address/0x7A18EDfe055488A3128f01F563e5B479D92ffc3a) |
| Gnosis Safe Factory      | [`0xaacfeea03eb1561c4e67d661e40682bd20e3541b`](https://polygonscan.com/address/0xaacfeea03eb1561c4e67d661e40682bd20e3541b) |
| Polymarket Proxy Factory | [`0xaB45c5A4B0c941a2F231C04C3f49182e1A254052`](https://polygonscan.com/address/0xaB45c5A4B0c941a2F231C04C3f49182e1A254052) |

***

## 判定合约

| 合约                    | 地址                                                                                                                         |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| UMA Adapter           | [`0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74`](https://polygonscan.com/address/0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74) |
| UMA Optimistic Oracle | [`0xCB1822859cEF82Cd2Eb4E6276C7916e692995130`](https://polygonscan.com/address/0xCB1822859cEF82Cd2Eb4E6276C7916e692995130) |

***

## 安全

### 审计

CTF Exchange V2 已由两家独立审计公司审计：

| 审计方        | 报告                                                                                                                                                                      |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Quantstamp | [CTF Exchange V2 — Quantstamp — March 2026](https://github.com/Polymarket/ctf-exchange-v2/blob/main/audits/CTF%20Exchange%20V2%20-%20Quantstamp%20-%20March%202026.pdf) |
| Cantina    | [CTF Exchange V2 — Cantina — March 2026](https://github.com/Polymarket/ctf-exchange-v2/blob/main/audits/CTF%20Exchange%20V2%20-%20Cantina%20-%20March%202026.pdf)       |

### 漏洞赏金

可通过 [Cantina 漏洞赏金计划](https://cantina.xyz/bounties/ff945ca2-2a6e-4b83-b1b6-7a0cd3b94bea)报告安全漏洞。

***

## 源代码

<CardGroup cols={1}>
  <Card title="CTF Exchange V2" icon="github" href="https://github.com/Polymarket/ctf-exchange-v2">
    订单撮合和结算合约
  </Card>
</CardGroup>
