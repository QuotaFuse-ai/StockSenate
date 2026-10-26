# TASK-QUOT-50 交付说明（M0–M3 引擎落地）

- 需求来源：QUOT-50「StockSenate M0-M3 引擎实现」
- 交付日期：2026-10-26
- 当前阶段：M0–M3 可运行；`SenateGraph.propagate` 由占位抛错变为完整流水线
- 权威需求：`docs/specs/requirements-traceability.md`（状态列已按本次交付刷新）
- 基线：`docs/tasks/TASK-QUOT-49/README.md`

## 一、结论

`ssenate` 从「契约可用」进入「功能可用」：离线 provider + 本地归档即可跑通
7 分析师 → 多空辩论 → 研究经理 → 交易员 → 三档风险 → 投资经理终审的完整链路，
并产出落盘报告；回测、告警、队列、MCP、WebUI 均有可执行入口与测试。

验收结果：

| 项 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `pytest -q` | 99 passed（连续 3 次全绿） |
| 配置校验 | `ssenate config validate --config config` | `ok: true`，无 error/warning，退出码 0 |
| 追溯矩阵 | 120 条需求行 | 已实现 104 / 待实现 11 / 骨架 4 / 进行中 1（18.2） |

测试分布：alerts 31、backtest 9、config_schema 9、mcp 13、screening 10、queue 8、
webui 7、cli 7、contracts 4、webui_parity 1。

## 二、里程碑实现明细

### M0 核心

| 能力 | 实现 |
|---|---|
| `propagate(code, date, *, hitl, overrides)` | `graph/senate_graph.py` 真执行并返回 `(final_state, decision)`；`decision` 含 `run_id/trace_id/rating/action/execution=false/horizon/confidence/reasons/risks/partial/low_quality/quality_score/hitl/data/budget_tripped/skipped/disclaimer` |
| 数据层 | `data/envelope.py`（九字段 + 状态枚举 + `forbid_llm_fill`）、`data/router.py`（`stockdata/local/hybrid/priority` 四模式、按域优先级、失败留痕）、`data/codes.py`、`data/sanitize.py` |
| Provider | `data/providers/`：TickHub-CN / TickHub-US / Newsieve / JSON / TextDoc / SQL / Composite，`local_json` 布局 `{domain}/{code}/{date}.json` |
| 配置 | `config/loader.py`（`STOCKSENATE_*` 优先、旧前缀告警、deep merge）、`config/schema.py` 强化校验、`home_dir()` 指向 `~/.stock_senate/`；`config migrate` 只读迁移旧目录 |
| 可观测 | `observability/logging.py`：stdout 只走结果、日志走 stderr、`trace_id` 贯穿、key/token/DSN 脱敏 |

### M1 流水线

| 能力 | 实现 |
|---|---|
| 角色 | `roles/registry.py`（六项自描述字段、别名归一、12 席位上限）、`roles/tool_gate.py`（越权结构化错误、自定义角色只读）、`roles/schemas.py`（弱模型输出强约束）、`roles/llm.py`（快/深分层 + 同 provider/model 复用） |
| 节点 | `workflow/nodes/`（analysts / debate_nodes / managers / quality），`workflow/offline.py` 提供确定性离线模型 |
| 辩论与核验 | `workflow/debate.py` 收敛打分 + 阈值提前结束并记录原因；`workflow/verify.py` 数值必须来自结构化数据，冲突标 `conflict` |
| HITL | `workflow/hitl.py` 两个强制挂起点，同意/驳回/修改/超时四动作，自动放行留痕 |
| 预算 | `workflow/budget.py` 三道上限，命中交付部分结果并标 `partial` |
| CLI 参数 | `analyze --enable/--disable/--weight/--no-hitl` 生效并写入报告 |
| 报告 | `report/store.py` 快照落盘、`report/render.py` 四类视图、`report/diff.py` 字段级差异、`report/performance.py` 事后归因 |

### M2 回测 / 告警 / 队列

| 能力 | 实现 |
|---|---|
| 回测 | `backtest/engine.py`、`backtest/models.py`；`ssenate backtest run` 支持滑点、手续费、仓位、重叠窗口、净值/方向准确率/alpha/信号衰减 |
| 告警 | `alerts/rules.py`（DSL + 三级）、`alerts/state.py`（去重/状态机）、`alerts/channels/`（JSON / Webhook 验签幂等 / WebUI）；`ssenate alert list|rules validate|test` |
| 队列 | `queue/manager.py`（并发、限速、重试/跳过、暂停/恢复/取消）、`queue/locks.py` 同标的不重复分析；`ssenate queue status|submit|cancel|pause|resume` |
| 选股 | `screening/dedup.py` 代码归一与多策略合并去重 |
| 退出码 | 0/2/3/4/7/8 在各入口一致 |

### M3 MCP + WebUI

| 能力 | 实现 |
|---|---|
| MCP | `mcp_server/server.py` 双传输 stdio / Streamable HTTP（127.0.0.1:8765，服务名 `stock-senate`，非本机必须 token；stdio stdout 只有 JSON-RPC）、`mcp_server/tools.py` 五个 `stock_senate_*` 工具 |
| WebUI | `webui/launch.py` + `webui/pages/`（十页：分析/进度/报告/Diff/回测/告警/队列/设置/历史/运维只读）、`webui/auth.py`（登录、CSRF、写操作审计、无密钥回显）、默认 8788 |
| 一致性与归因 | `report/performance.py`、`sync-news` 数据通道；`tests/test_webui_parity.py` 断言同一 `run_id` 在 CLI / MCP / WebUI 三处一致 |

## 三、实现中确认的关键约束

1. **回测成交模型**：信号在下一交易日开盘成交（T+1），按 `slippage` 调整；涨跌停日不假设价格而是跳过/顺延并计数；按 100 股整手下单、双边收费；多标的等额分仓；取数固定 `as_of = min(end, today)`，晚于时点的 bar 丢弃，`data_as_of` 晚于分析日的信封直接拒绝（`metrics.no_lookahead=true`）。
2. **`local_json` 解包规则**：`{"data": {...}}` 只在同层存在 `meta` 或任一信封键时才解包；裸 `data` 字段保持原样，避免把业务字段误当信封。
3. **Provider 域门控**：`Provider.supports(domain)` 只有在 `domains` 为空或包含该域时才为真；覆盖部分 provider 时必须同时覆盖 `domains`，否则该域会被静默跳过并返回 `missing`（`attempts` 为空）。发布配置 `local_json.domains` 已限定为五类证券域。
4. **MCP token 环境变量**：按 `STOCKSENATE_MCP_TOKEN` → `STOCKSENATE_MCP_HTTP_TOKEN` → 旧前缀顺序解析；空值视为未配置，非本机绑定直接拒绝启动。
5. **脱敏规则**：`tokens`（预算计数）不脱敏、`token`（凭据）脱敏；`*_env` 键名不算密钥；值侧额外匹配 `sk-…` 与 `Bearer …`。
6. **持久化顺序**：`state["decision"]` 必须先赋值为返回用 payload 再落盘，否则快照只剩 `FinalDecision`（`action=null`、无 `data`/`execution`）。该缺陷由 `test_webui_parity` 发现并修复。
7. **WebUI 技术栈**：以 Starlette + uvicorn 交付而非 Streamlit，理由见 DEC-008；`[webui]` extra 同步改为 `starlette + uvicorn`。
8. **stdio 测试方式**：客户端逐条发送并等待回包后再关闭 stdin；一次性写入后立刻 EOF 会在负载下取消未完成的响应（已改为交互式会话）。

## 四、与 PRD / 任务书口径的差异

- 任务书把回测/告警/队列列为 M2、MCP/WebUI 列为 M3；PRD §8 的里程碑表切分更细。本次以任务书交付范围为准，四项全部落地，追溯矩阵逐行标注验收证据。
- WebUI 由 Streamlit 改为 Starlette（DEC-008），PRD 中「Streamlit」描述按 DEC-008 解读。
- 质量门当前是规则式三维校验（`workflow/nodes/quality.py`），不是 LLM-as-a-Judge 模型评分；追溯矩阵 §9.4 已如实注明。

## 五、未完成项（追溯矩阵保留 骨架 / 待实现）

| 项 | 现状 |
|---|---|
| 增量分析（重复标的只拉新增 + 历史摘要） | 未实现；当前每次全量分析 |
| LangGraph checkpoint 恢复 | 未实现；进程中断需重跑 |
| 短期记忆 / 长期反思（PGVector）/ Map-Reduce 压缩 | 未实现 |
| OpenTelemetry / Langfuse 导出 | `trace_id` 已贯穿，未接后端 |
| `release.yml`（build + twine）与 `uv.lock` | 未实现 |
| 自定义节点/边的任意拓扑 | `workflow.toml` 可配轮次/阈值/HITL，任意拓扑未实现 |
| 各 provider 独立契约测试、浏览器 E2E、alpha 分桶 | 待补 |
| Claude Agent SDK provider、旧 `data_vendors` 键映射 | 未实现 |
| Docker/compose 构建验证 | 文件就绪，未构建 |

## 六、验证命令

```bash
export PYTHONIOENCODING=utf-8
export PYTHONPATH=/d/workplace/StockSenate/src
PY=/d/workplace/ruanjian/venv/Scripts/python.exe

$PY -m pytest -q                                              # 99 passed
$PY -m stock_senate.cli config validate --config config       # ok: true
STOCKSENATE_LLM_PROVIDER=offline STOCKSENATE_DATA_MODE=local \
  $PY -m stock_senate.cli analyze 600519 --config config --output json --no-hitl
$PY -m stock_senate.cli mcp stdio                             # stdout 仅 JSON-RPC
$PY -m stock_senate.cli webui --host 127.0.0.1 --port 8788
```

## 七、变更文件

- 新增实现：`data/`（envelope、router、codes、sanitize、providers）、`roles/`（registry、tool_gate、schemas、llm）、`workflow/`（nodes、debate、verify、hitl、budget、offline、context、digest）、`backtest/`、`alerts/`、`queue/`、`report/`（store、render、diff、performance）、`mcp_server/`、`webui/`、`observability/logging.py`、`screening/dedup.py`、`config/edit.py`、`config/loader.py`
- 修改：`graph/senate_graph.py`、`cli.py`、`config/schema.py`、`config/workflow.toml`（`[queue]` 段）、`data/__init__.py`、`webui/__init__.py`、`pyproject.toml`（`[webui]` extra）、`README.md`、`tests/test_contracts.py`
- 新增测试：`tests/test_alerts.py`、`test_backtest.py`、`test_mcp.py`、`test_queue.py`、`test_screening.py`、`test_webui.py`、`test_webui_parity.py`
- 文档：`docs/specs/requirements-traceability.md`（状态列 + 更新记录）、`docs/decisions/DEC-008-webui-starlette.md`、本文件
