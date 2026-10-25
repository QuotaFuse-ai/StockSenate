# TASK-QUOT-49 交付说明（基线 v0.1.0）

- 需求来源：QUOT-49「开发StockSenate」
- 交付日期：2026-10-26
- 当前阶段：基线就绪，引擎按 M0–M3 实现中
- 权威需求：`councli.docx` v1.1（19 章）、`councli文档.docx` v1.2（20 章）；差异已归一进 `docs/decisions/`

## 交付清单

| 类别 | 文件 | 说明 |
|---|---|---|
| 产品文档 | `docs/specs/PRD.md`、`cli-reference.md`、`priority.md` | 定位、边界、31 项功能、CLI 契约、优先级 |
| 需求追溯 | `docs/specs/requirements-traceability.md` | 逐章映射 + 附录 A 安全清单 S-01…S-10 |
| 决策记录 | `docs/decisions/DEC-001…DEC-007` | 命名、数据层、端口、回测语义、角色键、无价位、httpx |
| 配置 | `config/*.toml`（7 个） | 角色、工作流、输出、数据源、模型、告警、预算 |
| 提示词 | `prompts/`（18 个） | 7 分析师 + 多空 + 经理 + 交易员 + 3 风险 + 闸门 |
| 代码骨架 | `src/stock_senate/` | CLI 全命令面、配置校验、图契约 `SenateGraph.propagate` |
| 测试 | `tests/`（20 例） | CLI 冒烟、配置约束、命名/返回契约 |
| 工程基线 | `pyproject.toml`、`LICENSE`、`NOTICE`、`.gitignore`、`.env.example` | 打包、归属、环境变量 |
| 部署 | `Dockerfile`、`docker/entrypoint.sh`、`docker-compose.yml` | webui 8788 / mcp 8765，仅绑 127.0.0.1 |
| CI | `.github/workflows/ci.yml` | lint / type / test（win+linux × 3.11/3.12）/ audit / package / 依赖隔离 |

## 当前能力边界

已可用（有测试）：

- `ssenate config validate|migrate|init|path|show`
- `ssenate role list|show|validate`（enable/disable 待 M1）
- `ssenate version`、`--help`、`tradingagents` 弃用入口
- `config/schema.py` 强制：16 个 role_llms 键、别名归一、12 分析师席位、
  `numeric_extraction=false`、`forbid_llm_fill=true`、`deny_write_tools=true`、
  禁用未来函数、输出模板禁止交易字段

未实现（骨架）：`analyze`、`backtest`、`alert`、`report`、`queue`、`mcp`、`webui`、`performance`、`sync-news` 的执行体；`SenateGraph.propagate` 目前抛 `NotImplementedError`。

## 验证命令

```bash
export PYTHONIOENCODING=utf-8
export PYTHONPATH=/d/workplace/StockSenate/src
PY=/d/workplace/ruanjian/venv/Scripts/python.exe

$PY -m pytest -q                                   # 20 passed
$PY -m stock_senate config validate --config config --output json
$PY -m stock_senate role list --config config --output json   # 16 行
```

注意：本地 venv 未装 `ruff`，lint 由 CI 执行。

## 分工与后续

| 顺序 | 角色 | 范围 |
|---|---|---|
| 1 | 后端架构师 | M0 核心骨架：配置加载、数据层与 provider、Schema、图节点接入、analyze 端到端 |
| 2 | 后端架构师 | M1 分析流水线：7 分析师、辩论、经理、交易员、风险、终审、HITL、报告 |
| 3 | 后端架构师 | M2 回测 / 告警 / 队列 / 增量 / 预算熔断 |
| 4 | 后端架构师 | M3 MCP 双传输 + WebUI 九页 |
| 5 | 测试工程师 | 按 `requirements-traceability.md` 逐条对比 v1.1/v1.2 并补集成/E2E/回归 |
| 6 | 安全工程师 | 按附录 A S-01…S-10 复核 |

## 待人工确认

- GitHub 推送：本地仓库已初始化并提交，`https://github.com/QuotaFuse-ai/StockSenate.git` 需要凭据，未经确认不推送。
- PyPI 名称 `stocksenate` 是否可用（`pip index versions stocksenate`）。
