# DEC-002 数据层边界：DataAccessLayer 是唯一取数入口

- 状态：已采纳
- 日期：2026-10-26
- 影响版本：0.1.0+

## 背景

常见做法是在各 analyst 节点内直接调用数据接口，采集、缓存、重试、降级散落在工作流里，导致：换数据源要改节点；离线复现困难；数据缺失时模型会用「常识」补数值。StockSenate 的数据采集统一交给 TickHub-CN / TickHub-US / Newsieve，本仓库不重复实现采集、缓存、代理池与降级。

## 决策

1. 所有取数经 `DataAccessLayer`，节点与回测引擎**不得**直接 import 任何 provider 或 HTTP 客户端。
2. 每次取数返回 `DataEnvelope`：

   | 字段 | 含义 |
   |---|---|
   | `domain` | 数据域（market / news / fundamentals / policy / ...） |
   | `code` | 标的代码或主题标识 |
   | `data_as_of` | 数据时点（交易日/时间戳），回测判未来函数的唯一依据 |
   | `payload` | 结构化数据；文本资料只放原文，不抽数值 |
   | `source` | provider 名 |
   | `source_detail` | 接口/文件/SQL 来源细节 |
   | `fetched_at` | 实际抓取时间 |
   | `freshness` | `realtime` / `cached` / `archive` |
   | `confidence` | `high` / `medium` / `low` |
   | `data_source_status` | `ok` / `degraded` / `missing` |

3. 四种运行模式：`stockdata`、`local`、`hybrid`、`priority`；`priority` 按域配置 provider 顺序。
4. 文本资料（TXT/MD/PDF/Word）只做定性阅读，`numeric_extraction` 强制 `false`。
5. `forbid_llm_fill = true` 强制开启：数据缺失时输出 `missing` 与原因，禁止模型补数值。
6. 全源失败不抛栈：返回 `data_source_status=missing`，CLI 以退出码 4 结束。

## 后果

- 离线可用：`mode=local` 时不依赖任何底座服务，`data/local/{domain}/{code}/{date}.json` 即可复现。
- 回测可以把 `data_as_of` 当硬约束，天然排除未来函数。
- provider 增加不影响工作流代码，只需在 `data_source.toml` 注册与路由。
- 代价是多一层信封构造与状态标注，节点必须显式处理 `degraded` / `missing`。

## 相关

- `config/data_source.toml`
- `src/stock_senate/config/schema.py` `_check_data_source`
- `docs/specs/requirements-traceability.md` §7
