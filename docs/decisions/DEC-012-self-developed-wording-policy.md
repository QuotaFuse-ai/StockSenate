# DEC-012：文档统一为自研产品口径

- 日期：2026-10-26
- 状态：已确认（产品负责人）
- 关联：DEC-001（命名与兼容）、DEC-011（命名确定为 stocksenate）、QUOT-49

## 背景

产品负责人在 QUOT-49 上明确：StockSenate 是团队自研产品，文档与对外材料不得把任何外部项目写成能力基线或能力来源，也不得以对照式表述，或声称按百分比还原某个既有实现的措辞，来描述本项目。此前 README、`docs/specs/*`、配置注释与部分校验提示沿用了这套说法，容易让读者误以为本项目是他人实现的衍生物。

## 决定

1. **能力表述直接写本项目**：README 与 PRD 直接列出角色链路、数据层边界、报告与交互形态，不再使用两列对照表。
2. **能力基线是本项目自己的契约**：CLI 语义、`propagate` 返回值、`role_llms` 键位、checkpoint、报告分区，以 `docs/specs/*` 与自动化测试为准，不以外部项目为验收基准。
3. **许可证归属照常保留**：`NOTICE` 与 `docs/specs/references.md` 继续列出第三方项目与 Apache-2.0 归属，这是法律义务，与产品口径无关；归属表述统一为「独立实现 + 第三方归属声明」。
4. **文档标题去对照化**：`references.md` 改为「外部链接清单」，其许可证小节改为「研究语境与许可证」；`cli-reference.md` 改为「CLI / Python API / MCP 手册」；追踪矩阵第四、五章改为「基础能力清单」「Issue 闭环清单」。
5. **统一停用一类措辞**：凡暗示本项目派生自其他项目的定语与对照式表述一律停用；「可复现」保留，仅表示结果可重复验证。
6. **历史记录**：`docs/decisions/*`（DEC-002 / DEC-004 / DEC-008）与 `docs/tasks/*` 只做最小措辞替换，事实、结论与验收口径未改；原文保留在 git 历史。
7. **导入文档同步**：仓库根目录四份 v1.2 导入文档（`councli.txt`、`councli文档.txt`、`councli.docx`、`councli文档.docx`）按同一口径替换，内容与编号未变。

## 影响

- 改动文件：`README.md`、`docs/specs/{PRD,priority,references,requirements-traceability,cli-reference}.md`、`config/llm_config.toml`、`src/stock_senate/config/schema.py`（校验提示文案）、`src/stock_senate/graph/senate_graph.py`、`tests/test_contracts.py`、四份导入文档。
- `references.md` 链接总数保持 25 条，`scripts/check_links.py` 与追踪矩阵第 19 条的统计口径不变。
- 后续新增文档按本决定执行；如需恢复对照式表述，需另立决策记录。

## 核验（2026-10-26）

- 全仓文本文件扫描（排除 `.git`、依赖目录与二进制）：停用措辞 0 命中。
- 两轮 `.docx` 重写后 `zipfile.testzip()` 为 `None`，所有 XML 部件可正常解析。
- 测试套件全绿，计数与本次改动前一致。
