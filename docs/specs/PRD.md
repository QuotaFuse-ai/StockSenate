# StockSenate 产品需求文档（PRD）

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 状态 | 待评审 |
| 产品名 | StockSenate |
| 中文名 | StockSenate 多角色投研合议系统 |
| PyPI | `stocksenate` |
| Python 包 | `stock_senate` |
| CLI | `ssenate`（入口 `stock_senate.cli:main`） |
| 数据底座 | TickHub-CN、TickHub-US（资讯域经 TickHub-CN） |
| 来源文档 | `councli.docx` v1.1、`councli文档.docx` v1.2（含独立 WebUI） |
| 需求来源 | QUOT-49 |
| 作者 | 产品经理 |

---

## 1. 背景与问题

「多角色辩论 + LangGraph」在 A 股文本上的可行性已有公开实践验证，但大多数实现停留在研究演示：数据直连免费源、没有回测、没有告警、没有队列、没有质量闸门，工程化能力几乎为零。StockSenate 立项要正面解决六个结构性问题：

1. **数据层与业务层耦合**：采集、限流、降级写在图节点里，任何数据源抖动都会让整次分析失败（Issue 102）。
2. **回测名不副实**：只有事后归因（`performance`），没有滑点、手续费、持仓、重叠窗口，不能回答「这套判断在过去赚不赚钱」（Issue 105①）。
3. **告警能力缺失**：只有静态规则描述，没有去重、升级、通道重试与签名（Issue 105②）。
4. **成本不可控**：一次分析 30~50 次 LLM 调用，没有预算上限、没有熔断、没有批量队列。
5. **输出质量不可控**：弱模型的 tool-calling 不稳定，会产生空报告；没有 Schema 约束、没有质量评分。
6. **工程基线缺失**：配置散落、日志弱、无 CI、无 lock file、依赖冲突（httpx）无解。

StockSenate 的目标不是「再加几个 Agent」，而是把上述六项变成可交付、可验证、可运维的能力。

---

## 2. 产品定位与边界

### 2.1 是什么

面向研究与教学的**多角色投研合议框架**：7 个分析师 + 多空辩论 + 研究经理 + 交易员 + 三档风险辩论 + 投资经理终审，产出结构化投研报告与方向性结论，并支持回测推演与告警。

### 2.2 不是什么（硬边界，写进代码而不是写进免责声明）

| 不做 | 落地方式 |
|---|---|
| 实盘交易、自动下单 | 代码中不存在下单/券商接口；`output_template.toml` 的 `forbidden_fields` 校验会拒绝 `order_id`/`broker_account`/`auto_order` |
| 输出可执行价位 | 不产出建仓价、止损位、目标价、仓位比例；Trader 与 PM 只给方向、评级与理由 |
| 荐股与投资建议 | README、报告页脚、WebUI 首屏统一声明；不托管服务、不代为分析 |
| 重复实现数据采集 | 行情/财报/公告/研报/资金/风险事件/资讯一律经 DataAccessLayer 向 TickHub 或本地 Provider 取 |
| 用 LLM 记忆补数值 | 数据缺失标 `missing` 或 `degraded`，禁止生成数字 |
| 从文本资料里抽数值 | `TextDocProvider` 只做定性阅读，`numeric_extraction = false` |

### 2.3 与 TickHub 的职责边界

| 能力 | StockSenate 核心 | WebUI | TickHub 底座 |
|---|---|---|---|
| 多角色分析、辩论、终审 | 实现 | 展示/操作 | — |
| 回测、告警、报告 | 实现 | 展示/操作 | — |
| 数据采集、缓存、代理池、降级 | 消费 | — | 实现 |
| 字段映射、风险事件结构化 | 消费 | 展示 | 实现 |
| 资讯抓取与过滤（`news` / `social_buzz` / `policy_feed`） | 消费 | 展示 | 实现 |
| MCP 协议服务 | 实现 | — | 实现 |
| 运维监控面板 | — | 只读跳转 | 实现 |

> Newsieve 在 PyPI 上不可获取（404），资讯三域自 2026-10-26 起由 TickHub-CN 的 `eastmoney_stock_news` / `cls_telegraph` / `cctv_news` 提供，见 DEC-009。

原则：StockSenate **不直连**东财 / 巨潮 / 通达信 / 同花顺；WebUI 只做业务界面；MCP Server 与 WebUI 分进程，WebUI 不得污染 MCP stdio 的 stdout。

---

## 3. 目标用户与场景

| 用户 | 场景 | 关键诉求 |
|---|---|---|
| 个人研究者 | 对单只标的做一次完整合议，读报告 | 一次命令跑完，输出可比对、可追溯 |
| 投研小组 | 每天批量跑一篮子标的，统一模板 | 队列、并发、预算上限、失败跳过 |
| 教学者 | 演示多 Agent 辩论如何收敛 | 拓扑可配、过程可观察、可复现 |
| 量化/数据工程师 | 把系统接进自己的流水线 | MCP / Python API / 稳定退出码 |
| 平台运维 | 看服务状态与数据健康 | 端口分离、结构化日志、trace_id |

### 3.1 核心用户故事

- **US-01** 作为研究者，我运行 `ssenate analyze 600519`，在 30 分钟内拿到包含五个分区的完整报告和一份 `summary.json`，其中每条结论都能追到数据来源与 `data_as_of`。
- **US-02** 作为研究者，当某个数据源挂掉时，我看到报告里明确标注 `degraded` 与缺失域，而不是拿到一份悄悄用旧数据或模型猜测拼出来的报告。
- **US-03** 作为投研小组成员，我把 50 只标的丢进队列，设置并发 3 与每日预算，任务按优先级跑，超预算的自动暂停并告警。
- **US-04** 作为教学者，我把 `max_debate_rounds` 从 2 改成 5，观察辩论收敛评分的变化，并导出两次运行的字段级 diff。
- **US-05** 作为量化工程师，我通过 MCP 的 `analysis` 工具发起分析，用 `report` 工具取回结构化结果，不需要解析日志。
- **US-06** 作为运维，我用同一个 `trace_id` 在日志、追踪、用量记录之间对齐一次失败运行。
- **US-07** 作为风控负责人，我在 `after_trader` 检查点拦截一次我不认可的交易方案，修改意见后让它继续。
- **US-08** 作为新用户，我装上包后 `ssenate config init` 生成七个配置文件，`ssenate config validate` 明确告诉我哪一行配错了。

---

## 4. 功能清单

### 4.1 P0（无此不能交付）

| ID | 功能 | 验收要点 |
|---|---|---|
| F-01 | 七分析师 + 多空辩论 + 研究经理 + 交易员 + 三档风险辩论 + 投资经理终审 | 拓扑固定且可审计；每节点输出符合 Pydantic Schema |
| F-02 | `SenateGraph.propagate(code, date)` 保持 `(final_state, decision)` 返回 | 签名稳定；`TradingAgentsGraph` 别名可用 |
| F-03 | DataAccessLayer + `DataEnvelope` 统一契约 | 六字段齐全；`freshness`/`confidence`/`data_source_status` 枚举受限 |
| F-04 | 四种数据模式 `stockdata / local / hybrid / priority` | 按域优先级路由；回退留痕；全源失败返回 `missing` 而非抛栈 |
| F-05 | TickHub-CN / TickHub-US 接入（MCP stdio 或本地 import），资讯三域由 TickHub-CN 承担 | 断网时可切本地；stdio 模式 stdout 纯净 |
| F-06 | 角色配置 `agent_roles.toml`，内置 7 + 自定义 5，上限 12 | 超限报错；工具权限按角色隔离 |
| F-07 | 细粒度 `role_llms` | 16 个合法键全支持；错键报错；同 provider+model 复用客户端 |
| F-08 | 工作流可配 `workflow.toml` | 分析师可增删；轮数可调；不可跳过终审 |
| F-09 | CLI `ssenate` 全命令 + 稳定退出码 | `--help` 可读；json/table/csv 走 stdout，日志走 stderr |
| F-10 | 报告输出：五个分区 + `summary.json` + 数据附录 | 与 CLI/WebUI/MCP 三处一致 |
| F-11 | 配置统一为七个 toml + 校验 | `config validate` 定位到文件与键 |
| F-12 | 结构化日志 + `trace_id` | 一次运行一个 id，贯穿日志/追踪/用量 |
| F-13 | 兼容层：`tradingagents` 命令、旧配置目录迁移、旧环境变量前缀 | 旧命令打印弃用警告；旧目录只读迁移 |

### 4.2 P1（企业级能力，v1.0 必须完整）

| ID | 功能 | 验收要点 |
|---|---|---|
| F-14 | 每节点 Pydantic Schema + 本地重试 + `partial` 标记 | 弱模型空输出被捕获并标 `partial`，不静默消失 |
| F-15 | 安全护栏：注入剥离 + 合规过滤 | 外部文本中的指令不改变系统行为 |
| F-16 | HITL：`after_trader` / `before_final` 两个检查点 | 超时策略可配；自动放行必须留痕 |
| F-17 | LLM-as-a-Judge 质量闸门 | 三维评分；<70 标 `low_quality` 且不阻断交付 |
| F-18 | Map-Reduce 上下文压缩 | 超阈值触发；引用不丢 |
| F-19 | Agent Memory：短期图状态 + 长期反思库 | 长期库可关闭；不得跨标的污染 |
| F-20 | 全链路追踪 OpenTelemetry / Langfuse | 可关；trace_id 与日志对得上 |
| F-21 | 任务熔断与预算 | 调用数/token/时长三道上限；命中行为可配 |
| F-22 | 批量队列 | 并发、限速、重试/跳过、暂停/恢复/取消 |
| F-23 | 告警引擎 | 三级、去重窗口、静默时段、Webhook 签名与幂等 |
| F-24 | 回测引擎 | 滑点、手续费、持仓、重叠窗口、净值曲线、方向准确率、alpha 分桶、Buy→Sell 衰减、防未来函数 |
| F-25 | 报告快照与字段级 diff | 两次运行可对比到字段 |
| F-26 | 多格式输出 | md/json 必选；pdf/docx 依赖缺失时降级并注明 |
| F-27 | MCP Server：stdio + Streamable HTTP | http 默认 127.0.0.1 + 鉴权；五个工具齐备 |
| F-28 | 独立 WebUI | 端口 8788；与 MCP / TickHub Admin 分进程 |
| F-29 | 增量分析 | 同日同标的复用未变化域；公告/风险事件触发失效 |
| F-30 | 选股去重 | 多策略候选合并后按代码唯一 |
| F-31 | CI + lock file + 依赖审计 | ruff/mypy/pytest/pip-audit；`uv.lock` 入库 |

### 4.3 P2（增强，可排到 v1.1）

| ID | 功能 | 说明 |
|---|---|---|
| F-32 | Docker / compose 一键部署 | `ssenate` + `stocksenate-webui` 两个服务 |
| F-33 | PDF 报告样式模板 | 版式可换 |
| F-34 | WebUI 历史筛选与收藏 | 按标的/日期/评级 |
| F-35 | 自定义角色可视化编辑 | 仍受 12 角色上限约束 |
| F-36 | 用量成本估算与看板 | 依赖 `budget.toml` 单价表 |

---

## 5. 关键流程

### 5.1 单次分析

```
ssenate analyze 600519 --date 2026-10-24
  → 加载配置（7 个 toml，未知键报错）
  → 建 trace_id
  → DataAccessLayer 按域路由取数，产出一组 DataEnvelope
  → 7 个分析师并行 → Schema 校验（失败重试 → partial）
  → 多空辩论（轮数/收敛可配）→ 研究经理结论
  → 交易员方案 → HITL(after_trader)
  → 三档风险辩论 → 投资经理终审 → HITL(before_final)
  → 质量闸门评分（<70 → low_quality）
  → 写报告 + summary.json + 快照 + 用量
  → 返回 (final_state, decision)
```

### 5.2 回测

`ssenate backtest --codes ... --start ... --end ...` 逐窗口取「分析日之前」的数据做决策，按下一交易日开盘成交、计滑点与手续费、按窗口持有，最后输出净值曲线、方向准确率、alpha 分桶与 Buy→Sell 衰减。任何 `data_as_of` 晚于分析日的数据直接判失败（防未来函数）。

### 5.3 批量队列

入队 → 按优先级排队 → worker 按并发执行 → 命中限速则退避 → 单任务失败按策略重试或跳过 → 命中预算熔断则整队暂停并告警。

---

## 6. 非功能需求

| 维度 | 要求 |
|---|---|
| 性能 | 单标的默认配置端到端 ≤ 30 分钟；`ssenate --help` ≤ 1 秒（引擎模块懒加载） |
| 可用性 | 单数据源失败不导致整次分析失败；全源失败给出可读原因 |
| 可观测 | 每个节点有结构化日志与 span；日志默认脱敏 API key |
| 安全 | Webhook 签名 + 幂等键；MCP http 默认仅本机 + token；配置不落明文密钥（走环境变量） |
| 兼容 | Python ≥ 3.11；既有 Python API、CLI、配置目录、环境变量均有迁移方案 |
| 许可 | Apache-2.0，保留第三方归属 `NOTICE` |
| 可测试 | 无 LLM 也能跑通 CLI/配置/数据契约的单测；回测有固定快照 |

---

## 7. 依赖与风险

| 风险 | 影响 | 对策 |
|---|---|---|
| httpx 结构性冲突（mootdx `<0.26` vs google-genai `>=0.28.1`） | 全量 `uv sync` 失败 | 不直接依赖 httpx；不提供 `[google]` extra；需要时显式安装（DEC-007） |
| 弱模型 tool-calling 不稳定 | 报告空白 | Schema 强约束 + 本地重试 + `partial` 标记（已定，F-14） |
| 数据源限流/封禁 | 取数失败 | 交给 TickHub 底座；本地 JSON 兜底 |
| LLM 成本失控 | 账单 | 预算 + 熔断 + 队列（F-21/F-22） |
| 输出被误读为投资建议 | 合规风险 | 硬边界 + 报告页脚 + 不含价位（§2.2） |
| WebUI 与 MCP 互相污染 stdout | MCP 协议破裂 | 分进程 + stdout 纯净性测试 |

---

## 8. 里程碑

| 里程碑 | 内容 | 退出标准 |
|---|---|---|
| M0 | 骨架、CLI、配置、数据契约、Schema、日志 | `pip install -e .` 后 `ssenate --help` / `config validate` 可用；契约单测通过 |
| M1 | 七分析师 + 辩论 + 终审 + 数据接入 + 报告 | 单标的端到端产出五分区报告 |
| M2 | 质量闸门、HITL、告警、MCP、追踪、用量 | 五个 MCP 工具可用；告警可试跑 |
| M3 | 回测、队列、WebUI、增量、选股去重 | 回测可复现；WebUI 8788 可操作 |
| M4 | 打包发布、Docker、CI 全绿、文档对齐 | `python -m build` + `twine check` 通过 |

---

## 9. 验收总则

1. 文档 v1.1 §1–§19 与 v1.2 全部章节，逐条在 `requirements-traceability.md` 有对应模块与验收方式，无空项。
2. 功能验收以「可重复运行的命令 + 可断言的输出」为准，不以截图或口头说明为准。
3. 安全边界（不实盘、不产出价位、不泄露密钥、Webhook 验签、MCP 鉴权）由安全工程师独立复核。
4. 兼容项（Python API 签名、CLI 旧命令、配置目录迁移、环境变量前缀）必须有自动化测试。
5. 任一 P0 项缺失即不进入验收。

---

## 10. 变更记录

| 日期 | 版本 | 变更 | 依据 |
|---|---|---|---|
| 2026-10-26 | v1.1 | 数据底座移除 Newsieve（PyPI 404，不可获取）；`news` / `social_buzz` / `policy_feed` 三域改由 TickHub-CN 提供；`[backtest]` extra 移除（回测为纯 Python，无 pandas/numpy import）；默认 LLM 所需 `openai` / `anthropic` 声明为必装依赖；`[mcp]` 下限提到 `mcp>=2` | QUOT-56、DEC-009 |
