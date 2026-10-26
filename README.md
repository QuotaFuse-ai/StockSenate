<h1 align="center">StockSenate</h1>

<p align="center">
  <b>多角色投研合议系统</b><br>
  7 角色研报 · 多空辩论 · 三档风控 · 投决终审 · 回测推演
</p>

<p align="center">
  研究框架，不是投资产品 · 不接实盘 · 不输出可执行价位
</p>

---

StockSenate 是自研的 A 股多角色投研合议系统：
7 位分析师并行产出研报，经多空辩论、研究经理汇总、交易员提案、三档风控审查，由组合经理终审；
数据采集不在议事层实现，统一交给 `TickHub-CN` / `TickHub-US` / `Newsieve` 提供，
StockSenate 自身只负责「多智能体合议 + 回测推演」。

- **完整合议链路**：7 分析师 → 多空辩论 → 研究经理 → 交易员 → 三档风险辩论 → 投资经理终审 → 质量门控，节点与输出契约稳定。
- **数据层独立**：议事层不直连东财/巨潮/通达信；`DataAccessLayer` 统一消费 `TickHub-CN`（A 股）、`TickHub-US`（美/港股）、`Newsieve`（资讯），也可离线读取本地 JSON、文本与 SQL。
- **生产级增强**：Pydantic 输出强约束、单节点局部重试、辩论收敛、增量分析、安全护栏、HITL 审批、LLM-as-a-Judge、全链路追踪、任务熔断、告警引擎、批量队列、独立 WebUI、MCP Server。
- **一套环境装全部模型**：MiniMax / DeepSeek / Qwen / GLM / OpenAI / Anthropic / Google / xAI / OpenRouter / Ollama / OpenAI 兼容网关。

> **定位边界**：本项目面向研究与教学，不构成投资建议，不提供荐股服务。
> 框架内没有建仓价、止损位、目标价、仓位比例这类输出——不是默认关闭，是设计上不做。
> 所有 AI 生成内容均可能有误，投资决策请咨询持牌专业机构。

---

## 能力一览

| 维度 | StockSenate |
|------|-------------|
| 定位 | 多角色投研合议系统（研究 + 回测推演） |
| 数据来源 | `DataAccessLayer` → `TickHub-CN` / `TickHub-US` / `Newsieve`，可插拔本地 JSON / 文本 / SQL |
| 数据采集 | 由数据底座负责；议事层不重复实现采集、缓存、代理池、降级 |
| 分析师 | 7 个（市场/舆情/新闻/基本面 + 政策/游资/解禁），角色提示词内置，输出 Schema 强约束 |
| 辩论与决策 | 多空辩论 → 研究经理 → 交易员 → 三档风控 → 投决，含收敛打分、可配置拓扑、interrupt 人工审批 |
| 模型配置 | 快/深两档 + `role_llms` 细粒度，角色键位稳定 |
| 断点恢复 | SQLite/Postgres checkpoint 与增量分析 |
| 报告 | `summary.json` + `complete_report.md` + Markdown/PDF，含结构化版本快照、字段级 Diff、多格式输出 |
| 决策统计 | `performance` 事后归因保持独立口径；回测引擎是单独能力，两者分开验收 |
| 交互 | CLI + 独立 WebUI（独立进程/端口/鉴权）+ MCP Server（stdio/http） |
| 工程化 | GitHub Actions + `uv.lock` + pip-audit + 全提供商同环境 import smoke test |
| 包名 / 命令 | `stocksenate` / `ssenate`（`tradingagents` 为兼容别名） |

---

## 架构

```text
┌───────────────────────────┐   ┌───────────────────────────┐   ┌───────────────────────────┐
│  StockSenate WebUI        │   │  StockSenate MCP Server   │   │  数据底座控制台（外部）      │
│  独立进程 · 127.0.0.1:8788 │   │  stdio / http :8765       │   │  TickHub Admin :8787      │
└─────────────┬─────────────┘   └─────────────┬─────────────┘   └─────────────┬─────────────┘
              │                               │                               │
              └───────────────┬───────────────┘                               │
                              ▼                                               │
                 ┌──────────────────────────┐                                 │
                 │  SenateGraph 工作流       │   只读展示数据状态 + 跳转          │
                 │  7 分析师 → 多空辩论      │                                 │
                 │  → 研究经理 → 交易员      │                                 │
                 │  → 三档风控 → 投决终审    │                                 │
                 └────────────┬─────────────┘                                 │
                              ▼                                               │
                 ┌──────────────────────────┐                                 │
                 │  DataAccessLayer         │                                 │
                 │  DataEnvelope + 路由 +    │                                 │
                 │  溯源 + 降级标记          │                                 │
                 └────────────┬─────────────┘                                 │
                              ▼                                               │
        ┌─────────────────────┴─────────────────────┐                          │
        ▼                     ▼                     ▼                          │
  TickHub-CN (A 股)     TickHub-US (美/港股)    Newsieve (资讯)  ◄──────────────┘
  MCP stdio/http        MCP stdio/http          MCP stdio/http
  或 local import       或 local import          或 local import
        │                     │                     │
        └──────────┬──────────┴──────────┬──────────┘
                   ▼                     ▼
            本地 JSON 归档          文本 / SQL（只读）
```

**职责边界**

| 能力 | StockSenate | 数据底座（TickHub / Newsieve） |
|------|-------------|-------------------------------|
| 多角色分析、辩论、终审 | ✅ | ❌ |
| 回测、告警、报告、WebUI | ✅ | ❌ |
| 数据采集、缓存、代理池、限流 | ❌ 不重复实现 | ✅ |
| 字段映射、风险事件结构化 | 消费 | ✅ 生产 |
| 数据源健康与降级 | 消费状态并标注置信度 | ✅ 执行 |
| LLM 调用与多模型 | ✅ | ❌ |

---

## 角色体系

默认 7 个分析师全部内置，`role_llms` 键位固定如下：

| 配置键 | 展示名 | 职责 | 阶段 |
|---|---|---|---|
| `fundamentals`（别名 `basic`） | 基本面分析师 | 财报三表、盈利能力、估值 | analyst |
| `market`（别名 `technical`） | 技术面分析师 | K 线形态、技术指标、量价 | analyst |
| `social`（别名 `sentiment`） | 情绪分析师 | 讨论热度、散户情绪 | analyst |
| `news` | 新闻分析师 | 行业新闻、公告、宏观事件 | analyst |
| `policy` | 政策分析师 | 监管政策、产业政策、窗口指导 | analyst |
| `hot_money` | 游资龙虎榜分析师 | 龙虎榜、大单流向、主力资金 | analyst |
| `lockup`（别名 `unlock`） | 解禁监控分析师 | 限售解禁、减持、股权质押 | analyst |
| `bull` / `bear` | 多空辩手 | 多空辩论 | debate |
| `research_manager` | 研究经理 | 综合研判、投资计划 | manager |
| `trader` | 交易员 | A 股约束下的交易方案（方向/评级/理由） | trader |
| `risk_aggressive` / `risk_neutral` / `risk_conservative` | 三档风控 | 风险辩论 | risk |
| `portfolio_manager` | 投资经理 | 终审评级 + 理由 | final |
| `quality_gate` | 质量门控 | 输出质量校验 | gate |

自定义角色通过 `config/agent_roles.toml` 声明，上限默认 12 个（7 内置 + 5 自定义）。
新增角色必须定义：名称与提示词、工具白名单、Pydantic 输出 Schema、模型层级、阶段、是否参与辩论及权重。
自定义角色失败可跳过主流程，报告标注 `partial`。

---

## 安装

需要 **Python ≥ 3.11**（与 TickHub-CN 保持一致）。

```bash
pip install stocksenate              # 核心
pip install "stocksenate[all]"       # 全功能
pip install -e ".[dev]"              # 开发
```

可选 extras：`stockdata`（TickHub 本地直连）、`mcp`（MCP Server）、`webui`（Starlette + uvicorn 控制台，见 DEC-008）、`all`。回测为纯 Python 实现，无独立 extra。

```bash
cp .env.example .env          # 按文件内【必填】备注填写，模板不含任何真实密钥
ssenate config validate       # 启动时自动读取当前目录的 .env；已导出的变量优先
```

---

## 快速开始

### 命令行

```bash
# 单标的分析
ssenate analyze 600000 --date 2024-12-31

# 动态启停角色 / 调权重
ssenate analyze 600000 --date 2024-12-31 --disable custom_macro
ssenate analyze 600000 --date 2024-12-31 --enable custom_macro --weight 0.6

# 角色管理
ssenate role list
ssenate role show fundamentals
ssenate role validate
ssenate role enable custom_macro
ssenate role disable custom_macro

# 回测（基于已保存决策记录）
ssenate backtest run --code 600000 --start 2024-01-01 --end 2024-12-31

# 告警
ssenate alert list
ssenate alert rules validate

# 报告版本 Diff
ssenate report diff --code 600000 --from 2024-11-30 --to 2024-12-31

# 批量队列
ssenate queue status
ssenate queue submit --file tasks.csv

# 配置
ssenate config validate
ssenate config migrate

# MCP Server
ssenate mcp stdio
ssenate mcp http --port 8765

# 独立 WebUI
ssenate webui --host 127.0.0.1 --port 8788
```

完整命令、参数、退出码与 MCP 配置见 [docs/specs/cli-reference.md](docs/specs/cli-reference.md)。

### Python API

```python
from stock_senate import SenateGraph

graph = SenateGraph()
final_state, decision = graph.propagate(code="600000", date="2024-12-31")
print(decision["rating"], decision["reason"])
```

- `propagate(code, date)` 返回 `(final_state, decision)` 二元组，签名稳定。
- `TradingAgentsGraph` 保留为 `SenateGraph` 的别名。
- `summary.json`、`complete_report.md` 是衍生输出，不替代返回值。

### 旧命令兼容

```bash
tradingagents analyze 600000     # 可用，启动时打印弃用警告并提示迁移到 ssenate
```

---

## 配置

所有配置集中在 `config/`，运行时合并用户目录 `~/.stock_senate/`：

| 文件 | 作用 |
|------|------|
| `config/agent_roles.toml` | 角色、提示词、权重、工具白名单、输出 Schema、模型层级 |
| `config/workflow.toml` | 拓扑、并行组、辩论轮次、收敛阈值、终止条件、interrupt 节点 |
| `config/output_template.toml` | 报告模块开关与多格式输出 |
| `config/data_source.toml` | 数据模式、provider 优先级、实时开关、超时与降级 |
| `config/llm_config.toml` | 提供商、模型、`role_llms`、超时、重试、预算 |
| `config/alert_rules.toml` | 告警级别、条件、动作、去重、Webhook |
| `config/budget.toml` | 任务熔断（最大调用数、Token、超时） |

- 旧目录 `~/.tradingagents/` 在首次启动时**只读扫描并迁移**到 `~/.stock_senate/`，旧目录不删除。
- 环境变量前缀 `STOCKSENATE_*`；同时兼容读取旧前缀 `TRADINGAGENTS_*`，新前缀优先。
- 所有配置文件提供 JSON Schema 校验与迁移脚本（`ssenate config validate` / `ssenate config migrate`）。

---

## 数据接入

`DataAccessLayer` 输出统一 `DataEnvelope`：

```python
class DataEnvelope(BaseModel):
    domain: str              # kline / financial / announcement / research / risk_event ...
    code: str
    data_as_of: date
    payload: dict
    source: str              # tickhub_cn / tickhub_us / json / text_doc / sql
    source_detail: str
    fetched_at: datetime
    freshness: str           # realtime / cached / archive
    confidence: str          # high / medium / low
    data_source_status: str  # ok / degraded / missing
```

四种模式（`config/data_source.toml`）：

| 模式 | 语义 |
|------|------|
| `stockdata` | 只走数据底座（TickHub / Newsieve） |
| `local` | 只读本地数据，支持断网回放 |
| `priority` | 按 `[data_source.priority]` 逐级 fallback |
| `hybrid` | 历史/归档优先本地，实时/缺失/刷新走数据底座 |

硬约束：

- 数值类数据（财务、K 线、资金）**只允许**来自数据底座或结构化数据库，禁止从 TXT/PDF/Word 抽取。
- 文本类数据只用于公告、研报、政策、新闻分析。
- 实时采集默认关闭；开启后必须带超时、降级与防重复采集锁。
- 本地源不可用**不得静默返回空**，必须标记 `data_source_status=missing|degraded`。

---

## WebUI 与 MCP

### 独立 WebUI

```bash
ssenate webui --host 127.0.0.1 --port 8788
```

独立进程、独立端口、独立鉴权；默认只监听本机，未配置鉴权令牌时拒绝写操作。
页面：分析工作台、辩论过程、最终报告、历史记录、报告 Diff、回测中心、告警面板、角色管理、工作流拓扑、批量队列、配置校验、数据状态只读卡。

运维监控不在本界面重复建设，只做只读展示并跳转数据底座控制台（默认 `:8787`）。

### MCP Server

```bash
ssenate mcp stdio          # 供 Cursor / Claude Desktop 等客户端
ssenate mcp http --port 8765
```

- stdio 模式 stdout **只输出 MCP 协议报文**，所有日志走 stderr。
- http 模式默认 `127.0.0.1`，对外必须鉴权。
- 与数据底座的 MCP 分开命名、分开鉴权、分开日志，通过 `trace_id` 串联。
- 暴露工具：分析任务、回测、告警查询、报告查询、配置查询。

---

## 回测与 performance 的区别

| | `ssenate backtest` | `ssenate performance` |
|---|---|---|
| 性质 | 策略推演（独立能力） | 决策事后归因 |
| 输入 | 已保存决策记录 + 历史行情 | 已落盘决策记录 |
| 能力 | 滑点、手续费、仓位、重叠窗口、收益曲线、方向准确率、alpha 分组、Buy→Sell alpha 衰减 | 方向正确率、评级分组、评级区分度 |
| 时点 | `data_as_of` 历史时点推演，防未来函数 | 固定持有期回看 |
| 验收 | 固定数据集可复现、无未来函数、无样本泄漏 | 统计口径固定、可复核 |

**不要把 performance 当回测，也不要把回测当策略业绩。**

---

## 兼容与迁移

| 项 | 新值 | 兼容策略 |
|---|---|---|
| PyPI 包 | `stocksenate` | 仅本包；不发布 quotafuse 前缀的包 |
| Python 包 | `stock_senate` | 不提供 `qf_council` shim（从未发布） |
| CLI | `ssenate` | 注册旧命令 `tradingagents`，打印弃用警告 |
| Python API | `SenateGraph` | `TradingAgentsGraph` 别名保留，`propagate` 签名不变 |
| 配置目录 | `~/.stock_senate/` | 首次启动从 `~/.tradingagents/` 只读迁移 |
| 环境变量 | `STOCKSENATE_*` | 兼容读取 `TRADINGAGENTS_*`，新前缀优先 |
| MCP Server 名 | `stock-senate` | 与 `tickhub-cn` 等底座 MCP 分开命名与鉴权 |
| Docker 服务 | `ssenate` / `stocksenate-webui` | 与 `tickhub` / `tickhub-admin` 分离 |

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/specs/PRD.md](docs/specs/PRD.md) | 产品需求：功能清单、用户故事、验收标准、里程碑 |
| [docs/specs/requirements-traceability.md](docs/specs/requirements-traceability.md) | 文档章节 → 模块 / CLI → 验收 全量追溯矩阵 |
| [docs/specs/priority.md](docs/specs/priority.md) | P0 / P1 / P2 优先级与排期 |
| [docs/specs/cli-reference.md](docs/specs/cli-reference.md) | 全部 CLI / Python API / MCP 命令与退出码 |
| [docs/decisions/](docs/decisions/) | 命名、数据层、端口等架构决策（只增不改） |
| `councli.docx` / `councli文档.docx` | 需求原始文档（v1.1 / v1.2） |

---

## 开发

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
mypy src
python -m build
```

CI 覆盖：`pytest` + `ruff` + `mypy` + `pip-audit` + 全提供商同环境 import smoke test + 打包检查。
依赖锁定使用 `uv.lock`；发布包只声明最小依赖范围。

## License

Apache License 2.0。本项目为独立实现，研究框架的语境与术语受
[TradingAgents 论文](https://arxiv.org/abs/2412.20138)启发；
`TradingAgentsGraph` 兼容别名仅为接口便利，不包含第三方项目源码。
归属与第三方许可证声明见 `NOTICE`。
