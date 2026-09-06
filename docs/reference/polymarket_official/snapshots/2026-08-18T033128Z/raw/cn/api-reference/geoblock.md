> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 地区限制

> 在 Polymarket API 下单前检查地区限制

由于监管要求和国际制裁合规，Polymarket 限制某些地区的下单操作。在下单前，Builder 应验证用户的地理位置。

<Warning>
  来自受限地区的订单将被拒绝。请在你的应用中实现地区限制检查，以便在用户尝试交易前提供适当的提示。
</Warning>

***

## 地区限制端点

检查请求 IP 地址的地理合规性：

```bash theme={null}
GET https://polymarket.com/api/geoblock
```

<Note>此端点在 `polymarket.com` 上，不在 API 服务器上。</Note>

### 响应

```json theme={null}
{
  "blocked": true,
  "ip": "203.0.113.42",
  "country": "US",
  "region": "NY"
}
```

| 字段        | 类型      | 说明                      |
| --------- | ------- | ----------------------- |
| `blocked` | boolean | 用户是否被限制下单               |
| `ip`      | string  | 检测到的 IP 地址              |
| `country` | string  | ISO 3166-1 alpha-2 国家代码 |
| `region`  | string  | 地区/州代码                  |

***

## 受限地区

限制分为三类。**完全封锁**表示既不能下新订单，也不能平仓现有持仓。**仅可平仓**表示用户可以平仓现有持仓，但不能开新仓。每一类都会注明适用于前端、API 还是两者。代码：国家为 ISO 3166-1 alpha-2，次级行政区为 ISO 3166-2。

### 受 OFAC 制裁地区（完全封锁）

前端和 API 均被封锁。不能下新订单，也不能平仓现有持仓。

| 地区                | 代码    |
| ----------------- | ----- |
| Iran              | IR    |
| Syria             | SY    |
| Cuba              | CU    |
| North Korea       | KP    |
| Ukraine — Crimea  | UA-43 |
| Ukraine — Donetsk | UA-14 |
| Ukraine — Luhansk | UA-09 |

### 监管受限地区（前端与 API 均仅可平仓）

用户可以平仓现有持仓，但不能开新仓，前端和 API 均如此。

| 地区                                   | 代码    |
| ------------------------------------ | ----- |
| Australia                            | AU    |
| Belarus                              | BY    |
| Belgium                              | BE    |
| Burundi                              | BI    |
| Brazil                               | BR    |
| Canada — British Columbia            | CA-BC |
| Canada — Ontario                     | CA-ON |
| Canada — Alberta                     | CA-AB |
| Canada — Quebec                      | CA-QC |
| Central African Republic             | CF    |
| Congo (Kinshasa)                     | CD    |
| Ethiopia                             | ET    |
| France                               | FR    |
| Germany                              | DE    |
| Iraq                                 | IQ    |
| Italy                                | IT    |
| Lebanon                              | LB    |
| Libya                                | LY    |
| Myanmar                              | MM    |
| New Zealand                          | NZ    |
| Nicaragua                            | NI    |
| North Korea                          | KP    |
| Poland                               | PL    |
| Russia                               | RU    |
| Singapore                            | SG    |
| Somalia                              | SO    |
| Slovakia                             | SK    |
| South Sudan                          | SS    |
| Sudan                                | SD    |
| Taiwan                               | TW    |
| Thailand                             | TH    |
| United Kingdom                       | GB    |
| United States                        | US    |
| United States Minor Outlying Islands | UM    |
| Venezuela                            | VE    |
| Yemen                                | YE    |
| Zimbabwe                             | ZW    |

### 监管受限地区（仅前端仅可平仓）

仅在 Polymarket 前端仅可平仓；API 本身不受限制。

| 地区           | 代码 |
| ------------ | -- |
| Ireland      | IE |
| Japan        | JP |
| Malta（仅体育市场） | MT |
| Netherlands  | NL |

***

## 限制逻辑

地区限制系统包括：

1. **OFAC 制裁国家**：受美国外国资产控制办公室（OFAC）制裁的国家
2. **其他监管限制**：因特定监管合规原因而添加的国家

***

## 服务器基础设施

* **主服务器**：eu-west-2
* **最近的非地区限制区域**：eu-west-1

<Tip>
  **可申请直接同地协作（co-location）。** 完成 [KYC/KYB
  表单](https://docs.google.com/forms/d/e/1FAIpQLSfY-3Dl3yxq8HKFjFad8YzKZmm0k3Gdg29HD6gL-K-AmI6KXw/viewform)的用户可以申请直接在
  `eu-west-2` 中进行同地协作，以获得到 Polymarket 主服务器的最低延迟。
</Tip>

***

## 使用示例

<Tabs>
  <Tab title="TypeScript">
    ```typescript theme={null}
    interface GeoblockResponse {
      blocked: boolean;
      ip: string;
      country: string;
      region: string;
    }

    async function checkGeoblock(): Promise<GeoblockResponse> {
      const response = await fetch("https://polymarket.com/api/geoblock");
      return response.json();
    }

    // Usage
    const geo = await checkGeoblock();

    if (geo.blocked) {
      console.log(`Trading not available in ${geo.country}`);
    } else {
      console.log("Trading available");
    }
    ```
  </Tab>

  <Tab title="Python">
    ```python theme={null}
    import requests

    def check_geoblock() -> dict:
        response = requests.get("https://polymarket.com/api/geoblock")
        return response.json()

    # Usage
    geo = check_geoblock()

    if geo["blocked"]:
        print(f"Trading not available in {geo['country']}")
    else:
        print("Trading available")
    ```
  </Tab>
</Tabs>

***

## 为什么有这些限制

地区限制的实施是为了确保合规：

* 国际制裁和禁运
* 当地金融法规
* 博彩和预测市场法律
* 反洗钱（AML）要求
* 了解你的客户（KYC）法规

如果你认为自己被错误限制或有关于地区可用性的问题，请联系 [Polymarket Support](https://polymarket.com/support)。

***

## 下一步

<CardGroup cols={2}>
  <Card title="身份验证" icon="key" href="/cn/getting-started/api#身份验证">
    了解如何对交易请求进行身份验证。
  </Card>

  <Card title="下单" icon="plus" href="/cn/trading/quickstart">
    开始下单（从合规地区）。
  </Card>
</CardGroup>
