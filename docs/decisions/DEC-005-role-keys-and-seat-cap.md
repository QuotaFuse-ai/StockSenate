# DEC-005 角色键、别名与 12 席上限

- 状态：已采纳
- 日期：2026-10-26
- 影响版本：0.1.0+

## 背景

文档同时出现「16 个角色」「上限 12 个角色」「7 个内置分析师」三种说法，且 `role_llms` 的键与角色名存在 `technical`/`market`、`basic`/`fundamentals`、`sentiment`/`social`、`unlock`/`lockup` 四组混用。若不定义清楚，配置校验要么误报要么静默回退。

## 决策

### 1. `role_llms` 的 16 个合法键

```
market  social  news  fundamentals  policy  hot_money  lockup
bull  bear
research_manager  trader
risk_aggressive  risk_neutral  risk_conservative
quality_gate  portfolio_manager
```

### 2. 别名归一（只读兼容，不写入）

| 别名 | 归一为 |
|---|---|
| `technical` | `market` |
| `basic` | `fundamentals` |
| `sentiment` | `social` |
| `unlock` | `lockup` |

别名与正式名同时出现且配置不一致时报错，不猜测哪个优先。

### 3. 上限 12 席只约束「分析师」

- 内置分析师 7 个：market / social / news / fundamentals / policy / hot_money / lockup
- 自定义分析师最多 5 个
- 合计 **12 个分析师席位**
- `bull` / `bear` / `research_manager` / `trader` / 三档风险 / `quality_gate` / `portfolio_manager` 属于**固定工作流阶段**，不占分析师席位，也不计入上限

`config validate` 只统计 `kind == "analyst"` 且 `enabled = true` 的条目。第 13 个分析师启用即报错并指出具体键名。

### 4. 自定义角色六项必填

提示词、工具白名单、输出 Schema、模型层级、所属阶段、辩论权重。缺任一项校验报错。

### 5. 未知键必须报错

`role_llms` 中出现既非 16 合法键也非别名的键时直接失败，**禁止静默回退到默认模型**——静默回退会让用户以为换了模型而实际没换。

## 后果

- 「16 个角色」与「12 席上限」不再矛盾：前者是可配置的模型槽位，后者是分析师人数。
- 文档、报告、WebUI 统一显示正式键名，别名只用于读取旧配置。
- 校验器的一处实现即唯一事实来源，测试只需覆盖第 13 席与错键两条。

## 相关

- `src/stock_senate/config/schema.py`（`ROLE_LLM_KEYS` / `ROLE_ALIASES` / `_check_roles`）
- `config/agent_roles.toml`、`config/llm_config.toml`
- `docs/specs/requirements-traceability.md` §8.2、§8.4
