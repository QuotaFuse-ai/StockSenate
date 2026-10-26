# TASK-QUOT-56 — P0 修复：compose 部署链路、事实抽取、退出码、安装矩阵与 CI 全绿

- 关联 Issue：QUOT-56（stage 3，父单 QUOT-49）
- 执行人：后端架构师
- 完成日期：2026-10-26
- 边界：只改 `D:\workplace\StockSenate`；未修改 `tests/` 下 QUOT-51 的既有断言；未 `git push`；遵守 DEC-007；产品边界不变（只输出研究结论，不输出价格位/仓位/交易指令）。

## 结论摘要

| # | P0 项 | 状态 | 关键证据 |
|---|---|---|---|
| 1 | compose 部署链路不可用 | 已修复（本机无 Docker，未做真实 `up`） | webui/mcp healthcheck 都指向 `/healthz`；`depends_on: service_healthy` 保留 |
| 2 | 5 个数据域抽出 0 事实 / 质量闸门失明 | 已修复 | 真实 TickHub 回放：事实 7→17，有事实域 2→4，`partial` false→true，端到端 `quality_score` 83.3→73.3 |
| 3 | 退出码契约（0/2/3/4/7/8） | 已修复 | 19 个退出码场景测试通过，`tests/` 内 `xfail` 为 0 |
| 4 | CI 全绿 | 本机全绿，CI 待推送确认 | `.[dev]` 范围内 ruff/format/mypy 全清、`pytest` 192 passed、build+twine 通过 |
| 5 | 安装矩阵 | 已修复 | 干净 venv `pip install .` 后 `ssenate --version` 与离线 `analyze` 均正常 |

## 1. compose 部署链路

问题：`docker-compose.yml` 的 webui healthcheck 打的是 Streamlit 的 `/_stcore/health`，而 WebUI 是 Starlette 应用，只提供 `/healthz`。webui 永远不 healthy，导致 `depends_on: service_healthy` 的 mcp 永远不启动。

修复：

- `docker-compose.yml`：webui healthcheck 改为 `curl -fsS http://127.0.0.1:8788/healthz`；mcp healthcheck 同为 `/healthz`；保留 `depends_on: webui: {condition: service_healthy}`。
- `Dockerfile`：`HEALTHCHECK` 改为 `http://127.0.0.1:${STOCKSENATE_WEBUI_PORT:-8788}/healthz`；镜像内安装 `curl` 仅供 healthcheck 使用。
- `docker/entrypoint.sh`：`webui` / `mcp` 分别监听 `STOCKSENATE_WEBUI_PORT` / `STOCKSENATE_MCP_PORT`（默认 8788 / 8765），与 compose 中的端口和探针一致。
- `STOCKSENATE_DEFAULTS_DIR=/app/config`：wheel 不带 `config/`，镜像内默认配置目录必须显式指向挂载点。

代码侧证据：

- `src/stock_senate/webui/auth.py`：`PUBLIC_PATHS` 包含 `/healthz`，并注册 `Route("/healthz", ...)`。
- `src/stock_senate/mcp_server/server.py`：`HEALTH_PATH = "/healthz"`，通过 `custom_route` 暴露。

**诚实降级**：本机 `which docker` → `docker: not found`，因此没有执行真实的 `docker compose up`。已做的是配置/代码一致性核对；真实容器 healthy 状态需在有 Docker 的环境复核。追溯矩阵 18.3 已按此如实描述。

## 2. 事实抽取与质量闸门

问题：真实 TickHub（600519，2026-10-23）下 financial/announcement/money_flow/lockup_calendar/risk_event 抽取 0 条事实却仍为 `status=ok`；质量闸门按“域状态”而非“事实条数”打分，给出 `quality_score=83.3`、`low_quality=false`。

修复：

- `workflow/digest.py`：`_LIST_KEYS` 补齐 `announcements`、`flows`、`events`、`history`、`upcoming` 等 provider 实际使用的包裹键；`_financial_facts` 先读 `indicators`，再回退 `income`；`_lockup_rows` 合并 `upcoming` + `history`。
- `workflow/offline.py`：`quality_verdict` 改为按每个域的 `fact_count > 0` 计算覆盖率；`partial` = 存在 `status=ok` 但 0 事实的域；`low_quality = score < 阈值 or partial`；notes 明确写出“有事实的数据域 X/12（共 N 条事实）”。

受控 A/B（同一批 12 个真实 TickHub 回放载荷；before 用 HEAD 的 `digest.py` + `offline.py`）：

| 数据域 | before 事实数 | after 事实数 |
|---|---:|---:|
| financial | 0 | 7 |
| kline | 5 | 5 |
| money_flow | 0 | 3 |
| research | 2 | 2 |
| announcement | 0 | 0（provider 返回空列表） |
| lockup_calendar | 0 | 0（`history`/`upcoming` 均为空） |
| risk_event | 0 | 0（`events` 为空） |
| dragon_tiger / news / policy_feed / quote / social_buzz | missing | missing |

Digest 级与端到端：

| 指标 | before | after |
|---|---:|---:|
| 总事实数 | 7 | 17 |
| 有事实的数据域 | 2 | 4 |
| digest 质量分 | 23.3 | 13.3 |
| `partial` | false | true |
| 端到端 `quality_score` | 83.3 | 73.3 |
| 端到端 `low_quality` | false | true |
| 端到端 `partial` | false | true |

三个仍为 0 的域已核对原始载荷确实为空（不是解析漏读），因此 0 事实是正确的；闸门现在会如实标记 partial，而不是把它们算作“可用”。

证据文件：

- `evidence/facts-before.json` / `evidence/facts-after.json`
- `evidence/quality-before.json` / `evidence/quality-after.json`

新增测试：`tests/test_digest_quality.py`（4 个用例，覆盖嵌套列表、financial 分段、按事实计分、全有事实时不误报）。

## 3. 退出码契约

- `0/2/3/4/7/8` 全部可达：`LLMRateLimited` 映射为 7（此前声明但不可达）；非法 `--date` 为 2；未知 `--disable` 为 3；无数据 4；部分成功 8。
- `backtest run` 新增 `--config`。
- 环境变量优先级实现为 `STOCKSENATE_CONFIG_DIR > TRADINGAGENTS_CONFIG_DIR > QF_COUNCIL_CONFIG_DIR > ~/.stock_senate`，命中旧变量时向 stderr 打迁移提示。
- `role show technical` 别名可用。
- `tests/` 全目录 `xfail` 为 0；退出码场景测试 19 个全部通过。

**测试断言变更说明（按边界要求在本单说明）**：`test_backtest_unknown_option_exits_2` 原先把 `--config` 当作“未知选项”来断言退出码 2；本单按需求给 `backtest run` 增加了合法 `--config`，该断言前提已不成立，故改为使用真正未知的 `--not-an-option`，断言值与意图（未知选项 → 2）不变。另对 `tests/test_cli_exit_codes.py` 的 `# 7 — rate limited (declared; analyze does not map it yet)` 注释做了纯注释更新（映射已实现），不涉及断言。除此之外未改动任何 QUOT-51 既有断言。

## 4. CI 全绿

复现方式与 CI 一致：仅安装 `.[dev]`。

- `ruff check src tests`：通过。
- `ruff format --check src tests`：102 files already formatted。
- `mypy src`：84 source files，无问题。
- `pytest -o addopts="" -q`：**192 passed**（无 xfail）。
- `python -m build` + `twine check dist/*`：通过（package job 可产出 dist）。
- audit 由 `--strict` 改为 `pip-audit --skip-editable`：`stocksenate` 自身尚未发布到 PyPI，`--strict` 会因为“自己的可编辑包无法审计”置红，与第三方依赖 CVE 无关；`--skip-editable` 保留对第三方依赖的审计语义。

CI 配置同步：`[dev]` 现在携带 `[webui,mcp]`（测试确实 import MCP 与 WebUI，此前 ubuntu 上因缺 `starlette` 收集失败）；package job 依赖 lint+test。

**待推送确认**：`.github/workflows/ci.yml` 的修改尚未推送，因此 run 40254571700 的 7 个红项只能在推送后由 CI 复验；本机已按同一安装范围全部复现为绿。追溯矩阵 5.10 保持“进行中”。

## 5. 安装矩阵

- 核心依赖新增 `openai>=1.40`、`anthropic>=0.34`：默认 `llm_provider="minimax"` 走 OpenAI 协议，`claude` 走 Anthropic，二者在 `roles/llm.py` 中延迟 import；不声明则干净安装后 `analyze` 会 traceback。
- `mcp` extra 改为 `mcp>=2.0`，与代码使用的 `from mcp.server.mcpserver import MCPServer` 一致。
- 移除 `[backtest]` extra：`src/` 内没有 pandas/numpy import，回测是纯 Python。
- `starlette`/`uvicorn` 保留在 `[webui]`；`[dev]` 引入 `[webui,mcp]`。
- 移除 `newsieve`（PyPI 404）：provider 文件删除，`config/data_source.toml` 与 PRD 同步，新闻类三个域由 `tickhub-cn` 承担。决策记录见 `docs/decisions/DEC-009-newsieve-removal.md`。
- 干净 venv 验证：`pip install .` → `ssenate --version` 输出 `stocksenate 0.1.0`；离线 `analyze` 退出 0；缺 SDK 时（minimax 未安装）以退出码 3 给出清晰提示，而不是 traceback。

## 变更文件（本单相关）

- 部署：`Dockerfile`、`docker-compose.yml`、`docker/entrypoint.sh`
- 引擎：`src/stock_senate/workflow/digest.py`、`src/stock_senate/workflow/offline.py`
- CLI：`src/stock_senate/cli.py`
- 依赖/打包：`pyproject.toml`、`.github/workflows/ci.yml`
- 数据：`src/stock_senate/data/providers/__init__.py`、删除 `newsieve.py`、`config/data_source.toml`
- 文档：`docs/specs/{PRD,cli-reference,priority,requirements-traceability}.md`、`docs/decisions/DEC-009-newsieve-removal.md`
- 测试：新增 `tests/test_digest_quality.py`；`tests/test_cli.py` 增加环境变量优先级回归；`tests/test_cli_exit_codes.py` 注释更新
- 证据：`docs/tasks/TASK-QUOT-56/evidence/*.json`
- 其余大量 `src/`、`tests/` 改动为 ruff/mypy 清零所需的机械修复，不改变对外契约。

## 未完成 / 风险

- 真实 `docker compose up` 未执行（本机无 Docker），需在有 Docker 的环境做一次双容器 healthy 复核。
- CI 的真实 7 job 结果需推送后确认；本机复现为绿。
- `uv.lock` 仍为待实现（追溯矩阵 5.11），不在本单范围。
- 回测分板块涨跌停与沪深 300 基准属 P1，不在本单。
