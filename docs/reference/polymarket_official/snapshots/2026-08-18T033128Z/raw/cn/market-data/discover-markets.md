> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 发现市场

> 了解如何查找和筛选事件与市场，并探索相关元数据。

查找集成所需的事件和市场：既可以从特定的 Polymarket 链接开始，也可以广泛浏览当前活跃的内容。这些数据是公开的，无需身份验证。

## 事件

一个事件围绕一组相关问题组织一个或多个市场。单市场事件只提出一个是非问题；多市场事件则将更广泛的问题拆分为各个结果，例如在选举中为每位候选人建立一个市场。

### 获取事件

已知事件标识符时，获取单个事件。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchEvent()`，按 ID 获取事件。

    ```ts theme={null}
    const event = await client.fetchEvent({ id: "90177" });

    // event: Event
    ```

    也可以通过 Polymarket URL 或 slug 获取事件：

    <CodeGroup>
      ```ts URL theme={null}
      const event = await client.fetchEvent({
        url: "https://polymarket.com/event/will-the-us-confirm-that-aliens-exist-before-2027",
      });
      ```

      ```ts Slug theme={null}
      const event = await client.fetchEvent({
        slug: "will-the-us-confirm-that-aliens-exist-before-2027",
      });
      ```
    </CodeGroup>

    <Accordion title="输出：Event">
      <CodeGroup>
        ```ts Event Type theme={null}
        type Market = {
          id: string;
          slug?: string | null;
          question?: string | null;
          conditionId: string | null;
          outcomes: {
            yes: { tokenId: string | null };
            no: { tokenId: string | null };
          };
        };

        type Event = {
          id: string;
          slug?: string | null;
          title?: string | null;
          markets: Market[];
        };
        ```

        ```json Event Example theme={null}
        {
          "id": "90177",
          "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
          "title": "Will the US confirm that aliens exist by...?",
          "markets": [
            {
              "id": "703257",
              "slug": "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
              "question": "Will the US confirm that aliens exist before 2027?",
              "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
              "outcomes": {
                "yes": {
                  "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417"
                },
                "no": {
                  "tokenId": "7305630249804085635496399869905769372294302716159034447326228509068694952392"
                }
              }
            },
            "..."
          ]
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_event()`，按 ID 获取事件。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    event = await client.get_event(id="90177")

    # event: Event
    ```

    也可以通过 Polymarket URL 或 slug 获取事件：

    <CodeGroup>
      ```python URL theme={null}
      event = await client.get_event(
          url="https://polymarket.com/event/will-the-us-confirm-that-aliens-exist-before-2027",
      )
      ```

      ```python Slug theme={null}
      event = await client.get_event(
          slug="will-the-us-confirm-that-aliens-exist-before-2027",
      )
      ```
    </CodeGroup>

    <Accordion title="输出：Event">
      <CodeGroup>
        ```python Event Type theme={null}
        class MarketOutcome:
            token_id: str | None

        class MarketOutcomes:
            yes: MarketOutcome
            no: MarketOutcome

        class Market:
            id: str
            slug: str | None
            question: str | None
            condition_id: str | None
            outcomes: MarketOutcomes

        class Event:
            id: str
            slug: str | None
            title: str | None
            markets: tuple[Market, ...]
        ```

        ```json Event Example theme={null}
        {
          "id": "90177",
          "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
          "title": "Will the US confirm that aliens exist by...?",
          "markets": [
            {
              "id": "703257",
              "slug": "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
              "question": "Will the US confirm that aliens exist before 2027?",
              "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
              "outcomes": {
                "yes": {
                  "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417"
                },
                "no": {
                  "token_id": "7305630249804085635496399869905769372294302716159034447326228509068694952392"
                }
              }
            },
            "..."
          ]
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    按 ID 获取事件：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/events/90177"
    ```

    也可以按 slug 获取事件：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/events/slug/will-the-us-confirm-that-aliens-exist-before-2027"
    ```

    <Accordion title="输出：Event">
      <CodeGroup>
        ```ts Event Type theme={null}
        type Market = {
          id: string;
          slug: string;
          question: string | null;
          conditionId: string | null;
          clobTokenIds: string | null;
        };

        type Event = {
          id: string;
          slug: string | null;
          title: string | null;
          markets: Market[];
        };
        ```

        ```json Event Example theme={null}
        {
          "id": "90177",
          "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
          "title": "Will the US confirm that aliens exist by...?",
          "markets": [
            {
              "id": "703257",
              "slug": "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
              "question": "Will the US confirm that aliens exist before 2027?",
              "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
              "clobTokenIds": "[\"107505882767731489358349912513945399560393482969656700824895970500493757150417\",\"7305630249804085635496399869905769372294302716159034447326228509068694952392\"]"
            },
            "..."
          ]
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>
</Tabs>

### 列出事件

需要可浏览或可筛选的数据源时，列出事件。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listEvents()`，分页浏览事件。

    ```ts theme={null}
    const pages = client.listEvents({ closed: false, pageSize: 20 });

    for await (const page of pages) {
      // page.items: Event[]
    }
    ```

    <Accordion title="输出：Event[]">
      <CodeGroup>
        ```ts Page Type theme={null}
        type Market = {
          id: string;
          slug?: string | null;
          question?: string | null;
          conditionId: string | null;
        };

        type Event = {
          id: string;
          slug?: string | null;
          title?: string | null;
          markets: Market[];
        };

        type Page = {
          items: Event[];
          hasMore: boolean;
          nextCursor?: string;
        };
        ```

        ```json Page Example theme={null}
        {
          "items": [
            {
              "id": "16183",
              "slug": "kraken-ipo-in-2025",
              "title": "Kraken IPO by ___ ?",
              "markets": [
                {
                  "id": "516950",
                  "slug": "kraken-ipo-in-2025",
                  "question": "Kraken IPO in 2025?"
                },
                "..."
              ]
            },
            {
              "id": "16263",
              "slug": "macron-out-in-2025",
              "title": "Macron out by...?",
              "markets": ["..."]
            },
            "..."
          ],
          "hasMore": true,
          "nextCursor": "9YTr9qyfU9571U9_leL7LNSWUI6EXd7rpAqdh-EbPr17InYiOjEsImsiOiJldmVudHMiLCJvaCI6IjRmNTNjZGExOGMyYmFhMGMwMzU0YmI1ZjlhM2VjYmU1ZWQxMmFiNGQ4ZTExYmE4NzNjMmYxMTE2MTIwMmI5NDUiLCJrZXlzIjpbeyJ0Ijoic3RyaW5nIiwidiI6IjI1ODE1In1dfQ"
        }
        ```
      </CodeGroup>
    </Accordion>

    传入一个或多个数字标签 ID，按标签筛选：

    ```ts theme={null}
    const pages = client.listEvents({
      tagIds: [745], // numeric ID for the "nba" tag; see Tags below to look these up
      closed: false,
    });

    for await (const page of pages) {
      // page.items: Event[]
    }
    ```
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_events()`，分页浏览事件。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_events(closed=False, page_size=20)

    async for page in pages:
        ...  # page.items: tuple[Event, ...]
    ```

    <Accordion title="输出：Event[]">
      <CodeGroup>
        ```python Page Type theme={null}
        class Market:
            id: str
            slug: str | None
            question: str | None
            condition_id: str | None

        class Event:
            id: str
            slug: str | None
            title: str | None
            markets: tuple[Market, ...]

        class Page:
            items: tuple[Event, ...]
            has_more: bool
            next_cursor: str | None
        ```

        ```json Page Example theme={null}
        {
          "items": [
            {
              "id": "16183",
              "slug": "kraken-ipo-in-2025",
              "title": "Kraken IPO by ___ ?"
            },
            {
              "id": "16263",
              "slug": "macron-out-in-2025",
              "title": "Macron out by...?"
            },
            "..."
          ],
          "has_more": true,
          "next_cursor": "eyJmIjoiYWQyZjRkNmY4ZDZkIiwiayI6IjlZVHI5cXlmVTk1NzFVOV9sZUw3TE5TV1VJNkVYZDdycEFxZGgtRWJQcjE3SW5ZaU9qRXNJbXNpT2lKbGRtVnVkSE1pTENKdmFDSTZJalJtTlROalpHRXhPR015WW1GaE1HTXdNelUwWW1JMVpqbGhNMlZqWW1VMVpXUXhNbUZpTkdRNFpURXhZbUU0TnpOak1tWXhNVEUyTVRJd01tSTVORFVpTENKclpYbHpJanBiZXlKMElqb2ljM1J5YVc1bklpd2lkaUk2SWpJMU9ERTFJbjFkZlEiLCJwIjoiL2V2ZW50cy9rZXlzZXQiLCJzdmMiOiJnYW1tYSIsInYiOjF9"
        }
        ```
      </CodeGroup>
    </Accordion>

    传入数字标签 ID，按标签筛选：

    ```python theme={null}
    pages = client.list_events(
        tag_ids=[745],  # numeric ID for the "nba" tag; see Tags below to look these up
        closed=False,
    )

    async for page in pages:
        ...  # page.items: tuple[Event, ...]
    ```
  </Tab>

  <Tab title="API">
    列出活跃事件：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/events/keyset?closed=false&limit=20"
    ```

    <Accordion title="输出：事件分页">
      ```json Example theme={null}
      {
        "events": [
          {
            "id": "16183",
            "slug": "kraken-ipo-in-2025",
            "title": "Kraken IPO by ___ ?"
          },
          "..."
        ],
        "next_cursor": "9YTr9qyfU9571U9_leL7LNSWUI6EXd7rpAqdh-EbPr17InYiOjEsImsiOiJldmVudHMiLCJvaCI6IjRmNTNjZGExOGMyYmFhMGMwMzU0YmI1ZjlhM2VjYmU1ZWQxMmFiNGQ4ZTExYmE4NzNjMmYxMTE2MTIwMmI5NDUiLCJrZXlzIjpbeyJ0Ijoic3RyaW5nIiwidiI6IjI1ODE1In1dfQ"
      }
      ```
    </Accordion>

    将 `next_cursor` 作为 `after_cursor` 传入，以获取下一页：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/events/keyset?closed=false&limit=20&after_cursor=<next_cursor>"
    ```

    使用 `tag_id` 按标签筛选：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/events/keyset?tag_id=745&closed=false&limit=20"
    ```
  </Tab>
</Tabs>

## 市场

一个市场对应一个是非问题，并包含两个结果 token ID：一个对应 YES，另一个对应 NO。

### 获取市场

已知市场标识符时，获取单个市场。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchMarket()`，按 ID 获取市场。

    ```ts theme={null}
    const market = await client.fetchMarket({ id: "703257" });

    // market: Market
    ```

    也可以通过 Polymarket URL 或 slug 获取市场：

    <CodeGroup>
      ```ts URL theme={null}
      const market = await client.fetchMarket({
        url: "https://polymarket.com/market/will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
      });
      ```

      ```ts Slug theme={null}
      const market = await client.fetchMarket({
        slug: "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
      });
      ```
    </CodeGroup>

    <Accordion title="输出：Market">
      <CodeGroup>
        ```ts Market Type theme={null}
        type Market = {
          id: string;
          slug?: string | null;
          question?: string | null;
          conditionId: string | null;
          outcomes: {
            yes: { tokenId: string | null };
            no: { tokenId: string | null };
          };
        };
        ```

        ```json Market Example theme={null}
        {
          "id": "703257",
          "slug": "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
          "question": "Will the US confirm that aliens exist before 2027?",
          "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "outcomes": {
            "yes": {
              "tokenId": "107505882767731489358349912513945399560393482969656700824895970500493757150417"
            },
            "no": {
              "tokenId": "7305630249804085635496399869905769372294302716159034447326228509068694952392"
            }
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_market()`，按 ID 获取市场。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    market = await client.get_market(id="703257")

    # market: Market
    ```

    也可以通过 Polymarket URL 或 slug 获取市场：

    <CodeGroup>
      ```python URL theme={null}
      market = await client.get_market(
          url="https://polymarket.com/market/will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
      )
      ```

      ```python Slug theme={null}
      market = await client.get_market(
          slug="will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
      )
      ```
    </CodeGroup>

    <Accordion title="输出：Market">
      <CodeGroup>
        ```python Market Type theme={null}
        class MarketOutcome:
            token_id: str | None

        class MarketOutcomes:
            yes: MarketOutcome
            no: MarketOutcome

        class Market:
            id: str
            slug: str | None
            question: str | None
            condition_id: str | None
            outcomes: MarketOutcomes
        ```

        ```json Market Example theme={null}
        {
          "id": "703257",
          "slug": "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
          "question": "Will the US confirm that aliens exist before 2027?",
          "condition_id": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
          "outcomes": {
            "yes": {
              "token_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417"
            },
            "no": {
              "token_id": "7305630249804085635496399869905769372294302716159034447326228509068694952392"
            }
          }
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    按 ID 获取市场：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/markets/703257"
    ```

    也可以按 slug 获取市场：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/markets/slug/will-the-us-confirm-that-aliens-exist-before-2027-789-924-249"
    ```

    <Accordion title="输出：Market">
      ```json Example theme={null}
      {
        "id": "703257",
        "slug": "will-the-us-confirm-that-aliens-exist-before-2027-789-924-249",
        "question": "Will the US confirm that aliens exist before 2027?",
        "conditionId": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
        "clobTokenIds": "[\"107505882767731489358349912513945399560393482969656700824895970500493757150417\",\"7305630249804085635496399869905769372294302716159034447326228509068694952392\"]"
      }
      ```
    </Accordion>
  </Tab>
</Tabs>

### 列出市场

需要可筛选的数据源时，列出市场，例如按标签、体育项目或流动性浏览。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listMarkets()`，分页浏览市场。

    ```ts theme={null}
    const pages = client.listMarkets({
      tagId: 745, // numeric ID for the "nba" tag; see Tags below to look these up
      closed: false,
      pageSize: 20,
    });

    for await (const page of pages) {
      // page.items: Market[]
    }
    ```

    <Accordion title="输出：Market[]">
      ```json Example theme={null}
      [
        {
          "id": "741099",
          "slug": "will-lebron-james-retire-before-next-nba-season",
          "question": "Will LeBron James retire before next NBA season?",
          "conditionId": "0x73057b771600660ac6e659c5b831587fd3bdd378e63f359731aa3e1538577fb0"
        },
        {
          "id": "1747460",
          "slug": "will-giannis-antetokounmpo-play-for-the-atlanta-hawks-in-2026-27",
          "question": "Will Giannis Antetokounmpo play for the Atlanta Hawks in 2026-27?",
          "conditionId": "0xff2715084bb2cf20d9b88b74ef809c3317b1e99f6c006faee9b83bc85aa0fc99"
        },
        "..."
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_markets()`，分页浏览市场。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_markets(
        tag_id=745,  # numeric ID for the "nba" tag; see Tags below to look these up
        closed=False,
        page_size=20,
    )

    async for page in pages:
        ...  # page.items: tuple[Market, ...]
    ```

    <Accordion title="输出：Market[]">
      ```json Example theme={null}
      [
        {
          "id": "741099",
          "slug": "will-lebron-james-retire-before-next-nba-season",
          "question": "Will LeBron James retire before next NBA season?",
          "condition_id": "0x73057b771600660ac6e659c5b831587fd3bdd378e63f359731aa3e1538577fb0"
        },
        {
          "id": "1747460",
          "slug": "will-giannis-antetokounmpo-play-for-the-atlanta-hawks-in-2026-27",
          "question": "Will Giannis Antetokounmpo play for the Atlanta Hawks in 2026-27?",
          "condition_id": "0xff2715084bb2cf20d9b88b74ef809c3317b1e99f6c006faee9b83bc85aa0fc99"
        },
        "..."
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="API">
    按标签列出活跃市场：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/markets/keyset?tag_id=745&closed=false&limit=20"
    ```

    <Accordion title="输出：市场分页">
      ```json Example theme={null}
      {
        "markets": [
          {
            "id": "540817",
            "slug": "new-rhianna-album-before-gta-vi-926",
            "question": "New Rihanna Album before GTA VI?",
            "conditionId": "0x1fad72fae204143ff1c3035e99e7c0f65ea8d5cd9bd1070987bd1a3316f772be"
          },
          "..."
        ],
        "next_cursor": "8ejWEAtEdA8gG05m-tRMeLtBDl1Wbw1fUbXVfm0eYSZ7InYiOjEsImsiOiJtYXJrZXRzIiwib2giOiI0ZjUzY2RhMThjMmJhYTBjMDM1NGJiNWY5YTNlY2JlNWVkMTJhYjRkOGUxMWJhODczYzJmMTExNjEyMDJiOTQ1Iiwia2V5cyI6W3sidCI6InN0cmluZyIsInYiOiI1NTg5NDAifV19"
      }
      ```
    </Accordion>

    将 `next_cursor` 作为 `after_cursor` 传入，以获取下一页：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/markets/keyset?tag_id=745&closed=false&limit=20&after_cursor=<next_cursor>"
    ```
  </Tab>
</Tabs>

选择市场后，继续阅读[市场详情](/cn/market-data/market-details)，提取 token ID 和交易字段。

## 系列

系列将同一主题下定期发生的一组事件组织在一起。例如，每周的美联储利率决议系列可为每次会议建立一个事件，而贯穿赛季的联赛系列可为每场对阵建立一个事件。

### 获取系列

按 ID 获取系列。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchSeries()`，按 ID 获取系列。

    ```ts theme={null}
    const series = await client.fetchSeries({ id: "1" });

    // series: Series
    ```

    <Accordion title="输出：Series">
      ```json Example theme={null}
      {
        "id": "1",
        "slug": "nfl",
        "title": "NFL",
        "recurrence": "weekly",
        "closed": false
      }
      ```
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_series()`，按 ID 获取系列。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    series = await client.get_series("1")

    # series: Series
    ```

    <Accordion title="输出：Series">
      ```json Example theme={null}
      {
        "id": "1",
        "slug": "nfl",
        "title": "NFL",
        "recurrence": "weekly",
        "closed": false
      }
      ```
    </Accordion>
  </Tab>

  <Tab title="API">
    按 ID 获取系列：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/series/1"
    ```

    <Accordion title="输出：Series">
      ```json Example theme={null}
      {
        "id": "1",
        "slug": "nfl",
        "title": "NFL",
        "recurrence": "weekly",
        "closed": false
      }
      ```
    </Accordion>
  </Tab>
</Tabs>

### 列出系列

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listSeries()`，分页浏览系列。

    ```ts theme={null}
    const pages = client.listSeries({
      recurrence: "weekly",
      closed: false,
      pageSize: 20,
    });

    for await (const page of pages) {
      // page.items: Series[]
    }
    ```

    <Accordion title="输出：Series[]">
      ```json Example theme={null}
      [
        { "id": "1", "slug": "nfl", "title": "NFL" },
        { "id": "2", "slug": "nba", "title": "NBA" },
        { "id": "3", "slug": "mlb", "title": "MLB" },
        "..."
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_series()`，分页浏览系列。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_series(
        recurrence="weekly",
        closed=False,
        page_size=20,
    )

    async for page in pages:
        ...  # page.items: tuple[Series, ...]
    ```

    <Accordion title="输出：Series[]">
      ```json Example theme={null}
      [
        { "id": "1", "slug": "nfl", "title": "NFL" },
        { "id": "2", "slug": "nba", "title": "NBA" },
        { "id": "3", "slug": "mlb", "title": "MLB" },
        "..."
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="API">
    列出活跃的每周系列：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/series?recurrence=weekly&closed=false&limit=20"
    ```

    <Accordion title="输出：Series[]">
      ```json Example theme={null}
      [
        { "id": "1", "slug": "nfl", "title": "NFL" },
        { "id": "2", "slug": "nba", "title": "NBA" },
        { "id": "3", "slug": "mlb", "title": "MLB" },
        "..."
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

## 体育

体育元数据将体育项目映射到 Polymarket 标签和市场类型。你可以用它按联赛浏览体育市场、查询可用于筛选的有效市场类型、查找与一场比赛关联的事件，或查找球队名单。

### 列出体育项目

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listSports()`，列出支持的体育项目。

    ```ts theme={null}
    const sports = await client.listSports();

    // sports: SportsMetadata[]
    ```

    <Accordion title="输出：SportsMetadata[]">
      ```json Example theme={null}
      [
        { "id": 1, "sport": "ncaab", "tags": "1,100149,100639" },
        { "id": 2, "sport": "epl", "tags": "1,82,306,100639,100350" },
        { "id": 3, "sport": "lal", "tags": "1,780,100639,100350" }
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_sports()`，列出支持的体育项目。
    同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    sports = await client.get_sports()

    # sports: tuple[SportsMetadata, ...]
    ```

    <Accordion title="输出：SportsMetadata[]">
      ```json Example theme={null}
      [
        { "id": 1, "sport": "ncaab", "tags": "1,100149,100639" },
        { "id": 2, "sport": "epl", "tags": "1,82,306,100639,100350" },
        { "id": 3, "sport": "lal", "tags": "1,780,100639,100350" }
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="API">
    列出支持的体育项目：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/sports"
    ```

    <Accordion title="输出：SportsMetadata[]">
      ```json Example theme={null}
      [
        { "sport": "ncaab", "tags": "1,100149,100639" },
        { "sport": "epl", "tags": "1,82,306,100639,100350" },
        { "sport": "lal", "tags": "1,780,100639,100350" }
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

### 体育市场类型

每个体育市场都带有一个市场类型，表示其定价的盘口：独赢盘（哪支球队获胜）、让分盘（胜出多少）或总分盘（双方总得分高于或低于某值）。先获取有效值，再在列出市场时传入其中一个或多个进行筛选。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchSportsMarketTypes()`，列出
    支持的市场类型。市场会在有值时通过 `sports.sportsMarketType` 报告其类型。

    ```ts theme={null}
    const { marketTypes } = await client.fetchSportsMarketTypes();

    // marketTypes: string[]
    ```

    <Accordion title="输出：string[]">
      ```json Example theme={null}
      {
        "marketTypes": ["moneyline", "spreads", "totals"]
      }
      ```
    </Accordion>

    按类型筛选市场：

    ```ts theme={null}
    const pages = client.listMarkets({
      sportsMarketTypes: ["spreads"],
      closed: false,
      pageSize: 20,
    });

    for await (const page of pages) {
      // page.items: Market[]
    }
    ```
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用
    `get_sports_market_types()`，列出支持的市场类型。同步的
    `PublicClient` 和 `SecureClient` 也提供相同方法。每个市场通过
    市场会在有值时通过 `sports.sports_market_type` 报告其类型。

    ```python theme={null}
    market_types = await client.get_sports_market_types()

    # market_types.market_types: tuple[str, ...] | None
    ```

    <Accordion title="输出：string[]">
      ```json Example theme={null}
      {
        "market_types": ["moneyline", "spreads", "totals"]
      }
      ```
    </Accordion>

    按类型筛选市场：

    ```python theme={null}
    pages = client.list_markets(
        sports_market_types=["spreads"],
        closed=False,
        page_size=20,
    )

    async for page in pages:
        ...  # page.items: tuple[Market, ...]
    ```
  </Tab>

  <Tab title="API">
    列出支持的体育市场类型。市场会在有值时通过 `sportsMarketType` 报告其类型：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/sports/market-types"
    ```

    <Accordion title="输出：string[]">
      ```json Example theme={null}
      {
        "marketTypes": ["moneyline", "spreads", "totals"]
      }
      ```
    </Accordion>

    按类型筛选市场：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/markets/keyset?sports_market_types=spreads&closed=false&limit=20"
    ```
  </Tab>
</Tabs>

### 列出一场比赛的相关事件

体育比赛的市场可能分布在一个主事件和多个配套事件中。根据具体比赛，配套事件可能包括球员专项盘口、半场和下半场结果、精确比分，或将其他盘口集中在一起的 more-markets 事件。配套事件的 slug 会在主事件 slug 后追加后缀，例如 `-player-props`、`-halftime-result` 或 `-more-markets`。

如果只需要一个配套事件，可追加已知后缀。若要查找当前的配套事件，请使用下面的列表查询流程，而不是逐个猜测后缀。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchEvent()`。如果只需要
    more-markets 事件，请在主事件的 slug 后追加 `-more-markets`。

    ```ts theme={null}
    const mainSlug = "mls-chi-vwh-2026-07-16";

    const moreMarkets = await client.fetchEvent({
      slug: `${mainSlug}-more-markets`,
    });
    ```

    若要列出当前与比赛关联的事件，请先获取主事件并读取 `sports.gameId`，再将其传给
    `listEvents()`。并非每个事件都有比赛 ID，因此筛选前需要先检查。

    ```ts theme={null}
    const mainSlug = "mls-chi-vwh-2026-07-16";
    const game = await client.fetchEvent({ slug: mainSlug });
    const gameId = game.sports.gameId;

    if (gameId == null) {
      throw new Error("Event does not have a game ID");
    }

    const pages = client.listEvents({ gameIds: [gameId], pageSize: 20 });

    for await (const page of pages) {
      // page.items: Event[]
    }
    ```

    <Accordion title="输出：事件分页">
      ```json Example theme={null}
      {
        "items": [
          {
            "id": "662915",
            "slug": "mls-chi-vwh-2026-07-16",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC"
          },
          {
            "id": "662970",
            "slug": "mls-chi-vwh-2026-07-16-halftime-result",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - Halftime Result"
          },
          {
            "id": "662972",
            "slug": "mls-chi-vwh-2026-07-16-second-half-result",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - Second Half Result"
          },
          {
            "id": "662974",
            "slug": "mls-chi-vwh-2026-07-16-exact-score",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - Exact Score"
          },
          {
            "id": "662976",
            "slug": "mls-chi-vwh-2026-07-16-first-to-score",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - First Team to Score"
          },
          {
            "id": "663131",
            "slug": "mls-chi-vwh-2026-07-16-more-markets",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - More Markets"
          }
        ],
        "hasMore": false
      }
      ```
    </Accordion>

    `market.sports.sportsMarketType` 在有值时可标识 more-markets 事件中的盘口，
    例如 `first_half_totals`、`both_teams_to_score` 和 `soccer_team_totals`。
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_event()`。如果只需要
    more-markets 事件，请在主事件的 slug 后追加 `-more-markets`。同步的
    `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    main_slug = "mls-chi-vwh-2026-07-16"

    more_markets = await client.get_event(slug=f"{main_slug}-more-markets")
    ```

    若要列出当前与比赛关联的事件，请先获取主事件并读取 `sports.game_id`，再将其传给
    `list_events()`。并非每个事件都有比赛 ID，因此筛选前需要先检查。

    ```python theme={null}
    main_slug = "mls-chi-vwh-2026-07-16"
    game = await client.get_event(slug=main_slug)
    game_id = game.sports.game_id

    if game_id is None:
        raise ValueError("Event does not have a game ID")

    pages = client.list_events(game_ids=[game_id], page_size=20)

    async for page in pages:
        ...  # page.items: tuple[Event, ...]
    ```

    <Accordion title="输出：事件分页">
      ```json Example theme={null}
      {
        "items": [
          {
            "id": "662915",
            "slug": "mls-chi-vwh-2026-07-16",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC"
          },
          {
            "id": "662970",
            "slug": "mls-chi-vwh-2026-07-16-halftime-result",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - Halftime Result"
          },
          {
            "id": "662972",
            "slug": "mls-chi-vwh-2026-07-16-second-half-result",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - Second Half Result"
          },
          {
            "id": "662974",
            "slug": "mls-chi-vwh-2026-07-16-exact-score",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - Exact Score"
          },
          {
            "id": "662976",
            "slug": "mls-chi-vwh-2026-07-16-first-to-score",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - First Team to Score"
          },
          {
            "id": "663131",
            "slug": "mls-chi-vwh-2026-07-16-more-markets",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - More Markets"
          }
        ],
        "has_more": false
      }
      ```
    </Accordion>

    `market.sports.sports_market_type` 在有值时可标识 more-markets 事件中的盘口，
    例如 `first_half_totals`、`both_teams_to_score` 和 `soccer_team_totals`。
  </Tab>

  <Tab title="API">
    如果只需要 more-markets 事件，请在主事件的 slug 后追加 `-more-markets`：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/events/slug/mls-chi-vwh-2026-07-16-more-markets"
    ```

    若要列出当前与比赛关联的事件，请先获取主事件并读取其 `gameId`：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/events/slug/mls-chi-vwh-2026-07-16"
    ```

    将该 ID 作为 `game_id` 传入：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/events/keyset?game_id=90104306&limit=20"
    ```

    <Accordion title="输出：Events Page">
      ```json Example theme={null}
      {
        "events": [
          {
            "id": "662915",
            "slug": "mls-chi-vwh-2026-07-16",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC",
            "gameId": 90104306
          },
          {
            "id": "662970",
            "slug": "mls-chi-vwh-2026-07-16-halftime-result",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - Halftime Result",
            "gameId": 90104306
          },
          {
            "id": "662972",
            "slug": "mls-chi-vwh-2026-07-16-second-half-result",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - Second Half Result",
            "gameId": 90104306
          },
          {
            "id": "662974",
            "slug": "mls-chi-vwh-2026-07-16-exact-score",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - Exact Score",
            "gameId": 90104306
          },
          {
            "id": "662976",
            "slug": "mls-chi-vwh-2026-07-16-first-to-score",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - First Team to Score",
            "gameId": 90104306
          },
          {
            "id": "663131",
            "slug": "mls-chi-vwh-2026-07-16-more-markets",
            "title": "Chicago Fire FC vs. Vancouver Whitecaps FC - More Markets",
            "gameId": 90104306
          }
        ]
      }
      ```
    </Accordion>

    如果响应包含 `next_cursor`，请在下一次请求中将其作为 `after_cursor` 传入。
    `market.sportsMarketType` 在有值时可标识 more-markets 事件中的盘口，例如
    `first_half_totals`、`both_teams_to_score` 和 `soccer_team_totals`。
  </Tab>
</Tabs>

### 列出球队

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listTeams()`，分页浏览球队。

    ```ts theme={null}
    const pages = client.listTeams({ league: ["nba"], pageSize: 20 });

    for await (const page of pages) {
      // page.items: Team[]
    }
    ```

    <Accordion title="输出：Team[]">
      ```json Example theme={null}
      [
        {
          "id": 114168,
          "name": "Candace's Rising Stars",
          "league": "nba",
          "abbreviation": "crs"
        },
        {
          "id": 114166,
          "name": "Chuck's Global Stars",
          "league": "nba",
          "abbreviation": "cgs"
        },
        "..."
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_teams()`，分页
    浏览球队。同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_teams(league="nba", page_size=20)

    async for page in pages:
        ...  # page.items: tuple[Team, ...]
    ```

    <Accordion title="输出：Team[]">
      ```json Example theme={null}
      [
        {
          "id": 114168,
          "name": "Candace's Rising Stars",
          "league": "nba",
          "abbreviation": "crs"
        },
        {
          "id": 114166,
          "name": "Chuck's Global Stars",
          "league": "nba",
          "abbreviation": "cgs"
        },
        "..."
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="API">
    列出联赛中的球队：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/teams?league=nba&limit=20"
    ```

    <Accordion title="输出：Team[]">
      ```json Example theme={null}
      [
        {
          "id": 114168,
          "name": "Candace's Rising Stars",
          "league": "nba",
          "abbreviation": "crs"
        },
        "..."
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

## 搜索

搜索可通过一次自由文本查询返回匹配的事件、标签和个人资料。可用于搜索栏或“跳转到市场”输入框。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `search()`，搜索 Polymarket。

    ```ts theme={null}
    const pages = client.search({ q: "aliens", pageSize: 10 });

    for await (const page of pages) {
      // page.items.events: Event[]
      // page.items.tags: SearchTag[]
      // page.items.profiles: Profile[]
    }
    ```

    <Accordion title="输出：SearchResults">
      <CodeGroup>
        ```ts SearchResults Type theme={null}
        type Event = {
          id: string;
          slug?: string | null;
          title?: string | null;
        };

        type SearchTag = {
          id: string;
          label?: string | null;
          slug?: string | null;
          eventCount?: number | null;
        };

        type Profile = {
          pseudonym?: string | null;
          wallet?: string | null;
        };

        type SearchResults = {
          events: Event[];
          tags: SearchTag[];
          profiles: Profile[];
        };
        ```

        ```json SearchResults Example theme={null}
        {
          "events": [
            {
              "id": "90177",
              "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
              "title": "Will the US confirm that aliens exist by...?"
            },
            "..."
          ],
          "tags": [],
          "profiles": []
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `search()`，搜索
    Polymarket。同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.search(q="aliens", page_size=10)

    async for page in pages:
        for search_results in page.items:
            # search_results.events: tuple[Event, ...]
            # search_results.tags: tuple[SearchTag, ...]
            # search_results.profiles: tuple[Profile, ...]
            ...
    ```

    <Accordion title="输出：SearchResults">
      <CodeGroup>
        ```python SearchResults Type theme={null}
        class Event:
            id: str
            slug: str | None
            title: str | None

        class SearchTag:
            id: str
            label: str | None
            slug: str | None
            event_count: int | None

        class Profile:
            pseudonym: str | None
            wallet: str | None

        class SearchResults:
            events: tuple[Event, ...]
            tags: tuple[SearchTag, ...]
            profiles: tuple[Profile, ...]
        ```

        ```json SearchResults Example theme={null}
        {
          "events": [
            {
              "id": "90177",
              "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
              "title": "Will the US confirm that aliens exist by...?"
            },
            "..."
          ],
          "tags": [],
          "profiles": []
        }
        ```
      </CodeGroup>
    </Accordion>
  </Tab>

  <Tab title="API">
    搜索 Polymarket：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/public-search?q=aliens"
    ```

    <Accordion title="输出：搜索结果">
      ```json Example theme={null}
      {
        "events": [
          {
            "id": "90177",
            "slug": "will-the-us-confirm-that-aliens-exist-before-2027",
            "title": "Will the US confirm that aliens exist by...?"
          },
          "..."
        ],
        "tags": [],
        "profiles": [],
        "pagination": {
          "hasMore": true,
          "totalResults": 28
        }
      }
      ```
    </Accordion>
  </Tab>
</Tabs>

## 标签

标签按类别、联赛、人物和主题组织事件与市场。每个标签可以关联多个带有排序的
相关标签，因此形成的是有向图，而不是严格的树状结构。利用这些关系，可以从
一个主题扩展到相邻主题。

### 列出标签

浏览可用标签，例如用于构建类别筛选器。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `listTags()`，分页浏览标签。

    ```ts theme={null}
    const pages = client.listTags({ pageSize: 20 });

    for await (const page of pages) {
      // page.items: Tag[]
    }
    ```

    <Accordion title="输出：Tag[]">
      ```json Example theme={null}
      [
        {
          "id": "101867",
          "slug": "product-marekt-fit",
          "label": "product marekt fit"
        },
        { "id": "1512", "slug": "caitlin-clark", "label": "caitlin clark" },
        "..."
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `list_tags()`，分页
    浏览标签。同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    pages = client.list_tags(page_size=20)

    async for page in pages:
        ...  # page.items: tuple[Tag, ...]
    ```

    <Accordion title="输出：Tag[]">
      ```json Example theme={null}
      [
        {
          "id": "101867",
          "slug": "product-marekt-fit",
          "label": "product marekt fit"
        },
        { "id": "1512", "slug": "caitlin-clark", "label": "caitlin clark" },
        "..."
      ]
      ```
    </Accordion>
  </Tab>

  <Tab title="API">
    列出标签：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/tags?limit=20"
    ```

    <Accordion title="输出：Tag[]">
      ```json Example theme={null}
      [
        {
          "id": "101867",
          "slug": "product-marekt-fit",
          "label": "product marekt fit"
        },
        { "id": "1512", "slug": "caitlin-clark", "label": "caitlin clark" },
        "..."
      ]
      ```
    </Accordion>
  </Tab>
</Tabs>

### 获取标签

通过 slug 获取标签并解析其数字 ID。前文“事件”和“市场”中的 `tagId`/`tagIds` 筛选器需要使用该 ID。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchTag()`，按 slug 获取标签。

    ```ts theme={null}
    const nba = await client.fetchTag({ slug: "nba" });

    // nba: Tag
    ```

    <Accordion title="输出：Tag">
      ```json Example theme={null}
      { "id": "745", "slug": "nba", "label": "NBA" }
      ```
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_tag()`，按 slug
    获取标签。同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    nba = await client.get_tag(slug="nba")

    # nba: Tag
    ```

    <Accordion title="输出：Tag">
      ```json Example theme={null}
      { "id": "745", "slug": "nba", "label": "NBA" }
      ```
    </Accordion>
  </Tab>

  <Tab title="API">
    按 slug 获取标签：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/tags/slug/nba"
    ```

    <Accordion title="输出：Tag">
      ```json Example theme={null}
      { "id": "745", "slug": "nba", "label": "NBA" }
      ```
    </Accordion>
  </Tab>
</Tabs>

### 获取标签关系

获取某个标签与相关标签之间的关系记录，可用于构建“你可能还喜欢”之类的功能。每条记录只是一个轻量指针（排序值和两个数字标签 ID），而不是完整的标签对象。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchRelatedTags()`，获取标签的
    关系记录。

    ```ts theme={null}
    const relatedTags = await client.fetchRelatedTags({ slug: "nba" });

    // relatedTags: RelatedTag[]
    ```

    <Accordion title="输出：RelatedTag[]">
      ```json Example theme={null}
      [{ "id": "58212", "tagId": 745, "relatedTagId": 1512, "rank": 1 }]
      ```
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用 `get_related_tags()`，
    获取标签的关系记录。同步的 `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    related_tags = await client.get_related_tags(slug="nba")

    # related_tags: tuple[RelatedTag, ...]
    ```

    <Accordion title="输出：RelatedTag[]">
      ```json Example theme={null}
      [{ "id": "58212", "tag_id": 745, "related_tag_id": 1512, "rank": 1 }]
      ```
    </Accordion>
  </Tab>

  <Tab title="API">
    获取标签的关系记录：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/tags/slug/nba/related-tags"
    ```

    <Accordion title="输出：RelatedTag[]">
      ```json Example theme={null}
      [{ "id": "58212", "tagID": 745, "relatedTagID": 1512, "rank": 1 }]
      ```
    </Accordion>
  </Tab>
</Tabs>

### 获取相关标签

一次获取某个标签所有相关标签的完整 `Tag` 对象，无需再按 ID 逐个获取。

<Tabs>
  <Tab title="TypeScript">
    在 `PublicClient` 或 `SecureClient` 上调用 `fetchRelatedTagResources()`，
    获取相关标签对象。

    ```ts theme={null}
    const relatedTagObjects = await client.fetchRelatedTagResources({
      slug: "nba",
      status: "active",
    });

    // relatedTagObjects: Tag[]
    ```

    <Accordion title="输出：Tag[]">
      ```json Example theme={null}
      [{ "id": "1512", "slug": "caitlin-clark", "label": "caitlin clark" }]
      ```
    </Accordion>
  </Tab>

  <Tab title="Python">
    在 `AsyncPublicClient` 或 `AsyncSecureClient` 上调用
    `get_related_tag_resources()`，获取相关标签对象。同步的
    `PublicClient` 和 `SecureClient` 也提供相同方法。

    ```python theme={null}
    related_tag_objects = await client.get_related_tag_resources(
        slug="nba",
        status="active",
    )

    # related_tag_objects: tuple[Tag, ...]
    ```

    <Accordion title="输出：Tag[]">
      ```json Example theme={null}
      [{ "id": "1512", "slug": "caitlin-clark", "label": "caitlin clark" }]
      ```
    </Accordion>
  </Tab>

  <Tab title="API">
    获取相关标签对象：

    ```bash theme={null}
    curl "https://gamma-api.polymarket.com/tags/slug/nba/related-tags/tags"
    ```

    <Accordion title="输出：Tag[]">
      ```json Example theme={null}
      [{ "id": "1512", "slug": "caitlin-clark", "label": "caitlin clark" }]
      ```
    </Accordion>
  </Tab>
</Tabs>
