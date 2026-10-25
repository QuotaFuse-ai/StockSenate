# DEC-001 命名与兼容策略

- 状态：已采纳
- 日期：2026-10-26
- 影响版本：0.1.0+

## 背景

开发文档 v1.1 使用 `quota-fuse-council` / `qf_council` / `qfc` 三套命名，v1.2 改为 StockSenate，但仍有旧的 `TradingAgents` 兼容诉求。同一份文档内存在 `qf-council` MCP 服务名、`~/.qf_council/` 配置目录、`QF_COUNCIL_*` 环境变量与新名称并存的问题，需要一次性定死。

## 决策

| 维度 | 最终值 | 说明 |
|---|---|---|
| 产品名 | StockSenate | 中文名「StockSenate 多角色投研合议系统」 |
| PyPI 分发包 | `stocksenate` | 发布名为最终名，不发布旧名 |
| Python 包 | `stock_senate` | src 布局，`src/stock_senate/` |
| CLI 命令 | `ssenate` | 避开 Linux `ss` 与 Windows `sc` |
| 入口点 | `stock_senate.cli:main` | `[project.scripts]` |
| 配置目录 | `~/.stock_senate/` | 首次启动只读迁移 `~/.tradingagents/` |
| 环境变量前缀 | `STOCKSENATE_*` | 兼容读取旧前缀，新前缀优先 |
| MCP 服务名 | `stock-senate` | 与 `tickhub-*` 区分 |
| 容器名 | `ssenate` / `stocksenate-webui` | 两个进程分容器 |
| 图类 | `SenateGraph` | 保留 `TradingAgentsGraph` 别名 |

旧命令 `tradingagents` 保留为入口点，运行时向 stderr 打印弃用警告后转发到同一 Typer 应用。

## 明确不做

- **不发布 `qf_council` / `quota_fuse_council` 的 Python 包 shim**。旧包名从未上 PyPI，shim 只会让依赖解析更混乱。改为安装说明与 `config migrate` 的迁移提示。
- **不保留 `qf-council` MCP 服务名**。MCP 客户端配置需要人工改一次，一次性切换比长期双名更清晰。
- 不保留 `QF_COUNCIL_*` 的写入路径，只保留读取兼容。

## 环境变量优先级

```
STOCKSENATE_*  >  TRADINGAGENTS_*  >  QF_COUNCIL_*  >  默认值
```

读到旧前缀时在 stderr 提示建议改名，不阻断运行。

## 后果

- 文档、README、测试统一使用上表右列，不再出现 `qf_council` 等旧标识。
- 旧用户升级需要一次 `ssenate config migrate` 或手工设置 `STOCKSENATE_CONFIG_DIR`。
- `tradingagents` 入口点带来少量维护成本，在 1.0 前移除。

## 相关

- `docs/specs/cli-reference.md` 环境变量与命令表
- `docs/specs/requirements-traceability.md` §1.2、§20
- `src/stock_senate/cli.py` `_config_dir()`、`legacy_main()`
