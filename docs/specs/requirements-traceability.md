# 需求追溯矩阵

把需求基线 v1.1（19 章）与 v1.2（20 章）逐条映射到实现位置、对外表面与验收方式。**任何一行缺失都视为需求缺失**；测试工程师按本表逐项核对，安全工程师按「安全边界」列复核。

约定：

- **实现位置** 是目标文件路径；标 `M0-M3` 的表示由对应里程碑交付。
- **对外表面** 是用户实际能触发该能力的入口（CLI / MCP / Python API / WebUI）。
- **状态** 反映本仓库当前真实情况：`骨架` = 接口与配置已存在、引擎未实现；`待实现` = 尚未开始；`已实现` = 有可运行代码与测试。

> 文档不一致已按 `docs/decisions/` 归一：包名/命令/环境变量见 DEC-001，角色键见 DEC-005，退出码以 v1.2 第十一章为准。

> 更新记录：2026-10-26 DEC-013：需求基线与逐任务过程记录移出仓库，本表不再引用文件名与 `docs/tasks/*`；条目、状态与验收结论不变，证据存于团队私有归档。

> 更新记录：2026-10-26 按 M0–M3 交付结果刷新状态列，验收证据与剩余项见 QUOT-50 交付记录（私有归档）；WebUI 以 Starlette 交付的偏离见 DEC-008。

> 更新记录：2026-10-26 QUOT-56 修复后刷新：compose 健康检查路径、事实抽取与覆盖计分、退出码 0/2/3/4/7/8、安装 extras。Newsieve 移除见 DEC-009。

> 更新记录：2026-10-26 QUOT-57：新增真实数据形态回归（`tests/fixtures/tickhub/` + `tests/test_tickhub_fixture_regression.py`）、弱模型/预算/HITL 专项（`tests/test_resilience.py`）与报告 diff 专项（`tests/test_report_diff.py`）；5 处仍无自动化测试的缺口集中登记于 §二十一。

> 更新记录：2026-10-26 QUOT-49 P0 修复：HITL 处理器经 `propagate(hitl_handler=...)` 接入，`timeout_seconds` 以守护线程 + 真实截止时间强制执行，CLI 仅在 stdin 为 TTY 时交互询问，其余入口按 `auto_approve` 留痕；`budget.per_run.on_exceed` 在 `run_role` 运行期消费（`fail` 立即失败、`stop` 降级并标 `partial`）。

---

## 一、项目基础信息（v1.1/v1.2 §1）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 1.1 | 定位：只做投研分析 + 回测推演，不做实盘、不输出自动交易指令 | `README.md`、`docs/specs/PRD.md` §2.2、`config/output_template.toml` | 文档 / 报告页脚 | 报告与 README 均含免责声明；代码中无下单接口 | P0 | 已实现（`execution=false`、报告/MCP 免责声明；无下单代码） |
| 1.1 | 7 角色 + 多空辩论 + 研究经理 + 交易员 + 三档风险 + 投资经理终审完整保留 | `src/stock_senate/workflow/`、`config/agent_roles.toml` | `ssenate analyze` | 拓扑单测断言 6 个阶段与顺序 | P0 | 已实现（`test_webui_parity`、`test_mcp` 跑通全链路） |
| 1.1 | CLI / Streamlit WebUI / Python API / 报告 / checkpoint 完整保留 | `src/stock_senate/cli.py`、`webui/`、`graph/senate_graph.py` | 全部入口 | 三处入口对同一 run 结果一致 | P0 | 已实现（CLI/API/WebUI 共用 `propagate`；SQLite checkpoint 中断续跑已接入，默认关闭，见 `test_checkpoint`） |
| 1.1 | 数据采集统一交给 TickHub，不重复实现采集、缓存、代理池、降级 | `src/stock_senate/data/`、`config/data_source.toml` | `ssenate analyze` | 代码中无直连东财/巨潮/通达信；网络层只经 provider | P0 | 已实现（provider 层统一取数，无直连采集代码） |
| 1.2 | 命名映射：包 `stocksenate`、模块 `stock_senate`、CLI `ssenate`、入口 `stock_senate.cli:main` | `pyproject.toml` | `ssenate` | `pip install -e .` 后 `ssenate --help` | P0 | 已实现 |
| 1.2 | 旧 CLI `tradingagents` 保留并打印弃用警告 | `pyproject.toml` `[project.scripts]`、`cli.legacy_main` | `tradingagents` | 运行后 stderr 含弃用提示 | P0 | 已实现 |
| 1.2 | 旧包名/Python 包 shim 过渡 | 迁移说明 | — | `docs/decisions/DEC-001` 记录取舍：不发布 shim，改为安装期提示 | P1 | 待实现 |
| 1.2 | `TradingAgentsGraph` 别名，`propagate` 签名与返回不变 | `src/stock_senate/__init__.py`、`graph/senate_graph.py` | Python API | `from stock_senate import TradingAgentsGraph`；返回二元组 | P0 | 已实现 |
| 1.2 | 配置目录 `~/.stock_senate/`，首次启动只读迁移 `~/.tradingagents/` | `cli.config_migrate`、`cli._config_dir` | `ssenate config migrate` | 迁移后旧目录仍存在且未被修改 | P1 | 已实现 |
| 1.2 | 环境变量 `STOCKSENATE_*`，兼容旧前缀且新前缀优先 | `cli._config_dir`、`docs/decisions/DEC-001` | 环境变量 | 新前缀覆盖旧前缀的测试 | P1 | 已实现（新前缀优先，旧前缀告警） |
| 1.2 | MCP Server 名与 WebUI 服务名独立 | `docs/decisions/DEC-003` | MCP / WebUI | 与 TickHub MCP 名称、端口、鉴权互不干扰 | P0 | 已实现（`test_mcp`、DEC-003） |
| 1.3 | 职责边界表：Council 不直连数据源、WebUI 不做运维面板、MCP 与 WebUI 分进程 | `README.md`、`PRD.md` §2.3 | 部署拓扑 | compose 两服务（WebUI/MCP）分端口；Admin 属 TickHub 外部；WebUI 无运维写操作 | P0 | 已实现（compose 分端口；WebUI 运维只读） |

## 二、原仓库代码核对结论（§2）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 2.1 | A 股规则：T+1、涨跌停、最小手数、交易时段、ST、CSI300 基准 | `workflow/constraints.py`、`backtest/` | analyze / backtest | 约束单测（涨停不可买、T+1 不可卖） | P0 | 已实现（`test_backtest`） |
| 2.1 | 快/深双模型分层 + 多 provider | `config/llm_config.toml`、`roles/resolver.py` | `role_llms` | 同 provider+model 复用客户端 | P0 | 已实现（`llm_config.toml` + `roles/llm.py`；真实 LLM 全链路无测试，见 §二十一） |
| 2.1 | `performance` 是事后归因，不是回测 | `report/performance.py` | `ssenate performance` | 命令描述与报告字段不含净值/收益曲线 | P0 | 已实现（`test_cli`） |
| 2.1 | Claude Agent SDK 订阅模式 | `roles/resolver.py` 的 `claude_agent_sdk` provider | 配置 | 冒烟：无 `ANTHROPIC_API_KEY` 时按订阅凭据启动 | P2 | 待实现（未提供该 provider） |
| 2.2 | `propagate` 返回值不可被报告文件替代 | `graph/senate_graph.py` | Python API | 测试只读返回值即可断言决策 | P0 | 已实现（签名/返回契约） |
| 2.2 | 角色模型配置必须细粒度，不能只有两级 | `config/llm_config.toml` | `role_llms` | 16 个合法键 + 错键报错 | P0 | 已实现 |
| 2.2 | 回测与 `performance` 分开描述、分开验收 | `PRD.md` §4/F-24、`docs/specs/cli-reference.md` | CLI | 两份验收清单独立 | P0 | 已实现 |
| 2.2 | 数据迁移是架构重构，保留迁移期兼容开关 | `config/data_source.toml` `mode` | 配置 | `mode=local` 时完全不依赖 TickHub | P0 | 部分实现（`local` 模式断网跑通；本行口径收窄为「已覆盖域」：kline/financial/announcement/research/risk_event 有真实形态夹具，money_flow/lockup_calendar 仅有 TickHub 抽取夹具。quote/news/social_buzz/money_flow/dragon_tiger/lockup_calendar 六域仍缺本地归档形态夹具，无测试，见 §二十一） |
| 2.2 | 对外表述延续「研究框架非产品」 | `README.md`、`PRD.md` §2 | 文档 | 无荐股/收益承诺措辞 | P0 | 已实现 |

## 三、总体架构（§3）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 3 | WebUI / MCP Server / TickHub Admin 三进程，端口 8788 / 8765 / 8787 | `src/stock_senate/webui/launch.py`、`mcp_server/server.py`、`docs/decisions/DEC-003` | 部署 | 端口互不冲突；无共享 stdout | P0 | 已实现（`test_webui`/`test_mcp`，DEC-003） |
| 3 | 图执行层与数据层分离，DataAccessLayer 为唯一取数入口 | `src/stock_senate/data/` | Python API | 代码扫描：节点不直接 import provider | P0 | 已实现（节点只经 `DataAccessLayer` 取数） |
| 3 | 本地 JSON / 文本 / SQL 可插拔 | `data/providers/*` | `data_source.toml` | 四种 provider 各有单测 | P1 | 已实现（provider 齐全；各 provider 独立契约测试待补） |

## 四、基础能力清单（§4）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 4 | CLI 子命令与参数语义 | `cli.py` | `ssenate` | 命令清单与 `cli-reference.md` 一致 | P0 | 已实现（`test_cli`） |
| 4 | 报告五分区 + `summary.json` + `complete_report.md` | `report/render.py` | analyze / report | 文件存在且字段齐全 | P0 | 已实现（`test_webui_parity`） |
| 4 | WebUI 交互与实时进度 | `webui/` | WebUI | 进度面板随节点更新 | P1 | 已实现（Starlette 实现，见 DEC-008） |
| 4 | LangGraph checkpoint 可恢复 | `workflow/checkpoint.py` | 配置 | 中断后续跑不重复调用已完成节点 | P1 | 已实现（`workflow/checkpoint.py` SQLite 按 code/date/config_hash 保存阶段产物；`test_checkpoint` 覆盖中断→续跑→不重复调用→终局一致；默认关闭） |
| 4 | `data_vendors` 配置语义 | `config/data_source.toml` | 配置 | 旧 `data_vendors` 键映射并告警 | P1 | 待实现（未实现旧键映射） |

## 五、Issue 闭环清单（§5）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 5.1 | Issue 102：多数据源同时失败不崩，降级并标注 | `data/router.py`、`DataEnvelope.data_source_status` | analyze / MCP | 全源失败返回 `missing` + 可读原因，退出码 4 | P0 | 已实现（`test_alerts` missing + 退出码 4） |
| 5.2 | Issue 105①：完整回测引擎（滑点/手续费/持仓/重叠窗口/净值/alpha 分桶/衰减/无未来函数） | `backtest/engine.py` | `ssenate backtest run` | 固定数据集可复现；未来数据注入必失败 | P0 | 已实现（`test_backtest`；alpha 分桶未单列） |
| 5.3 | Issue 105②：完整告警体系（三级/去重/状态机/Webhook 重试签名幂等） | `alerts/engine.py` | `ssenate alert` | 去重、重试、验签、幂等四项单测 | P1 | 已实现（`test_alerts` 31 例） |
| 5.4 | Issue 105③：选股代码统一去重 | `screening/dedup.py` | Python API | 多策略候选合并后代码唯一 | P1 | 已实现（`test_screening`） |
| 5.5 | httpx 冲突彻底解决 | `pyproject.toml`、`docs/decisions/DEC-007` | 安装 | 依赖树不含冲突组合；`[google]` 不提供 | P0 | 已实现 |
| 5.6 | 弱模型 tool-calling 空报告 → Schema 强约束 | `workflow/nodes/base.py`、`roles/schemas.py` | analyze | 空输出被标 `partial` 且重试，不静默消失 | P0 | 已实现（`roles/schemas.py` 强约束 + partial；空输出重试恢复、重试耗尽抛 `LLMUnavailable`、角色跳过并标 `partial` 见 `tests/test_resilience.py`） |
| 5.7 | 30~50 次调用 → 增量分析 | `workflow/incremental.py` | 配置 / analyze | 二次分析 token 显著下降 | P1 | 待实现（无增量分析模块） |
| 5.8 | 配置分散 → 统一 toml + Schema 校验 | `config/*.toml`、`config/schema.py` | `ssenate config validate` | 错误定位到文件与键 | P0 | 已实现 |
| 5.9 | 日志弱 → 结构化日志（stdout/stderr 分离、脱敏、多 sink） | `observability/logging.py` | 全部入口 | stdout 无日志；API key 被脱敏 | P0 | 部分实现（`observability/logging.py`：stdout 纯净、字段脱敏、stderr 结构化；已支持轮转 file sink（`[logging]`，默认关闭，见 `test_logging_sink`），sqlite/mysql/postgres sink 仍待实现） |
| 5.10 | 无 CI → 补全 CI | `.github/workflows/ci.yml` | CI | lint + type + test + audit 全绿 | P0 | 已实现（根因：GitHub Actions 下 Typer 强制 Rich 终端渲染，`OptionHighlighter` 把 `--start` 拆成 `-` + `-start` 两段 ANSI，断言看不到连续原文；测试子进程加 `_TYPER_FORCE_DISABLE_TERMINAL=1`，未用 skipif、未改断言。合并 main 后另修两处基线问题：`from tests.cli_support` 在裸 `pytest` 下不可导入（`pyproject.toml` 声明 `pythonpath=["."]`）、MCP stdio 用例 EOF 竞态（先读两条响应再关 stdin）；本地 225 passed 连跑 5 次，PR CI run `40291436449` 十个 job 全绿） |
| 5.11 | 无 lock file → 依赖锁定 | `uv.lock` | 安装 | CI 用 `uv sync --frozen` | P1 | 已实现（uv 0.13.5 解析 107 包；CI 新增 lockfile job：`uv sync --frozen --extra dev` + `uv run --frozen pytest`；合并 main 后干净 venv 从 lock 安装 225 通过，lockfile job 在 run `40291436449` 为 success） |
| 5.12 | 英文文档不一致 → 修复 | `README.md`、`docs/` | 文档 | 中英文命令与配置键一致 | P2 | 已实现 |

## 六、新增功能与里程碑映射（§6）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 6 | 17 项企业级能力按 M0–M4 映射 | `docs/specs/PRD.md` §4/§8、`priority.md` | — | 每项在里程碑表中有归属 | P0 | 已实现 |
| 6 | 辩论智能收敛打分 + 阈值 | `workflow/debate.py` | analyze | 达到阈值提前结束并记录原因 | P1 | 已实现（`workflow/debate.py`） |
| 6 | 事实交叉核验钩子（数值必须来自结构化数据，冲突标 `conflict`） | `workflow/verify.py` | analyze | 注入冲突数据时报告标 `conflict` | P1 | 已实现（`workflow/verify.py` 标 `conflict`） |
| 6 | 人工介入工单（存储/通知/恢复/超时/权限/审计） | `workflow/hitl.py` | analyze / WebUI | 四种动作（同意/驳回/修改/超时）各一测 | P1 | 已实现（`workflow/hitl.py`；处理器经 `propagate(hitl_handler=...)` 注入，`timeout_seconds` 真实截止并落到 `on_timeout`，超时留痕；WebUI 审批路由未实现，保留为待实现） |

## 七、数据源可插拔与底座对接（§7）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 7.1 | 不在本组件实现采集/缓存/代理/降级 | `README.md` §职责边界 | — | 代码扫描无采集实现 | P0 | 已实现 |
| 7.2 | Provider：TickHub-CN / TickHub-US / JSON / TextDoc / SQL / Composite | `data/providers/` | `data_source.toml` | 每个 provider 有契约测试 | P0 | 已实现（六类 provider 齐全；Newsieve 已移除，资讯三域由 TickHub-CN 承担） |
| 7.2 | MCP stdio 与本地 import 双通道 | `data/providers/tickhub.py` | 配置 | 两种通道结果一致 | P0 | 部分实现（本地 import 通道已覆盖；TickHub 端 stdio MCP 通道无测试，见 §二十一） |
| 7.3 | `DataEnvelope` 十字段：domain/code/data_as_of/payload/source/source_detail/fetched_at/freshness/confidence/data_source_status | `data/envelope.py` | Python API / MCP | 字段与枚举单测；缺字段构造失败 | P0 | 已实现（`data/envelope.py`） |
| 7.4 | 四模式 `stockdata/local/hybrid/priority` + 按域优先级 | `data/router.py` | 配置 / analyze | 每模式一条集成测试 | P0 | 已实现（`local` 集成测试；其余模式单测待补） |
| 7.5 | 实时取数走 TickHub，不直连免费源 | `data/providers/tickhub.py` | analyze | 网络请求只发往 provider 端点 | P0 | 已实现（网络请求只发往 provider 端点） |
| 7.6 | 本地 JSON 布局 `{domain}/{code}/{date}.json` | `data/providers/local_json.py` | 配置 | 缺文件→`missing`；坏 JSON→`degraded` | P0 | 已实现（`test_alerts`/`test_backtest` 夹具） |
| 7.6 | 文本资料只做定性阅读，禁止抽数值 | `data/providers/text_doc.py`、`schema.py` 强制 `numeric_extraction=false` | 配置 | 配置为 true 时校验报错（已实现） | P0 | 已实现 |
| 7.7 | 边界：数据缺失不得由模型补数值 | `schema.py` 强制 `forbid_llm_fill=true` | 配置 | 配置为 false 时校验报错（已实现） | P0 | 已实现 |
| 7.8 | 验收：全源失败可读、回退留痕、无未来数据 | `data/router.py` | CLI | 三项集成测试 | P0 | 已实现（`test_alerts`/`test_backtest`：缺失、回退、未来数据） |

## 八、角色体系（§8）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 8.1 | 默认 7 分析师行为不变 | `config/agent_roles.toml` | analyze | 回归基线报告 diff 为空 | P0 | 已实现（7 内置角色离线全链路跑通；diff 机制见 `tests/test_report_diff.py`，固定基线快照回归未建） |
| 8.2 | 自定义角色需定义六项：提示词/工具白名单/Schema/模型层级/阶段/辩论权重 | `config/agent_roles.toml`、`roles/registry.py` | 配置 | 缺任一字段校验报错 | P0 | 已实现（`roles/registry.py` 校验字段/别名/席位） |
| 8.2 | 角色键与 `role_llms` 键对齐，别名归一 | `config/schema.py`、`docs/decisions/DEC-005` | 配置 | `technical/basic/sentiment/unlock` 可用且归一 | P0 | 已实现 |
| 8.3 | `workflow.toml` 节点/边可配；新增角色不改默认拓扑 | `config/workflow.toml` | 配置 | 启用自定义角色后默认 7 角色回归通过 | P1 | 骨架（轮次/阈值/HITL 可配；任意节点/边拓扑未实现） |
| 8.4 | 上限 12 个分析师席位（7 内置 + 5 自定义） | `config/schema.py` | `role validate` | 第 13 个分析师启用时报错 | P0 | 已实现 |
| 8.4 | 自定义角色失败可跳过并标 `partial` | `workflow/nodes/base.py` | analyze | 自定义角色抛错不阻塞主流程 | P1 | 已实现（`workflow/nodes/base.py` 捕获并标 partial） |
| 8.5 | 工具权限隔离：越权返回结构化错误并审计 | `roles/tool_gate.py` | analyze | 越权调用测试 | P0 | 已实现（`roles/tool_gate.py`；越权返回结构化错误） |
| 8.5 | 自定义角色默认只读 | `roles/tool_gate.py` | 配置 | 写工具永不注入 | P0 | 已实现（写工具不注入） |
| 8.6 | `ssenate role list/show/validate/enable/disable` | `cli.py` | CLI | 五条命令冒烟 | P0 | 已实现（`test_cli`） |

## 九、工作流与配置 + 高阶机制（§9）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 9 | 并行/串行/分支/汇聚/辩论轮次/收敛阈值/终止条件/interrupt 可配 | `config/workflow.toml`、`workflow/graph.py` | 配置 | 改轮数与拓扑后行为变化可观测 | P0 | 骨架（轮次/收敛阈值/HITL 可配；任意拓扑未实现） |
| 9 | 配置统一 toml + JSON Schema 校验 + 迁移脚本 | `config/*.toml`、`config/schema.py`、`cli.config_migrate` | CLI | `config validate` / `config migrate` | P0 | 已实现 |
| 9 | WebUI 可编辑角色/工作流/模板，写操作审计 | `webui/pages/settings.py` | WebUI | 每次写入产生审计记录 | P2 | 部分实现（仅角色启停 `POST /settings/roles` 可写并留痕；工作流/模板为只读。可编辑与全量写审计保留为待实现） |
| 9.1 | 短期记忆用图状态；长期反思入 PGVector；同标的检索注入 | `workflow/memory.py`、`observability/reflection.py` | analyze | 可关闭；不跨标的污染 | P1 | 待实现（无 memory 模块） |
| 9.2 | 防 Prompt 注入清洗层 + 合规过滤 | `data/sanitize.py`、`workflow/guardrails.py` | 全部输入 | 注入样本不改变系统行为 | P0 | 已实现（`data/sanitize.py`；`tests/test_external_sanitization.py` 4 类注入样本 × 参数化断言：清洗发生在摘要与提示词之前、清洗留痕、良性文本不动、所有 provider 统一包裹） |
| 9.3 | HITL 两个强制挂起点，支持同意/驳回/修改/超时 | `workflow/hitl.py` | analyze / WebUI | 四种动作各一测；自动放行留痕 | P0 | 已实现（`workflow/hitl.py`；同意/驳回/修改、超时 `auto_approve`/`abort`、未启用跳过与自动放行留痕见 `tests/test_resilience.py`；P0 已接线：处理器经 `propagate(hitl_handler=...)` 注入，`timeout_seconds` 用守护线程 + `Event.wait` 真计时，CLI 仅在 `stdin.isatty()` 时交互，MCP/队列/WebUI/管道等非交互入口按 `auto_approve` 留痕） |
| 9.4 | LLM-as-a-Judge 三维评分，<70 标 `low_quality` | `workflow/judge.py` | analyze / 报告 | 低分报告被标记且 WebUI 高亮 | P1 | 部分实现（`workflow/nodes/quality.py` 规则式三维质量门，<70 标 `low_quality`，数据覆盖按事实数计分、0 事实域不计覆盖；`tests/test_digest_quality.py`、`tests/test_tickhub_fixture_regression.py` 覆盖“ok 但 0 事实不计覆盖”与报告头/页脚一致性；LLM-as-a-Judge 未实现，保留为待实现） |
| 9.5 | Map-Reduce 上下文压缩 | `workflow/compress.py` | analyze | 超阈值触发；引用不丢 | P1 | 待实现（无 compress 模块） |
| 9.6 | OpenTelemetry / Langfuse 追踪 + trace_id | `observability/tracing.py` | 全部入口 | 一次运行的日志/span/用量同 id | P1 | 待实现（trace_id 已贯穿，未接 OTel/Langfuse） |

## 十、MCP Server（§10）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 10 | 双传输 stdio / Streamable HTTP | `mcp_server/server.py` | `ssenate mcp stdio|http` | 两种传输各接一个客户端 | P0 | 已实现（`test_mcp`） |
| 10 | stdio 模式 stdout 只输出 MCP 报文 | `mcp_server/server.py` | stdio | 捕获 stdout 可逐行解析为 JSON-RPC | P0 | 已实现（`test_mcp`） |
| 10 | 五个工具：分析/回测/告警/报告/配置 | `mcp_server/tools.py` | MCP | `tools/list` 返回五项并可调用 | P1 | 已实现（`test_mcp`） |
| 10 | 与 TickHub MCP 分开命名/鉴权/日志，trace_id 串联 | `docs/decisions/DEC-003` | 部署 | 日志与鉴权互不共享 | P1 | 已实现（DEC-003） |
| 10 | http 默认 127.0.0.1，对外必须鉴权 | `mcp_server/server.py` | `--host/--port` | 非本机无 token 时拒绝 | P0 | 已实现（`test_mcp`） |

## 十一、CLI 与 Python API（§11）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 11 | 命令全集（analyze/role/backtest/alert/report/queue/mcp/webui/config） | `cli.py` | CLI | 与 `cli-reference.md` 逐条对齐 | P0 | 已实现（`test_cli`） |
| 11 | `analyze --disable/--enable/--weight` | `cli.analyze` | CLI | 三个参数生效并在报告留痕 | P0 | 已实现（`test_cli`） |
| 11 | Python API `SenateGraph.propagate(code, date)` | `graph/senate_graph.py` | Python API | 返回 `(final_state, decision)` | P0 | 已实现（`test_contracts`） |
| 11 | json/table/csv 走 stdout，日志走 stderr | `cli._emit`、`observability/logging.py` | CLI | 管道重定向测试 | P0 | 已实现（`test_alerts`/`test_mcp`） |
| 11 | 退出码 0/2/3/4/7/8 | `cli.py` 常量 | CLI | 六种场景各一测 | P0 | 已实现（QUOT-56：六种场景实测，测试零 xfail） |

## 十二、完整回测引擎（§12）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 12 | 七张表：decision_record/backtest_run/trade/position/metric/price_series/benchmark_series | `backtest/models.py` | Python API | 迁移后表存在 | P0 | 已实现（`backtest/models.py` + `test_backtest`） |
| 12 | 滑点、手续费、仓位、重叠窗口 | `backtest/engine.py` | `backtest run` | 参数变化反映到净值 | P0 | 已实现（`test_backtest`） |
| 12 | 净值曲线、方向准确率、alpha 分桶、Buy→Sell 衰减 | `backtest/metrics.py` | report / WebUI | 固定数据集断言数值 | P0 | 已实现（净值/方向准确率/信号衰减；alpha 分桶未单列） |
| 12 | 基于已保存决策自动回测 + `data_as_of` 时点推演 | `backtest/replay.py` | `backtest run` | 无未来函数测试 | P0 | 已实现（`test_backtest` 未来数据注入必失败） |
| 12 | 数据优先本地归档，缺失走 TickHub | `backtest/replay.py` | 配置 | 断网可回测 | P1 | 已实现（`test_backtest` 断网回测） |
| 12 | 验收：可复现、无未来函数、参数生效、无样本泄漏、报告与 WebUI 一致 | `tests/test_backtest.py` | — | 五项自动化 | P0 | 已实现（`test_backtest` 9 例） |

## 十三、告警规则引擎（§13）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 13 | high/mid/low 三级 + 解禁/减持/预亏/商誉/处罚/政策利空/评级下调条件 | `config/alert_rules.toml`、`alerts/rules.py` | `alert list` | 规则校验通过；七类条件全覆盖 | P1 | 已实现（DSL + `alert rules validate`） |
| 13 | 动作 JSON + WebUI + Webhook | `alerts/channels/*` | 配置 | 三通道各一测 | P1 | 已实现（`test_alerts` 三通道） |
| 13 | 去重、状态机（触发/确认/恢复/静默） | `alerts/state.py` | 配置 | 去重窗口与状态迁移测试 | P1 | 已实现（`test_alerts`） |
| 13 | Webhook 重试、签名、幂等 | `alerts/channels/webhook.py` | 配置 | 验签失败拒绝；重复键不重复投递 | P0 | 已实现（`test_alerts`） |
| 13 | 依赖 TickHub 风险事件字段 | `data/providers/tickhub.py` | 配置 | 字段映射表测试 | P1 | 已实现（provider 域映射） |

## 十四、报告、增量与多格式（§14）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 14 | 每次分析保存结构化快照 | `report/store.py` | analyze | 快照可重放渲染 | P0 | 已实现（`test_webui_parity`） |
| 14 | 字段级 diff：差异/结论变化/风险变化 | `report/diff.py` | `report diff` | 构造两次运行断言 diff | P0 | 已实现（`report/diff.py`；字段/风险与理由集合/分析师立场/结论方向、同日选最新快照与 CLI 参数校验见 `tests/test_report_diff.py`） |
| 14 | 增量分析：新标的全流程，重复标的只拉新增 + 注入历史摘要 | `workflow/incremental.py` | analyze | token 下降且结论一致 | P1 | 待实现 |
| 14 | 四种输出：精简结论/完整辩论/风险清单/Diff 摘要 | `output_template.toml`、`report/render.py` | `report export` | 四种格式均可导出 | P1 | 已实现（md/json 实测；pdf/docx 缺 extra 时降级为 md） |
| 14 | WebUI 报告/Diff/历史页面 | `webui/pages/` | WebUI | 三页可操作 | P1 | 已实现（`test_webui`） |

## 十五、任务熔断与批量队列（§15）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 15 | 三道上限（调用/token/超时）+ 优雅退出 | `config/budget.toml`、`workflow/budget.py` | 配置 | 命中后交付部分结果并标 `partial` | P0 | 已实现（`workflow/budget.py`，部分结果标 partial；调用/token/墙钟三道上限、配置读取与图级 `budget_tripped` 见 `tests/test_resilience.py`；`on_exceed` 已运行期消费：`fail` 抛 `BudgetExceeded`（退出 8，队列不重试），`stop` 降级为 fallback 并标 `partial`） |
| 15 | 队列：并发、限速、失败重试/跳过、进度查询 | `queue/manager.py` | `queue status/submit` | 50 任务并发 3 跑通 | P1 | 已实现（`test_queue`） |
| 15 | 与底座限流/缓存/防重复锁协同 | `queue/locks.py` | 配置 | 同标的不并发重复分析 | P1 | 已实现（`queue/locks.py`） |
| 15 | 暂停/恢复/取消 | `queue/manager.py` | CLI / WebUI | 三操作各一测 | P1 | 已实现（`test_queue`） |

## 十六、WebUI 独立设计（v1.2 §16）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 16.1 | 独立进程/端口/鉴权，不寄生 Admin Console | `webui/launch.py` | `ssenate webui` | 独立启动；无 TickHub 依赖 | P1 | 已实现（`test_webui`） |
| 16.2 | 默认 8788，`--host/--port` 可改 | `cli.webui` | CLI | 端口可改且不冲突 | P1 | 已实现（`test_webui`） |
| 16.3 | 页面清单：分析/进度/报告/Diff/回测/告警/队列/设置/历史 | `webui/pages/` | WebUI | 九页可达 | P1 | 已实现（`test_webui` 十页可达） |
| 16.4 | 运维监控只读展示并跳转 Admin | `webui/pages/ops.py` | WebUI | 无写操作 | P2 | 已实现（`test_webui`） |
| 16.5 | 环境变量（端口/鉴权/后端地址） | `.env.example`、`DEC-003` | 部署 | 变量生效 | P1 | 已实现（DEC-003） |
| 16.6 | 安全与审计：登录、写操作审计、无密钥回显 | `webui/auth.py` | WebUI | 审计记录完整；页面无密钥 | P0 | 已实现（`test_webui`：CSRF、审计、无密钥回显） |
| 16.7 | WebUI 与 CLI/MCP 结果一致 | `tests/test_webui_parity.py` | — | 同一 run 三处一致 | P1 | 已实现（`test_webui_parity`） |

## 十七、PyPI 发布与依赖（§17）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 17 | `pip install stocksenate`，Python ≥3.11 | `pyproject.toml` | 安装 | 干净环境安装成功 | P0 | 已实现（QUOT-56：干净 venv `pip install .` 后 `ssenate --version` / 离线 `analyze` 通过） |
| 17 | extras `[stockdata] [mcp] [webui] [all]` | `pyproject.toml` | 安装 | 每个 extra 单独可装 | P0 | 已实现（`[backtest]` 已移除：回测无 pandas/numpy import，见 DEC-009） |
| 17 | 不提供 `[google]`，httpx 冲突解法写进文档 | `pyproject.toml`、`README.md`、DEC-007 | 文档 | 依赖树无冲突 | P0 | 已实现 |
| 17 | `python -m build` + `twine upload` | `.github/workflows/release.yml` | CI | `twine check` 通过 | P1 | 已实现（tag `v*` 触发：build → `twine check` → artifact → Trusted Publishing 上传；publish job 使用 `environment: pypi` + `id-token: write`，由 PyPI 签发短期凭据，不需要 `PYPI_API_TOKEN` 或任何仓库 secret，见 DEC-011） |
| 17 | NOTICE / LICENSE / Apache-2.0 归属 | `LICENSE`、`NOTICE` | 仓库 | 发布包内含两文件 | P0 | 已实现 |

## 十八、测试、部署、边界与里程碑（§18）

| 章节 | 要求 | 实现位置 | 对外表面 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|---|
| 18.1 | 测试类型：单元/契约/集成/E2E/回归/安全 | `tests/`、`.github/workflows/ci.yml` | CI | 六类目录与用例存在 | P0 | 部分实现（单元/契约/集成/安全边界；新增真实形态回归 `tests/test_tickhub_fixture_regression.py`；浏览器 E2E 无测试，见 §二十一） |
| 18.2 | 18 项新增能力逐项验收 | 本文件 F-01…F-31 | — | 逐项打勾 | P0 | 已验收（QUOT-61：F-14…F-31 共 18 项 → 12 通过 / 3 部分通过 / 3 未通过；逐项证据存于团队私有归档（见 DEC-013）。未通过 F-18 压缩、F-19 Agent Memory、F-29 增量分析仅存配置无运行时代码，去向见报告 §5 遗留清单） |
| 18.3 | 部署：pip / Docker / compose | `Dockerfile`、`docker-compose.yml` | 部署 | compose 两个服务可起 | P2 | 部分实现（WSL2 Ubuntu 24.04 + Docker 29.1.3 / Compose 2.40.3 人工 `docker compose up -d`：webui 与 mcp 均 healthy，8788/8765 `/healthz` 返回 200；实测修掉两处阻塞：`# syntax` 指令拉取前端镜像卡死已移除、容器绑定 0.0.0.0 时两个进程要求 `STOCKSENATE_WEBUI_PASSWORD` / `STOCKSENATE_MCP_TOKEN` 已在 compose 与 `.env.example` 标明必填；未设口令时 webui 303 跳登录页、mcp 无 token 401。镜像构建与双服务启动无自动化测试，见 §二十一） |
| 18.4 | 边界约束：不实盘、不产出价位、不泄露密钥 | 全仓库 | — | 安全工程师复核清单 | P0 | 已实现（`execution=false`、无价位字段、日志脱敏） |
| 18.5 | 里程碑 M0–M4 与退出标准 | `PRD.md` §8 | — | 每个里程碑有验收脚本 | P0 | 已实现（M0–M3 有自动化验收；M4 未开始） |

## 十九、外部链接汇总（§19）

| 章节 | 要求 | 实现位置 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|
| 19 | 逐项核验官网/规范链接可访问、许可证一致 | `docs/specs/references.md` | 链接检查脚本 | 链接检查脚本可运行、失效链接被报告 | P2 | 已实现（`docs/specs/references.md` 收录 25 条项目/规范/依赖链接；`scripts/check_links.py` 联网核验 25/25 可访问，`--offline` 或 `STOCKSENATE_SKIP_LINK_CHECK=1` 可跳过；本项目采用 Apache-2.0，第三方归属与许可证声明见 `NOTICE`） |

## 二十、上线前核验项（v1.2 §20）

| 章节 | 要求 | 实现位置 | 验收方式 | 优先级 | 状态 |
|---|---|---|---|---|---|
| 20 | PyPI 名称 `stocksenate` 未被占用 | 发布前人工核验 | `pip index versions stocksenate` | P0 | 待核验 |
| 20 | CLI 名称 `ssenate` 无冲突（避开 `ss`/`sc`） | `docs/decisions/DEC-001` | Windows/Linux 双平台检查 | P0 | 已核验（文档记录） |
| 20 | 端口 8788/8765 未被 TickHub/Wire 占用 | `DEC-003` | `netstat` 检查 | P0 | 已核验（2026-10-26 本机 netstat 无占用） |
| 20 | Apache-2.0 归属与 NOTICE 完整 | `NOTICE` | 法务复核 | P0 | 已实现 |

---

## 二十一、无自动化测试的缺口（QUOT-57 登记）

以下五项在本仓库没有任何自动化用例覆盖；其中 compose 有一次性人工验证记录，其余完全未执行。这里如实登记为「无测试」，不用 `skip` 或放宽断言掩盖。

| # | 缺口 | 关联行 | 现状 | 为什么 CI 覆盖不了 | 补测建议 |
|---|---|---|---|---|---|
| G-01 | compose 真实构建 + 双服务启动 | 18.3、1.3 | 人工在 WSL2 跑通过一次；无自动化 | CI runner 无 Docker daemon（或需特权），构建耗时且依赖外网镜像 | 独立 job：`docker compose build` → `up -d` → 轮询 8788/8765 `/healthz` → `down -v`；失败时倾倒容器日志 |
| G-02 | 真实 LLM 全链路 | 2.1、18.1 | 全程 offline/stub；只测了协议与降级 | 需要真实 API Key，成本与稳定性不可控 | 手动 nightly：一个 provider、一个标的、断言 `execution=false` 与 schema 合法，结果归档不进 main CI |
| G-03 | TickHub stdio MCP 通道 | 7.2、10 | 只测本地 import 通道 | 需要可启动的 TickHub MCP 进程与凭据 | 契约测试：以假 MCP server 走 stdio，断言请求/响应帧与 import 通道结果一致 |
| G-04 | 浏览器 E2E | 18.1、16.3 | WebUI 仅 TestClient 级用例 | 需要浏览器与 Playwright 依赖，CI 时间与 flake 成本高 | 独立 job：Playwright 起 `ssenate webui`，点击分析→报告→diff 主路径，产物附截图 |
| G-05 | local 模式六域缺本地归档夹具 | 2.2、7.4 | quote/news/social_buzz/money_flow/dragon_tiger/lockup_calendar 无真实形态本地夹具 | 现有夹具是通用 `items` 占位，抽取路径未被真实载荷驱动 | 按 `tests/fixtures/tickhub/` 的方式补六域真实载荷，并断言 local 路由下各域 `fact_count` 行为 |

---

## 附录 A：安全边界复核清单（安全工程师逐条签字）

| # | 边界 | 检查方法 |
|---|---|---|
| S-01 | 仓库中不存在任何下单/券商/资金划转代码或依赖 | 关键词与依赖树扫描 |
| S-02 | 报告与 WebUI 不出现建仓价/止损位/目标价/仓位比例 | 输出 Schema 与模板字段审查 |
| S-03 | 日志与错误信息不打印 API key、token、DSN | 脱敏单测 + 日志抽样 |
| S-04 | Webhook 强制验签与幂等键，重放不产生重复副作用 | 重放攻击测试 |
| S-05 | MCP http 非本机访问必须鉴权，stdio stdout 纯净 | 未授权请求 401；stdout 解析 |
| S-06 | 外部文本的注入指令被剥离，不改变系统行为 | 注入样本集 |
| S-07 | 数据缺失不会被模型补数值 | 断网运行，检查 `data_source_status` |
| S-08 | 回测不使用晚于分析日的数据 | 注入未来数据必失败 |
| S-09 | WebUI 写操作有审计、无密钥回显 | 页面与审计日志检查 |
| S-10 | 依赖无已知高危漏洞（`pip-audit`） | CI 报告 |
