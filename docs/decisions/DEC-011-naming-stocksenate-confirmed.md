# DEC-011：命名确定为 `stocksenate`

- 日期：2026-10-26
- 状态：已确认（产品负责人）
- 关联：DEC-001（命名与兼容）、DEC-010（D-01 补记）、QUOT-49

## 背景

开发文档 v1.2 开篇（第 4–8 行）与 §16 规定的名称仍是 `quota-fuse-council` / `qf_council` / `qfc`；工程侧自 DEC-001 起已按 StockSenate 落地。两者长期并存，D-01 只能记成「改名是团队决定，不是文档已改」。

2026-10-26 产品负责人明确确认：**改名为 `stocksenate`**。

## 决定

| 层 | 定名 |
|---|---|
| PyPI 发行包 | `stocksenate` |
| Python 导入包 | `stock_senate` |
| CLI | `ssenate`（保留 `tradingagents` 弃用入口） |
| 配置根目录 | `~/.stock_senate/` |
| 环境变量前缀 | `STOCKSENATE_*`（兼容读取 `TRADINGAGENTS_*`，新前缀优先） |
| MCP 服务名 | `stock-senate` |

开发文档中的 `quota-fuse-council` / `qf_council` / `qfc` 由本决定取代。源文档不在本仓库，不回改；以本记录为准。

## 未解决

- `stocksenate` 在 PyPI 是否已被占用：需 `pip index versions stocksenate`，当前环境无外网出口，未核验。若已被占用，备选名需另行决策，不得擅自更名。
- 发布凭据（`TWINE_PASSWORD` / `PYPI_API_TOKEN` / `~/.pypirc`）仍缺失，发布动作由负责人执行。

## 影响

- `pyproject.toml`、README、`docs/specs/*` 已全部使用 `stocksenate`，无代码或文档改动。
- D-01 的补记据此成立：改名是团队决定，开发文档未改。
