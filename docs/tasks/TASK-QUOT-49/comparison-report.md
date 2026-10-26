# StockSenate 逐条对比验收报告（QUOT-51）

- 任务：QUOT-51「StockSenate 逐条对比验收与测试补齐」（父任务 QUOT-49）
- 执行：测试工程师，2026-10-26
- 基准：v1.1 `councli.docx`（19 章）、v1.2 `councli文档.docx`（20 章）
- 验收对象：`docs/specs/requirements-traceability.md`、`docs/tasks/TASK-QUOT-49/README.md`、`docs/specs/cli-reference.md`、`docs/specs/PRD.md`、`docs/decisions/DEC-001…DEC-008`、Stage-1 前端验证记录与 `src/stock_senate/`
- 边界：本次只新增测试与报告，未修改任何业务代码（`git status` 中无已跟踪文件被改动），未执行 `git push`

## 1. 结论摘要

| 维度 | 结果 |
|---|---|
| 追溯矩阵状态行 | 125 条：**通过 107**（已实现 105 + 已核验 2）／**不通过 16**（待实现 12 + 骨架 4）／进行中 1／待核验 1 |
| 文档与实现矛盾 | **11 条**（见 §6），其中 2 条影响上线判断（命名前提、WebUI 缺 3 页） |
| 阻断级缺陷 | **1 个**：QUOT-51-09，`docker compose` 中 webui 健康检查路径错误，导致 mcp 永不启动 |
| 行为缺陷（以 xfail 固定） | **5 个**：QUOT-51-09、07、01、02、06 |
| CLI 退出码 | 0/2/3/4/8 已验证可达；**7 声明但不可达**；2、3 各有 1 条路径返回 1 |
| MCP stdio 纯净性 | 通过：13 条测试确认 stdout 只含 JSON-RPC 行 |
| 角色契约 | 通过：16 键、4 别名、12 席位、第 13 个分析师与第 6 个自定义席位均被拒 |
| 端口 | 通过：8788/8765 与 8787/8000/8790/8791 不冲突，compose 仅绑回环 |
| 测试总量 | **185 例 / 0 failed / 0 errors / 5 xfailed / 18.061 s**；本次新增 86 例（81 断言通过，5 例固定已知缺陷） |

发布判断：**不建议直接发版**。功能主干与契约大体成立，但 compose 部署链路当前不可用，且退出码契约存在 4 处偏差；详见 §7。

## 2. 方法与证据

1. 从两份 docx 抽取纯文本（v1.1 484 行、v1.2 586 行），按章节逐条比对；本报告引用的原文均标注该抽取文本的行号。
2. 逐行读取追溯矩阵，按最后一列状态严格计数（仅匹配列首状态词，避免把备注里的"待补/未实现"算入）。
3. 对每条可疑项做代码定位或行为探测（`DataAccessLayer` 模式探测、`RoleRegistry.from_config` 探针、`docker-compose.yml` 健康检查与 WebUI 路由对照）。
4. 用黑盒方式补测试：`pytest` 子进程 + JUnit XML 计数，`CliRunner` 进程内测退出码，`TestClient` 测 WebUI 路由。
5. 最终全量执行：

```bash
cd /d/workplace/StockSenate
PYTHONPATH=src PYTHONIOENCODING=utf-8 /d/workplace/ruanjian/venv/Scripts/python.exe \
  -m pytest -q --junitxml=.tmp_final_suite.xml -p no:cacheprovider
# tests=185 failures=0 errors=0 skipped=5 time=18.061s
```

## 3. 追溯矩阵逐行核对

状态分布（125 条状态行）：

| 状态 | 数量 | 含义 |
|---|---:|---|
| 已实现 | 105 | 代码存在，且有测试或可执行行为佐证 |
| 已核验 | 2 | CLI 名、端口占用（本机 `netstat` 复核） |
| 待实现 | 12 | 模块/工作流缺失 |
| 骨架 | 4 | 部分能力存在，关键子能力缺失 |
| 进行中 | 1 | 18.2「18 项新增能力逐项验收」 |
| 待核验 | 1 | PyPI 名称 `stocksenate` 占用情况 |

### 3.1 不通过项（16 条）

| 矩阵行 | 需求 | 现状与证据 |
|---|---|---|
| 1.1 | CLI / WebUI / API / 报告 / checkpoint 完整保留 | 骨架：checkpoint 恢复缺失，`workflow/` 无 checkpoint 模块 |
| 1.2 | 旧包名/Python 包 shim 过渡 | 待实现：无 shim，仅在安装时提示（DEC-001 已记录取舍） |
| 2.1 | Claude Agent SDK 订阅模式 | 待实现：`roles/resolver.py` 无 `claude_agent_sdk` provider |
| 4 | LangGraph checkpoint 可恢复 | 待实现：无 `workflow/checkpoint.py` |
| 4 | 旧 `data_vendors` 键映射 | 待实现：`config/data_source.toml` 无该键及告警逻辑 |
| 5.7 | 30~50 次调用 → 增量分析 | 待实现：无 `workflow/incremental.py` |
| 5.11 | 依赖锁定 | 待实现：仓库无 `uv.lock`，CI 未用 `uv sync --frozen` |
| 8.3 | `workflow.toml` 任意节点/边可配 | 骨架：仅轮次/阈值/HITL 可配 |
| 9 | 并行/串行/分支/汇聚/任意拓扑 | 骨架：同上，拓扑固定 |
| 9.1 | 短期图状态 + 长期 PGVector 反思 | 待实现：无 memory 模块 |
| 9.5 | Map-Reduce 上下文压缩 | 待实现：无 `workflow/compress.py` |
| 9.6 | OTel / Langfuse 追踪 | 待实现：`trace_id` 已贯穿，未接 OTel/Langfuse |
| 14 | 增量分析（重复标的只拉新增） | 待实现：与 5.7 重复登记，同一能力 |
| 17 | `python -m build` + `twine upload` | 待实现：`.github/workflows/` 只有 `ci.yml`，无 `release.yml` |
| 18.3 | pip / Docker / compose 部署 | 骨架：文件就绪，未构建验证；且存在 QUOT-51-09 |
| 19 | 链接/许可证逐项核验 | 待实现：`docs/specs/references.md` 不存在 |

补充核验：
- `uv.lock`、`docs/specs/references.md`、`workflow/checkpoint.py`、`workflow/memory.py`、`workflow/compress.py`、`observability/tracing.py`、`.github/workflows/release.yml` 均确认不存在。
- 20 行「PyPI 名称未被占用」仍未核验，属发布前人工项。

## 4. v1.1 → v1.2 差异对齐

| 项 | v1.1 | v1.2 | 处理情况 |
|---|---|---|---|
| 章节 | 19 章 | 20 章 | v1.2 新增第十六章 WebUI，后续章号顺延（16→17、17→18、18→19、19→20） |
| 工作流 | 第九章无子节 | 新增 9.1 记忆、9.2 Guardrails、9.3 HITL、9.4 LLM-as-a-Judge、9.5 上下文压缩、9.6 全链路追踪 | 9.3 已实现；9.2 已实现（注入样本测试本次未补，属安全小队范围）；9.4 规则式质量门；9.1/9.5/9.6 未实现 |
| WebUI | 无独立设计 | 第十六章：12 页清单 + 环境变量 + 安全审计 | 实现 10 页，缺 3 页（见 §5.3） |
| 能力计数 | 「新增 17 项」（v1.1 2 处） | 首章「新增 17 项」（v12:19），验收章「18 项新增逐项验收」（v12:514） | 文档自相矛盾，矩阵按 18 项登记 |
| 命名 | 全篇 `qfc`/`qf_council`/`quota-fuse-council` | 同左，且 §1.2 明确保留旧名 | **全文 0 次 `StockSenate`、0 次 `ssenate`**（见 §6 D-01） |

## 5. 缺失项清单

### 5.1 矩阵已登记的缺失（16 条）

见 §3.1，不再重复。

### 5.2 矩阵未登记、但对照 v1.2 原文缺失的能力

| 来源 | 缺失内容 | 证据 |
|---|---|---|
| v1.2 §16.3 | WebUI「辩论过程」页：7 角色观点卡片、多空轮次、收敛分数、终止原因 | `webui/pages/` 无对应路由；grep 辩论/收敛/bull/bear 仅命中 `analysis.py:33` 的表单占位 |
| v1.2 §16.3 | WebUI「工作流拓扑」页：节点与边可视化、interrupt 点 | 无对应路由与模板 |
| v1.2 §16.3 | WebUI「配置校验」页：Schema 校验、迁移预览、回滚 | `settings.py` 仅内联校验函数，无独立页面；路由只有 `/settings`、`/settings/roles` |
| v1.2 §18.3 / §5.9 | 日志多 sink：file/sqlite/mysql/postgres；配置变更审计与回滚 | `observability/logging.py:149` 唯一 handler 为 `StreamHandler(sys.stderr)`，无文件/数据库 sink |
| v1.2 §7.8 | 「mode=local 断网可完成分析」 | `local` 模式下 12 个域中有 6 个无本地候选（见 §5.4） |

### 5.3 WebUI 页面覆盖（v1.2 §16.3 的 12 页）

| v1.2 页面 | 实现位置 | 结论 |
|---|---|---|
| 分析工作台 | `analysis.py`（`/`、`/analysis`）+ `progress.py` | 有 |
| 辩论过程 | — | **缺** |
| 最终报告 | `reports.py` | 有 |
| 历史记录 | `history.py` | 有 |
| 报告 Diff | `diff.py` | 有 |
| 回测中心 | `backtest.py` | 有 |
| 告警面板 | `alerts.py` | 有 |
| 角色管理 | `settings.py`（启停/权重） | 有（仅角色，工作流/模板只读） |
| 工作流拓扑 | — | **缺** |
| 批量队列 | `queue.py` | 有 |
| 配置校验 | — | **缺**（`/settings` 仅有只读配置展示） |
| 数据状态 | `ops.py` | 有 |

矩阵 §16.3 把清单写成「分析/进度/报告/Diff/回测/告警/队列/设置/历史」九页，并标注「十页可达」；与 v1.2 的 12 页清单不一致，且回避了 3 个缺失页面。

### 5.4 `local` 模式的域覆盖探测

```text
announcement  -> ['local_json']
financial     -> ['local_json', 'sql']
kline         -> ['local_json', 'sql']
policy_feed   -> ['text_docs']
research      -> ['text_docs', 'local_json']
risk_event    -> ['local_json']
quote / news / social_buzz / money_flow / dragon_tiger / lockup_calendar -> []
```

即 `mode=local` 下行情、新闻、情绪、资金流、龙虎榜、解禁 6 个域没有本地 provider，只能返回 `missing` 信封。v1.2 §7.8「断网可完成分析」只在受限域集合内成立，矩阵 §7.8 标注「已实现」缺少这一限制说明。

## 6. 文档与实现矛盾（11 条）

| ID | 文档表述 | 实现/实际行为 | 影响 |
|---|---|---|---|
| D-01 | DEC-001:9「开发文档 v1.2 改为 StockSenate」 | v1.1/v1.2 全文各 0 次 `StockSenate`、0 次 `ssenate`；v1.2 §1.2 仍规定包名 `quota-fuse-council`、包 `qf_council`、CLI `qfc` | 决策前提与原始需求不符；命名变更属团队自定，需在 DEC-001 补记「由团队决定」而非「文档已改」 |
| D-02 | v1.2 §7.4（v12:220-235）`[data_source]` / `[data_source.sql]` / `[data_source.priority]` | `config/data_source.toml` 顶层为 `[global]`/`[providers]`/`[routing]`/`[freshness]`，无 `sql`、`priority` 子表 | 按文档写的配置无法被加载器识别 |
| D-03 | v1.2 §8.2（v12:279-302）角色字段 `name/enable/weight/prompt_file/model_tier/stage/participate_in_debate/max_retries`，且要求「必须定义六项」 | 代码字段为 `display_name/kind/alias/prompt/tools/output_schema/enabled`；`roles/registry.py:100-108` 只强制 `kind` 与 `output_schema` | 探针：文档式角色被拒（`roles.basic.kind 必填`）；仅两字段的极简角色被接受（`prompt=''`、`tools=()`、权重 1.0） |
| D-04 | 矩阵 §7.3「`DataEnvelope` 九字段」 | 同句列出 10 个字段；`data/envelope.py:51-60` 实为 10 字段 | 文档笔误，需改为「十字段」 |
| D-05 | v1.2 §16.3 十二页清单 | 实现 10 页，缺辩论过程/工作流拓扑/配置校验；矩阵把清单缩为九页并称「十页可达」 | 验收口径被改写，缺页被掩盖 |
| D-06 | 矩阵 §5.9「多 sink 已实现」；v1.2 §18.3「日志支持 file/sqlite/mysql/postgres」 | `observability/logging.py:149` 仅有 stderr handler | 矩阵状态应降级；审计/回滚同样缺失 |
| D-07 | 矩阵 §9「WebUI 可编辑角色/工作流/模板，写操作审计」已实现 | `settings.py` 写操作仅 `POST /settings/roles`（启停）；工作流/模板为只读展示 | 矩阵夸大；审计仅覆盖角色启停 |
| D-08 | v1.2 §7.8「mode=local 断网可完成分析」 | 6/12 域无本地候选（§5.4） | 离线能力范围需在 PRD/矩阵中限定 |
| D-09 | DEC-001:37 环境变量优先级 `STOCKSENATE_* > TRADINGAGENTS_* > QF_COUNCIL_*` | `cli.py:83` 先读 `QF_COUNCIL_CONFIG_DIR`，再读 `TRADINGAGENTS_CONFIG_DIR` | 旧前缀同时设置时，决策与实现相反 |
| D-10 | v1.2 首章「新增 17 项」（v12:19） vs 验收章「18 项」（v12:514） | 矩阵按 18 项登记，18.2 仍「进行中」 | 验收对象数量需定稿 |
| D-11 | 矩阵 §9.4「LLM-as-a-Judge 已实现」 | 同一格备注「评分为规则式，非 LLM Judge」；无 `workflow/judge.py` | 状态与备注自相矛盾，应拆为「规则质量门已实现 / LLM Judge 待实现」 |

另：`ssenate backtest run` 无 `--config`，而 `analyze` 有（`cli.py:166` vs `cli.py:301`），只能靠环境变量指定配置目录。这不是文档矛盾，但属 CLI 契约一致性问题，建议补齐。

## 7. 行为缺陷与阻断项

### 7.1 阻断项

**QUOT-51-09（阻断，compose 部署不可用）**

- 现象：`docker-compose.yml:32` 健康检查请求 `http://127.0.0.1:8788/_stcore/health`，这是 Streamlit 的路径；WebUI 已按 DEC-008 改为 Starlette，只提供 `/healthz`（`webui/auth.py:282,293`）。
- 后果：webui 永远不健康 → `mcp` 的 `depends_on: condition: service_healthy`（`docker-compose.yml:42-44`）永不满足 → 双服务部署失败。
- 测试：`tests/test_ports_contract.py::test_compose_healthcheck_hits_a_route_the_app_serves` 以 xfail 固定；实测请求 `/healthz` 返回 200，`/_stcore/health` 返回 404。
- 修复建议（开发侧）：把 compose 第 32 行改为 `http://127.0.0.1:8788/healthz`，并保留 `curl -fsS`。

### 7.2 退出码契约缺陷

| 缺陷 | 场景 | 文档要求 | 实际 | 证据 |
|---|---|---|---|---|
| QUOT-51-07 | 分析触发限流 | `7`（cli-reference:8） | `1`（`LLMRateLimited` 逃逸出 Click） | `test_rate_limited_analyze_exits_7` xfail |
| QUOT-51-01 | `analyze --date 2026-13-99` | `2`（参数错误） | `1`（未捕获 `ValueError`） | `test_malformed_date_is_a_usage_error` xfail |
| QUOT-51-02 | `analyze --disable ghost` | `3`（配置错误） | `1`（`RoleError` 逃逸） | `test_unknown_disable_role_is_a_config_error` xfail |

已验证可达：`0`（version/help）、`2`（缺参、非法选项、窗口为 0、区间倒置等 5 条）、`3`（未知角色、`forbid_llm_fill=false`）、`4`（空归档 analyze/backtest）、`8`（回测部分标的缺失）。根因集中在 `cli.py:166-192`（analyze 无异常映射）与 `cli.py:301-338`（backtest 只捕获 `ValueError`）。

### 7.3 角色 CLI 契约缺陷

**QUOT-51-06（P2）**：`cli-reference:74` 承诺「别名可用并归一」，但 `role show technical` 直接按原始 TOML 键查找并退出 3；`role disable technical` 则通过（归一化只在部分命令生效）。测试 `tests/test_role_contract.py::test_cli_role_show_accepts_an_alias` 以 xfail 固定。

## 8. CLI 契约核对结果

| 检查项 | 结果 |
|---|---|
| `analyze` 参数/默认值 | 与 cli-reference §2.1 一致（`--date/--config/--output/--enable/--disable/--weight/--no-hitl`） |
| `backtest run` 参数 | 与 §2.3 一致，但无 `--config` |
| `role list\|show\|validate\|enable\|disable` | 命令齐全；`show` 别名缺陷见 QUOT-51-06 |
| `mcp http` / `webui` 默认地址端口 | 127.0.0.1:8765 / 127.0.0.1:8788，与 DEC-003 一致 |
| stdout/stderr 分流 | 用法错误只写 stderr，stdout 为空；`--output json` 成功时 stdout 为单个可解析 JSON 文档（有测试） |
| 报告不含可执行交易字段 | 通过（`test_analyze_never_emits_executable_trade_fields`） |
| MCP stdio 纯净性 | 通过：`test_stdio_stdout_is_only_jsonrpc` 逐行校验 |

## 9. 角色契约核对结果

| 检查项 | 结果 |
|---|---|
| `role_llms` 16 键 | 通过（`config/agent_roles.toml` 16 个 `[roles.*]`，7 个分析师启用，5 个空闲自定义席位） |
| 4 个别名 | `technical→market`、`basic→fundamentals`、`sentiment→social`、`unlock→lockup` 均可解析；别名不额外占席位；别名冲突被拒 |
| 12 个分析师席位 | 12 个接受；第 13 个拒绝 |
| 第 6 个自定义席位 | 拒绝（即使全局上限未满） |
| 其他 | 临时启停不改源文件；按别名设权重生效；`deny_write_tools=false` 被拒 |

## 10. 端口核对结果

- 本机 `netstat` 未见 8787 / 8000 / 8790 / 8791 / 8788 / 8765 被占用，与 DEC-003 端口表不冲突。
- `docker-compose.yml` 仅发布 `127.0.0.1:8788:8788`、`127.0.0.1:8765:8765`，未出现外部端口映射或 `0.0.0.0` 绑定。
- 健康检查路径不一致问题单列为 QUOT-51-09，不重复计入端口冲突。

## 11. 本次新增测试

新增 86 例（5 个文件 + 1 个辅助模块），全部为新增文件，未改动既有用例：

| 文件 | 用例数 | 覆盖 |
|---|---:|---|
| `tests/test_integration_data_layer.py` | 22 | 四模式选择顺序、回退留痕、`allow_fallback=false`、全源失败不抛异常、`forbid_llm_fill`、未来数据拒绝、local_json 归档选择、降级汇总 |
| `tests/test_e2e_cli.py` | 12 | `analyze` 全流程（离线 provider + 真实文件 + 报告落盘）、无交易字段、JSON/CSV 输出、空归档退出 4、降级仍成功；`backtest run` 产物、成本影响、空归档 4、部分缺失 8、可复现 |
| `tests/test_cli_exit_codes.py` | 19 | 退出码 0/2/3/4/7/8 全覆盖 + stdout/stderr 分流 + JSON 可解析 |
| `tests/test_role_contract.py` | 27 | 16 键、别名、12 席位、第 13 席、第 6 自定义席、权重、写工具拒绝、CLI 覆盖 |
| `tests/test_ports_contract.py` | 6 | 默认端口、与外部端口不相交、DEC-003 完整表、compose 仅回环、健康检查路由一致 |
| `tests/cli_support.py` | — | 子进程 CLI 辅助（UTF-8、离线 provider、临时 HOME） |

运行结果（含既有 99 例）：

```text
tests=185 failures=0 errors=0 skipped=5 time=18.061s
```

- 新增 86 例中：81 例断言通过，5 例 `xfail(strict=False)` 固定上文 5 个已知缺陷（修复后会自动转为 XPASS，不会被误报为通过）。
- 分模块：alerts 31、backtest 9、cli 7、cli_exit_codes 19（3 xfail）、config_schema 9、contracts 4、e2e_cli 12、integration_data_layer 22、mcp 13、ports_contract 6（1 xfail）、queue 8、role_contract 27（1 xfail）、screening 10、webui 7、webui_parity 1。

复现命令：

```bash
cd /d/workplace/StockSenate
export PYTHONPATH=$PWD/src PYTHONIOENCODING=utf-8
PY=/d/workplace/ruanjian/venv/Scripts/python.exe
$PY -m pytest -q
$PY -m pytest -q tests/test_integration_data_layer.py tests/test_e2e_cli.py \
  tests/test_cli_exit_codes.py tests/test_role_contract.py tests/test_ports_contract.py
```

## 12. 建议与后续

按优先级：

1. **P0 修复 QUOT-51-09**：compose 健康检查改为 `/healthz`；否则 Docker 部署验收无法通过。
2. **P0 统一退出码映射**：在 `analyze` / `backtest run` 外层把 `LLMRateLimited→7`、参数解析错误→`2`、`RoleError`/配置错误→`3` 映射为 `typer.Exit`，与 cli-reference §1 对齐。
3. **P1 修正矩阵与文档**：D-01、D-04、D-05、D-06、D-07、D-11 六处状态/描述需就地更正；D-02、D-03 需二选一——改配置 schema 或改文档，不能长期并存。
4. **P1 补 WebUI 缺页或缩范围**：辩论过程页是原版核心体验，建议优先；拓扑/配置校验若本期不做，应在 PRD 与矩阵中显式降级并标注里程碑。
5. **P1 日志 sink 与审计**：至少落地 `file` sink 与配置变更审计，满足 v1.2 §16.6/§18.3 的「写操作留痕」。
6. **P2 补 `--config` 一致性**：`backtest run` 增加 `--config`，避免只能依赖环境变量。
7. **P2 修正 `role show` 别名**：与 `disable/enable` 共用归一化入口。
8. **发布前人工项**：`pip index versions stocksenate` 核验名称；`docs/specs/references.md` 补全并做链接检查。

## 13. 附件与产出

- 本报告：`docs/tasks/TASK-QUOT-49/comparison-report.md`
- 新增测试：`tests/test_integration_data_layer.py`、`tests/test_e2e_cli.py`、`tests/test_cli_exit_codes.py`、`tests/test_role_contract.py`、`tests/test_ports_contract.py`、`tests/cli_support.py`
- 未修改任何业务代码；未提交、未推送。
