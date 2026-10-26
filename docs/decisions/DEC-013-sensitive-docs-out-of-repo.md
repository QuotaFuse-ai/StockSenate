# DEC-013：开发过程文档与需求基线移出公开仓库

- 日期：2026-10-26
- 状态：已确认（产品负责人，QUOT-49）
- 关联：QUOT-49（移除详细开发过程文档）、QUOT-63（公开仓库泄露需求基线，P0）、DEC-012（自研口径）

## 背景

产品负责人在 QUOT-49 上要求：从 GitHub 仓库移除 `docs/` 下的详细开发过程文档，其中含有不宜公开的信息。这批文档不是产品说明，而是交付过程中的工作记录：逐项验收证据、缺陷复现细节、安全审计的攻击可达性分析、以及本机路径与测试用假凭据样本。

同期 QUOT-63 记录：仓库此前是公开状态，根目录四份需求基线文件（`councli.txt` / `councli文档.txt` 及对应 `.docx`）被 `.gitignore` 的两条白名单主动纳入版本控制，已随多个 commit 与 tag 出现在公开历史上。

两者是同一类问题——**面向开发过程与本机环境的材料，不应进入对外仓库**——因此合并为一条决定。

## 决定

1. **`docs/tasks/` 不再入库**：按任务组织的交付过程记录（含证据 JSON、验收报告、安全审计）整体移出仓库，归档到仓库外的私有目录（本机 `_doc_archive/StockSenate-docs-2026-10-26/docs-tasks/`），不在仓库、不在分发物、不在镜像中出现。
2. **四份需求基线文件移出仓库**：`councli*` 一并移出，归档到同一私有目录的 `requirement-originals/`。`.gitignore` 中原有的 `!councli.docx`、`!councli文档.docx` 两条白名单删除——它们正是这两份文件得以入库的原因。
3. **忽略规则改为黑名单式**：`.gitignore` 增加 `councli*` 与 `docs/tasks/`，与既有的 `*.docx`、`*.pdf` 一起构成默认拒绝。
4. **CI 增加防泄漏门禁**：`.github/workflows/ci.yml` 新增 `leak-guard` 作业，`git ls-files` 命中 `councli*`、`docs/tasks/`、`*.docx`、`*.pdf` 即失败。忽略规则挡不住 `git add -f` 与分支重建，因此以索引内容为准。
5. **对外文档只描述产品**：`README.md`、`docs/specs/*` 不再按文件名引用需求基线，也不再指向 `docs/tasks/*`。追溯矩阵继续保留需求条目与验收口径，证据留在私有归档。
6. **DEC-010 / DEC-012 不改写**：决策记录只增不改，其中对 `docs/tasks/*` 与四份基线文件的引用按本决定理解为私有归档位置。

## 影响

- 入库文件减少 17 个（`docs/tasks/` 13 个 + 根目录需求基线 4 个）。
- `docs/specs/requirements-traceability.md`、`docs/specs/PRD.md`、`README.md` 的引用同步调整，矩阵条目与验收结论不变。
- 后续任务的交付记录写在私有归档，不再提交；如需入库，只能提交脱敏后的结论性内容。

## 未完成（需仓库 owner 执行）

本决定只处理**当前工作树**。历史清理与可见性变更属于破坏性共享操作，由 owner 执行：

1. 仓库转为 private（QUOT-63 第 2 步）。
2. `git filter-repo --invert-paths` 清除历史中的 `docs/tasks/`、`councli*`，然后 `git push --force --all` 与 `--tags`（第 5 步，会改写所有分支与 tag 的 SHA）。
3. 向 GitHub Support 申请清理缓存中的历史页面与 tag 源码包（第 6 步）。

在上述三步完成前，历史中的旧内容仍可被访问；`git rm` 只影响之后的提交。

## 核验（2026-10-26）

- `git ls-files` 命中 `councli*` / `docs/tasks/` / `*.docx` / `*.pdf`：0 条。
- `.gitignore` 中 `!councli*` 白名单已删除，`git check-ignore -v councli.txt docs/tasks/TASK-QUOT-49/README.md` 均命中忽略规则。
- 本地归档 17 个文件全部可读，仓库工作树不再包含这些内容。
