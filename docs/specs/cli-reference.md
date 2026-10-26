# CLI / Python API / MCP 手册

本文档是 `ssenate` 的对外契约。**改动命令名、参数名或退出码属于破坏性变更**，必须同步更新本文件与 `requirements-traceability.md`。

- 入口：`stock_senate.cli:main`
- 旧入口：`tradingagents` → 同一程序，启动时在 stderr 打印弃用警告
- 输出约定：`table/json/csv` 结果写 **stdout**；日志、警告、进度写 **stderr**
- 退出码：`0` 成功 · `2` 参数错误 · `3` 配置错误 · `4` 数据源不可用 · `7` 限流 · `8` 部分失败

---

## 1. 命令总览

```
ssenate --version | -V
ssenate version
ssenate analyze CODE [OPTIONS]
ssenate role list|show|validate|enable|disable
ssenate backtest run --start ... --end ... [--code ...]
ssenate alert list|test|serve
ssenate alert rules validate
ssenate report list|show|diff|export
ssenate queue status|submit|cancel|pause|resume
ssenate mcp stdio|http
ssenate webui [--host] [--port]
ssenate config show|path|init|validate|migrate
ssenate performance [--run-id]
ssenate sync-news [--since]
```

---

## 2. 命令详解

### 2.1 `ssenate analyze`

运行一次完整合议。

```
ssenate analyze 600519 --date 2026-10-24 --output json
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `CODE` | 必填 | 标的代码，如 `600519`、`000001` |
| `--date, -d` | 今天 | 分析基准日 `YYYY-MM-DD`；`data_as_of` 不得晚于该日 |
| `--config, -c` | `~/.stock_senate` | 配置目录 |
| `--output, -o` | `table` | `table` / `json` / `csv` |
| `--enable` | 空 | 本次临时启用自定义角色，可重复 |
| `--disable` | 空 | 本次临时停用角色，可重复 |
| `--weight` | 空 | `角色键=权重`，可重复，如 `custom_macro=0.6` |
| `--no-hitl` | 关闭 | 跳过人工检查点；报告中记录 `auto_approved` |

退出码：成功 `0`；决策被标 `partial` 时 `8`；全源失败 `4`；配置非法 `3`。

### 2.2 `ssenate role`

| 子命令 | 说明 | 关键参数 |
|---|---|---|
| `list` | 列出角色、启用状态、模型 | `--config`, `--output` |
| `show NAME` | 单个角色详情 | `NAME` |
| `validate` | 校验数量上限（分析师 ≤12）、Schema、工具白名单 | `--config` |
| `enable NAME` | 写回 `agent_roles.toml`（需审计） | `NAME` |
| `disable NAME` | 写回 `agent_roles.toml`（需审计） | `NAME` |

角色键（稳定契约）：

```
market social news fundamentals policy hot_money lockup
bull bear research_manager trader
risk_aggressive risk_neutral risk_conservative
portfolio_manager quality_gate
```

别名可用并归一：`technical→market`、`basic→fundamentals`、`sentiment→social`、`unlock→lockup`。

### 2.3 `ssenate backtest run`

```
ssenate backtest run --code 600519 --code 000001 --start 2025-01-01 --end 2025-12-31 \
  --window 5 --slippage 0.001 --fee 0.0003
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--code` | 空 | 标的，可重复 |
| `--codes` | 空 | 逗号分隔标的 |
| `--start` / `--end` | 必填 | 回测区间 |
| `--window` | 5 | 每个决策持有交易日数 |
| `--slippage` | 0.0 | 滑点（成交价比例） |
| `--fee` | 0.0 | 单边手续费率 |
| `--out-dir` | 空 | 结果目录 |
| `--config, -c` | `~/.stock_senate` | 配置目录 |

回测**不是** `performance`：前者是策略推演，后者是已保存决策的事后归因。

### 2.4 `ssenate alert`

| 子命令 | 说明 |
|---|---|
| `list` | 列出规则（`--config`, `--output`） |
| `test [--rule ID]` | 在最近数据上试跑，不推送 |
| `serve [--interval 60]` | 常驻轮询 |
| `rules validate` | 校验 `alert_rules.toml`，错误定位到规则与字段 |

### 2.5 `ssenate report`

| 子命令 | 说明 |
|---|---|
| `list [--code]` | 列出快照 |
| `show RUN_ID` | 读取报告 |
| `diff --code --from --to` | 字段级差异、结论变化、风险变化 |
| `export RUN_ID --format md\|json\|pdf\|docx [--out PATH]` | 导出（pdf/docx 需可选依赖，缺失时降级为 md 并注明） |

### 2.6 `ssenate queue`

| 子命令 | 说明 |
|---|---|
| `status [--job-id]` | 队列/任务状态 |
| `submit --file tasks.csv [--concurrency N]` | CSV 列名 `code[,date]` |
| `cancel JOB_ID` / `pause JOB_ID` / `resume JOB_ID` | 取消/暂停/恢复 |

### 2.7 `ssenate mcp`

| 子命令 | 说明 |
|---|---|
| `stdio` | stdout 只输出 MCP 报文，日志全部走 stderr |
| `http [--host 127.0.0.1] [--port 8765]` | Streamable HTTP；非本机访问必须鉴权 |

### 2.8 `ssenate webui`

`--host 127.0.0.1 --port 8788`。独立进程，不寄生 TickHub Admin Console。

### 2.9 `ssenate config`

| 子命令 | 说明 |
|---|---|
| `show` | 打印合并后的配置 |
| `path` | 打印配置目录 |
| `init` | 创建目录并报告缺失文件 |
| `validate` | 校验七个 toml，失败退出 `3` |
| `migrate [--dry-run]` | 把 `~/.tradingagents` 复制到 `~/.stock_senate`，旧目录只读保留 |

### 2.10 `ssenate performance`

已保存决策的事后归因：`direction_accuracy`、`up_rate`、`outperform_rate`、评级单调性。**不含净值曲线、不含收益。**

### 2.11 `ssenate sync-news`

经 TickHub-CN 拉取并过滤资讯（`eastmoney_global_news`），`--since YYYY-MM-DD` 缺省 7 天前。

---

## 3. Python API

```python
from stock_senate import SenateGraph, TradingAgentsGraph  # 同一类

graph = SenateGraph(debug=False, config={"llm_provider": "minimax"})
final_state, decision = graph.propagate("600519", "2026-10-24")
```

契约：

- `propagate(code, date)` 位置参数与返回 `(final_state, decision)` 不变。
- `hitl=` 与 `overrides=` 是仅关键字扩展参数，不影响既有调用方式。
- `decision` 至少包含 `rating`（Buy/Hold/Sell）、`reason`、A 股约束标记；不含任何可执行价位。
- 报告文件是衍生输出，**不是**返回值。

---

## 4. MCP 工具

| 工具 | 入参 | 返回 |
|---|---|---|
| `analysis` | `code`, `date?`, `hitl?` | `{run_id, decision, report_path, data_envelopes}` |
| `backtest` | `codes`, `start`, `end`, `window?`, `slippage?`, `fee?` | `{run_id, metrics, equity_curve_ref}` |
| `alert_query` | `code?`, `level?`, `since?` | `{alerts: [...]}` |
| `report_query` | `run_id` / `code+date` | 结构化报告 |
| `config_query` | `section?` | 生效配置（密钥打码） |

传输与安全：

- `stdio`：stdout 只允许 MCP 帧；任何日志写 stderr。
- `http`：默认 `127.0.0.1:8765`；对外需 `Authorization: Bearer <token>`；与 TickHub MCP 分开命名、鉴权、日志，通过 `trace_id` 串联。

---

## 5. 端口分配

| 服务 | 端口 | 说明 |
|---|---|---|
| TickHub Admin Console | 8787 | 运维面板，不归本项目 |
| StockSenate WebUI | 8788 | 业务界面 |
| StockSenate MCP (http) | 8765 | 默认仅本机 |
| TickHub MCP | 8000 | 底座 |
| QuotaFuse Wire | 8790 / 8791 | 资讯感知层，不冲突 |

---

## 6. 环境变量

| 变量 | 说明 |
|---|---|
| `STOCKSENATE_CONFIG_DIR` | 配置目录覆盖 |
| `STOCKSENATE_HOME` | 数据/缓存根目录（默认 `~/.stock_senate`） |
| `STOCKSENATE_LLM_PROVIDER` / `STOCKSENATE_LLM_API_KEY` / `STOCKSENATE_LLM_BASE_URL` | 模型凭据（也可在 `llm_config.toml` 配置，密钥只走环境变量） |
| `STOCKSENATE_MCP_HTTP_TOKEN` | MCP http 鉴权 token |
| `STOCKSENATE_WEBUI_PASSWORD` | WebUI 登录口令 |
| `STOCKSENATE_ALERT_WEBHOOK` / `STOCKSENATE_ALERT_WEBHOOK_SECRET` | 告警回调地址与签名密钥 |
| `STOCKSENATE_SQL_DSN` | SQL Provider 连接串（只读账号） |
| `STOCKSENATE_TRACE_EXPORTER` | `none` / `otlp` / `langfuse` |

兼容：旧前缀 `QF_COUNCIL_*`、`TRADINGAGENTS_*` 仍可读，**新前缀优先**，并在 stderr 提示迁移。

---

## 7. 配置文件与关键键

| 文件 | 关键键 |
|---|---|
| `agent_roles.toml` | `[roles.*]`（kind/prompt/tools/output_schema/enabled）、`[limits]`（max_roles=12、max_custom_roles=5）、`[tool_permissions]` |
| `workflow.toml` | `[topology]`、`[debate]`、`[hitl]`、`[checkpoint]`、`[incremental]`、`[quality_gate]`、`[compression]`、`[budget]` |
| `output_template.toml` | `[sections.*]`、`forbidden_fields`、`[export]`、`[disclaimer]` |
| `data_source.toml` | `[global]`（mode/forbid_llm_fill）、`[providers.*]`、`[routing]`、`[freshness]` |
| `llm_config.toml` | `[llm]` 七个基础键、`[llm.role_llms]`、`[llm.allowed_keys]`、`[llm.structured_output]` |
| `alert_rules.toml` | `[global]`、`[channels.*]`、`[[rules]]` |
| `budget.toml` | `[per_run]`、`[per_queue]`、`[daily]`、`[circuit_breaker]`、`[pricing]` |

`llm.role_llms` 合法键共 16 个（见 §2.2）；写错键 → `config validate` 退出 `3`，**不会静默回退默认模型**。

---

## 8. 安装

```bash
pip install stocksenate                  # 核心
pip install "stocksenate[all]"           # 数据 + 回测 + MCP + WebUI
pip install -e ".[dev]"                  # 开发

# 需要用 Gemini 时（mootdx 与 google-genai 的 httpx 冲突见 DEC-007）
pip install --no-deps "langchain-google-genai>=4.0.0"
pip install "google-genai>=1.53.0" "httpx>=0.28.1"
```

Python ≥ 3.11。
