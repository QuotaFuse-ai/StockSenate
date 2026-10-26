# DEC-009 移除 Newsieve provider，资讯三域改由 TickHub-CN 承担

- 状态：已采纳
- 日期：2026-10-26
- 影响版本：0.1.0+
- 取代：DEC-007 决策 3 中 extras 列表里的 `backtest`（其余不变）

## 背景

`config/data_source.toml` 曾把 `news` / `social_buzz` / `policy_feed` 三个域路由到 `[providers.newsieve]`（`package = "newsieve"`、`command = "nsv"`）。实际核验发现：

1. PyPI 上没有可安装的 `newsieve` 包（`pip install newsieve` 返回 404），provider 永远无法进入 `available` 状态；三个域在真实取数时必然落到 `missing`。
2. TickHub-CN 已经提供资讯类工具：`eastmoney_stock_news`（个股资讯，dict 含 `news` 列表）、`cls_telegraph`（财联社电报，list）、`eastmoney_global_news`（7x24 全球资讯，list）、`cctv_news`（新闻联播，dict 含 `items`）。四者在真实环境均返回数据。
3. `ssenate sync-news` 依赖一个可获取的资讯通道，否则该命令是死路。

## 决策

1. 删除 `src/stock_senate/data/providers/newsieve.py`，从 `providers/__init__.py` 的工厂表移除 `newsieve` 分支。
2. `tickhub.py` 的 `DOMAIN_TOOLS` 增加 `news` / `social_buzz` → `eastmoney_stock_news`，`policy_feed` → `cls_telegraph` / `cctv_news`，并补齐各自的 `_params`。
3. `config/data_source.toml` 删除 `[providers.newsieve]`，`[routing]` 改为 `news = ["tickhub_cn"]`、`social_buzz = ["tickhub_cn"]`、`policy_feed = ["tickhub_cn", "text_docs"]`；`tickhub_cn` 的 `domains` 同步声明这三个域。
4. `ssenate sync-news` 改调 `tickhub.TickHubProvider` 的模块级 `sync()`（`eastmoney_global_news`，按 `--since` 过滤），未安装 `tickhub-cn` 时以退出码 4 如实报告，不再抛裸栈。
5. **同时移除死 extra `[backtest]`**：`src/` 中没有任何 `pandas` / `numpy` import，回测引擎是纯 Python，声明该 extra 会让用户多装几十 MB 无用依赖。`[all]` 相应改为 `stocksenate[stockdata,mcp,webui]`。DEC-007 决策 3 的 extras 列表以本决策为准。
6. 默认 LLM provider 是 `minimax`（OpenAI 兼容协议），`claude` 走 Anthropic SDK；二者此前都未声明。现把 `openai`、`anthropic` 提到必装依赖，只影响**直接依赖**声明，不直接声明 `httpx`（DEC-007 决策 1 不变，httpx 仍由 SDK 自行解析）。
7. `[mcp]` 下限从 `mcp>=1.9` 提到 `mcp>=2.0`，因为代码使用 2.x 才有的 `mcp.server.mcpserver.MCPServer`；`[dev]` 增加 `stocksenate[webui,mcp]`，使 CI 只装 `.[dev]` 时 `test_webui*` / `test_mcp` 的顶层 import 不失败。

## 后果

- 优点：四个资讯域都有可获取的数据通道；`sync-news` 不再是 404 死路；安装矩阵与实际 import 一一对应。
- 优点：干净 venv `pip install .` 后 `ssenate analyze` 不再因缺 `openai` 抛 traceback。
- 代价：资讯/社媒/政策的抓取与过滤现在完全依赖 TickHub-CN；底座不可用时三域如实标 `missing`，不做本地兜底（与 DEC-002「不重复实现采集」一致）。
- 代价：`policy_feed` 的 `text_docs` 仍作为第二顺位，本地文档只做定性阅读，不抽数值。

## 相关

- `src/stock_senate/data/providers/tickhub.py`、`src/stock_senate/data/providers/__init__.py`
- `src/stock_senate/cli.py`（`sync_news_cmd`）
- `config/data_source.toml`
- `pyproject.toml`
- `docs/specs/PRD.md` §2.3、§10
- `docs/specs/requirements-traceability.md` §1.1、§7.2、§17
