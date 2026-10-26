# TASK-QUOT-58 — P0 安全与配置收口：S-03 密钥回显、S-06 注入清洗、F-01 路径穿越、.env 不生效

- 关联 Issue：QUOT-58（父单 QUOT-49；安全审计 S-01…S-10 的 P0 收口）
- 执行人：后端架构师
- 完成日期：2026-10-26
- 基线：`d5977f7`（开工前 `git pull --ff-only` 至 `a62b767`，含 DEC-010）
- 边界：未引入或提交任何真实密钥（测试全部使用哨兵串）；仓库仍无 `.env`、无 `*.local.toml` 被跟踪；遵守 DEC-007（未改依赖清单，未新增 `httpx`、未加 `[google]` extra）；产品边界不变（报告不输出建仓价/止损位/目标价/仓位）；未修改既有断言，未使用 `xfail`。

## 结论摘要

| # | 项 | 状态 | 关键证据 |
|---|---|---|---|
| 1 | S-03 配置/接口回显密钥与绝对路径 | 已修复 | `config show` 与 MCP 返回值统一走 `redact(..., mask_paths=True)`；哨兵串命中 0、临时绝对路径命中 0 |
| 2 | S-06 外部文本未清洗即进入摘要/提示词 | 已修复 | provider 工厂统一包一层 `SanitizingProvider`；4 类注入样本经清洗后摘要与提示词均无指令片段；清洗计数写入 `source_detail` |
| 3 | F-01 `code` 未规范化导致路径穿越 | 已修复 | 图入口、路由、provider 三层均做 `normalize_code` + 根目录约束；4 类恶意代码在 CLI/MCP/provider 均被拒绝，归档根外文件不可读 |
| 4 | `.env` 已声明依赖但不生效 | 已修复 | CLI 入口 `load_dotenv(override=False)`；仅 `.env` 时生效，已导出变量优先，MCP stdio stdout 仍只有 JSON-RPC |
| 5 | 未知配置键静默通过（低危，本单附带） | 已修复 | 默认告警、`--strict` 报错；随仓库配置 0 告警；名称型表（角色/供应商/渠道等）不误报 |
| 6 | 全量回归 | 通过 | `225 passed / 0 xfail`（基线 192，新增 33 条）；ruff、mypy 全清 |

## 1. S-03 密钥与本地路径回显（中危）

问题：`ssenate config show` 原样输出合并后的配置，`api_key`、`dsn` 等凭据以及 provider 的绝对 `root` 会进入 stdout；MCP 的配置查询与错误返回同样携带本机路径。

修复：

- `observability/logging.py`：`redact(value, *, mask_paths=False)` 增加绝对路径遮罩。密文键仍按既有 `_SECRET_KEY_RE`（`api_key` / `token` / `secret` / `password` / `dsn` 等）整体替换为 `***`；`mask_paths=True` 时，路径类键（`root` / `path` / `dir` / `home` / `file` 等）在值为绝对路径时替换为 `***`，字符串与异常文本中的绝对路径子串也会被替换。默认不遮路径，避免日志失去排障信息。
- `cli.py`：`config show` 改为 `_emit(redact(_load_config_dir(config), mask_paths=True), output)`。
- `mcp_server/tools.py`：`_out()`、`_error()`、`config_query` 均以 `mask_paths=True` 脱敏，CLI 与 MCP 两条出口口径一致。

验证（临时配置，仅哨兵串与临时绝对路径，无真实密钥）：

```bash
# llm_config.toml 注入 api_key = "sk-audit-sentinel-0000"、root = "C:/Users/.../private-archive"
ssenate config show --config <tmp>/config -o json
# 结果：exit=0，sentinel_hits=0，abs_path_hits=0，stderr 0 字节
```

单测：`tests/test_cli.py::test_config_show_does_not_echo_secrets_or_absolute_paths`（哨兵串、`dsn`、`config_dir`、provider `root` 均为 `***`）。

**诚实说明**：脱敏是「按键 + 值形态」判断，非路径键下的绝对路径字符串仍会被字符串级遮罩覆盖，但若用户把路径塞进任意非路径键的普通文本，只保证绝对路径子串被遮罩，不保证整段语义可读。

## 2. S-06 提示词注入清洗（中危）

问题：`TickHubProvider` 等外部文本（公告、研报、论坛）未经 `sanitize_external_text` 就进入 digest 与 LLM 提示词。审计 PoC 中「忽略以上指令」「你现在是」「输出建仓价/止损位」「评级上调为买入」原文可见。

修复：

- `data/providers/base.py`：新增 `sanitize_envelope()` 与 `SanitizingProvider`。对 envelope 的 `payload` 递归清洗所有字符串；清洗计数写入 `source_detail`（`sanitized: injections=N, sentences=M[, matched_terms=K]`），**只记录数量不记录命中词本身**，避免清洗说明再次把违禁词带进提示词。未被改写时原样返回，不产生噪声。
- `data/providers/__init__.py`：`build_provider()` 对所有 provider 统一返回 `SanitizingProvider(...)`，形成单一出口——现有与未来 provider 都无法绕过。
- `data/sanitize.py`：注入命中由「替换短语」改为「整句丢弃 + `[外来指令已移除]` 标记」。仅替换短语会留下「立即清空所有分析」这类真正的命令尾巴。合规过滤口径不变，`PROHIBITED_TERMS` 增补「评级上调为买入 / 评级上调至买入 / 上调评级至买入 / 上调为买入 / 上调至买入」，上游文档不能替参议院下定评级。

验证：

- `tests/test_external_sanitization.py`（7 条）：4 类注入样本（「忽略以上指令」「你现在是交易员」「输出建仓价 10 元/止损位 9 元」「评级上调为买入」）分别经 provider → digest → prompt 断言无指令片段且正常事实保留；`test_sanitization_is_recorded_not_silent` 断言改写有记录；`test_benign_text_is_untouched` 断言正常文本零改写；`test_factory_wraps_every_provider` 断言工厂出口统一包装。
- 未放宽合规过滤：`test_config_schema.py` 中原有 `text_doc_must_not_extract_numbers`、`llm_fill_must_stay_forbidden`、`output_template_must_forbid_order_fields` 均未改动并继续通过。

**诚实说明**：清洗是规则式的（句式 + 词表），对同义改写、跨句拼接、编码混淆不保证拦截；本轮未引入模型判别，避免新增外部依赖与不可解释行为。

## 3. F-01 标的代码路径穿越（中危）

问题：`code` 原样参与 `root/domain/code` 路径拼接，`../../etc/passwd`、`C:\Windows\win.ini` 等可逃逸归档根；CLI `analyze` 与 MCP `stock_senate_analysis` 均可触达。

修复（三层防御，任一层单独成立即可阻断）：

1. `graph/senate_graph.py`：`propagate` 入口 `normalize_code`，非法代码直接 `ValueError("analyze 需要合法的 A 股代码（6 位数字，可带 sh/sz/bj 前缀）")`。
2. `data/router.py`：`DataAccessLayer.fetch` 最先规范化；失败时记录一次 router attempt 并返回 `DataEnvelope.missing(..., detail="非法标的代码，已拒绝进入数据源")`，原始字符串仅作为 envelope 的 `code` 保留用于追溯，**不会下传 provider**。
3. `data/providers/base.py`：`Provider._safe_subdir(domain, code)` 统一「规范化 → `resolve()` → `is_relative_to(root)`」，不满足即 `ProviderNotAvailable`；`local_json.py`、`text_doc.py` 改为走该方法，直接调用 provider 也无法越界。

验证：`tests/test_code_traversal.py`（14 条）：

| 场景 | 断言 |
|---|---|
| `../../etc/passwd`、`..%2f..%2f`、`/etc/passwd`、`C:\Windows\win.ini` | router 返回 `missing`，provider 从未被调用 |
| 同上 4 类直达 `local_json` | 抛 `ProviderNotAvailable`，根外文件读不到 |
| 归档目录为指向根外的符号链接 | 解析后被拒绝 |
| 图入口 / CLI `analyze` / MCP `analysis` | 恶意代码统一被拒，不产生数据读取 |
| 合法代码 | 规范化后的代码传给 provider |
| 非法代码的 missing envelope | 保留请求原始 `code` 以便追溯，不静默改写 |

**诚实说明**：`..%2f..%2f` 不是被「解码后识别」，而是因含 `%` 与 `/` 不满足 A 股代码格式在规范化阶段被拒；两者效果一致，但不要把它理解为 URL 解码防护。当前代码规则为 6 位数字，可带 `sh/sz/bj` 前缀，港股/美股代码仍不受支持（属既有范围，非本单引入）。

## 4. `.env` 生效

问题：`pyproject.toml` 已声明 `python-dotenv>=1.0`，但入口从未调用 `load_dotenv`，仅放 `.env` 不生效。

修复：`cli.py` 新增 `_load_dotenv()`，在 `main()` 与 `legacy_main()` 的最前面调用：

```python
load_dotenv(dotenv_path=Path.cwd() / ".env", override=False, verbose=False)
```

- 只读当前工作目录的 `.env`，不向上搜索、不读用户目录，避免意外拾取无关凭据。
- `override=False`：已导出的环境变量优先，部署环境仍可覆盖。
- MCP stdio 仍由同一入口启动，dotenv 不向 stdout 输出任何内容。

验证：`tests/test_dotenv.py`（5 条）：`.env` 值到达 `RoleLLMResolver`；已导出变量优先；子进程 CLI 从 `.env` 读取 `STOCKSENATE_CONFIG_DIR`；导出值压过 `.env`；带 `.env` 时 MCP stdio stdout 只有 JSON-RPC 且不含哨兵串。`README.md` 与 `.env.example` 同步说明「自动读取 `./.env`，已导出变量优先」。

## 5. 未知配置键告警（低危，本单附带）

问题：审计 PoC 显示拼错或未支持的键 `ok=True` 且无任何提示。

修复：

- `config/schema.py`：为 7 个配置文件建立键白名单树。闭合表逐键枚举；名称由用户自定义的表（`roles.*`、`roles.custom.*`、`providers.*`、`routing.*`、`channels.*`、`sections.*`、`pricing.table.*`）用 `_ANY` 放行任意子名；标量/列表以 `None` 终止递归。
- 未知键默认进入 `warnings`（`ok` 仍为 `true`，退出码 0）；`validate_dir(..., strict=True)` 时进入 `errors`（退出码 3）。
- `cli.py`：`config validate --strict` 暴露该模式。

验证：

- `tests/test_config_schema.py`（新增 5 条）：默认告警、`strict` 转错误、顶层未知表、角色条目内未知键、名称型表新增名字零告警。
- `tests/test_cli.py::test_config_validate_strict_rejects_unknown_keys`：注入 `debate.max_debate_roundz`，宽松模式 exit 0 / `ok=True`，`--strict` exit 3 / `ok=False` 且定位到 `debate.max_debate_roundz`。
- 随仓库 `config/` 在默认与 `--strict` 下均 0 告警、`ok=True`，未引入误报。

**诚实说明**：白名单校验解决「拼写错误静默失效」，**不解决** DEC-010 中 C 组 D-02「配置 schema 二选一」的方向问题，也不改变 D-03 角色字段裁决；供应商配置块按其 `kind` 开放，未知的子键仍会告警但不会被拒绝。

## 6. 回归与边界核对

| 检查 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `.venv/Scripts/python.exe -m pytest -o addopts="" -q` | **225 passed / 0 xfail**（基线 192） |
| 新增用例 | traversal 14 + dotenv 5 + sanitization 7 + CLI 2 + schema 5 | 33 条，全部通过 |
| 静态检查 | `ruff check src tests` | All checks passed |
| 类型检查 | `mypy`（10 个改动源文件） | Success: no issues found |
| 密钥卫生 | `git ls-files` 匹配 `^\.env$` / `*.local.toml` | none |
| 依赖约束 | `git diff -- pyproject.toml uv.lock` | 空（DEC-007 未被破坏） |
| 配置回显 | 哨兵 `sk-audit-sentinel-0000` | stdout/stderr 均 0 命中 |

未做 / 不在本单范围：

- 未修改 `docs/tasks/TASK-QUOT-49/security-audit-S01-S10.md` 原文，复测记录独立放在本目录。
- S-05（MCP 非回环鉴权）、S-07（离线域范围）、S-10（依赖审计）未在本单改动；DEC-010 C 组 D-02/D-03/D-05/D-08 仍未裁决。
- 本次为规则级防护，未进行真实 LLM 红队；建议在 QUOT-61 的十八项验收中补一次端到端注入回归。
