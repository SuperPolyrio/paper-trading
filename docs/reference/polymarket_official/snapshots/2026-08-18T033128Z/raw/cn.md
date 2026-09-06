> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 概览

> 基于全球最大的预测市场进行构建。通过 Polymarket API 进行交易、集成并访问实时市场数据。

export const IconCard = ({icon, title, description, href, color}) => {
  return <a className="group flex flex-col p-5 border border-gray-200 dark:border-zinc-800 rounded-2xl hover:border-gray-300 dark:hover:border-zinc-700 hover:bg-gray-50 dark:hover:bg-zinc-900/50 transition-all duration-200" href={href}>
      <div className="flex items-center justify-center w-10 h-10 rounded-lg mb-4" style={{
    backgroundColor: color ? `${color}1A` : "#2E5CFF15"
  }}>
        <img src={"/images/icons/" + icon + ".svg"} />
      </div>
      <h3 className="text-base font-semibold text-gray-900 dark:text-zinc-50">
        {title}
      </h3>
      <p className="mt-1.5 text-sm text-gray-500 dark:text-zinc-400">
        {description}
      </p>
    </a>;
};

<a href="https://docs.polymarket.us" target="_blank" className="group flex items-center gap-3 mx-4 md:mx-24 mt-6 px-5 py-3.5 rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 hover:border-blue-500 transition-all duration-200 no-underline">
  <span className="text-2xl">🇺🇸</span>

  <span className="text-sm text-gray-600 dark:text-zinc-400">
    正在查找{" "}

    <span className="font-semibold text-gray-800 dark:text-zinc-200">
      Polymarket US
    </span>

    {" "}

    文档？
  </span>

  <span className="ml-auto text-sm font-medium text-gray-500 dark:text-zinc-400 group-hover:translate-x-0.5 transition-transform duration-200">
    访问美国站文档 →
  </span>
</a>

<svg className=" absolute stroke-black dark:stroke-white opacity-30 dark:opacity-40 top-0 right-0 md:w-[700px] md:h-[700px] w-[300px] h-[300px] pointer-events-none rotate-[-10deg] translate-x-20 md:translate-x-60 -translate-y-0 " width="1000" height="1205" viewBox="0 0 422 509" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path opacity="0.2" d="M399.991 0.832159C406.26 0.015159 410.64 0.654158 414.152 3.32016C417.675 5.99616 419.479 10.0462 420.389 16.3012C421.3 22.5692 421.302 30.9462 421.302 42.1872V465.86C421.302 477.11 421.3 485.492 420.389 491.76C419.479 498.015 417.675 502.06 414.153 504.727C410.631 507.394 406.251 508.037 399.984 507.222C394.49 506.507 387.627 504.683 378.742 502.199L374.809 501.096L27.221 403.558C20.692 401.727 15.839 400.366 12.144 398.847C8.461 397.332 5.977 395.678 4.16 393.287C2.344 390.897 1.42201 388.062 0.960008 384.108C0.496008 380.141 0.500001 375.102 0.500001 368.323V139.725C0.500001 132.946 0.500996 127.907 0.966996 123.939C1.431 119.985 2.353 117.149 4.161 114.759L4.16 114.758C5.977 112.368 8.461 110.715 12.144 109.2C15.839 107.68 20.692 106.32 27.221 104.489L374.809 6.95216C385.638 3.91716 393.71 1.65116 399.991 0.832159ZM374.342 290.988L86.543 371.765L84.828 372.246L86.543 372.728L374.342 453.504L374.978 453.682V290.81L374.342 290.988ZM46.789 173.266L46.807 334.782V335.441L47.441 335.264L335.205 254.505L336.92 254.023L335.205 253.542L47.424 172.784L46.789 172.605V173.266ZM374.342 54.5442L86.543 135.32L84.828 135.802L86.543 136.283L374.342 217.06L374.978 217.237V54.3652L374.342 54.5442Z" />
</svg>

<div className="relative overflow-hidden">
  <div className="relative z-10 pb-18 pt-8 max-w-6xl mx-auto ">
    <h1 className="block text-3xl px-4  md:px-24 font-semibold text-gray-900 dark:text-zinc-50 tracking-tight">
      Polymarket 开发文档
    </h1>

    <div className="max-w-2xl px-4  md:px-24 mt-4 text-lg text-gray-500 dark:text-zinc-500">
      基于全球最大的预测市场进行构建。为预测市场开发者提供 API、SDK 和工具。
    </div>
  </div>

  <div className="max-w-6xl mx-auto px-4  md:px-24 mt-10">
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 items-center">
      <div>
        <h2 className="text-xl font-semibold text-gray-900 dark:text-zinc-50">
          开发者快速入门
        </h2>

        <p className="mt-3 text-gray-500 dark:text-zinc-400 max-w-2xl">
          几分钟内发出第一个 API 请求。了解 Polymarket 平台的基础知识、获取市场数据、下单并赎回获胜持仓。
        </p>

        <div className="mt-6">
          <a href="/cn/trading/quickstart" className="inline-flex items-center px-4 py-2 text-sm font-medium text-white bg-primary rounded-full hover:bg-indigo-700 transition-colors">
            下第一笔订单 →
          </a>
        </div>
      </div>

      <div className="lg:pt-1">
        <CodeGroup>
          ```typescript TypeScript theme={null}
          import { createPublicClient } from "@polymarket/client";

          const client = createPublicClient();

          const pages = client.listMarkets({ closed: false });
          const { items: markets } = await pages.firstPage();
          ```

          ```python Python theme={null}
          from polymarket import AsyncPublicClient

          async with AsyncPublicClient() as client:
              markets = client.list_markets(closed=False)

              async for market in markets.iter_items():
                  print(market.question)
          ```

          ```bash API theme={null}
          curl -G "https://gamma-api.polymarket.com/markets/keyset" \
            --data-urlencode "closed=false" \
            --data-urlencode "limit=5"
          ```
        </CodeGroup>
      </div>
    </div>

    <div className="mt-12">
      <h2 className="text-2xl font-semibold text-gray-900 dark:text-zinc-50">
        熟悉 Polymarket
      </h2>

      <p className="mt-2 text-gray-500 dark:text-zinc-400 max-w-2xl">
        了解 Polymarket 的运作方式，探索市场和交易背后的概念，或选择集成方式。
      </p>

      <div className="mt-8">
        <CardGroup cols={3}>
          <Card title="Polymarket 101" icon="lightbulb" href="/cn/polymarket-101">
            了解预测市场如何运作以及如何使用 Polymarket。
          </Card>

          <Card title="核心概念" icon="book" href="/cn/concepts/markets-events">
            了解市场与事件、价格与订单簿、持仓、订单和判定。
          </Card>

          <Card title="SDK 与 API" icon="code" href="/cn/getting-started/sdks-apis">
            选择最适合应用的集成方式。
          </Card>
        </CardGroup>
      </div>
    </div>

    <div className="my-12 ">
      <a href="https://builders.polymarket.com" target="_blank">
        <img src="https://mintcdn.com/polymarket-292d1b1b/FOMte3ewbG-LVy3k/images/banner.png?fit=max&auto=format&n=FOMte3ewbG-LVy3k&q=85&s=d83f2f21e8474e998d8ba0f45810d978" alt="Banner" className="w-full rounded-2xl" width="2394" height="549" data-path="images/banner.png" />
      </a>
    </div>
  </div>

  <div className="max-w-6xl mx-auto px-4  md:px-24 pb-12">
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <IconCard icon="medal" title="构建者计划" description="在 Polymarket 上构建应用，并通过推动交易量获得奖励" href="https://builders.polymarket.com" />

      <IconCard icon="quiz" title="帮助中心" description="获取支持、报告问题并查找常见问题的答案" href="https://help.polymarket.com" />

      <IconCard icon="sensor" title="服务状态" description="查看 API 运行时间、服务健康状况和故障报告" href="https://status.polymarket.com" />
    </div>
  </div>
</div>
