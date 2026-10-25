# DEC-007 httpx 结构性冲突与依赖策略

- 状态：已采纳
- 日期：2026-10-26
- 影响版本：0.1.0+

## 背景

数据底座依赖的 `mootdx` 声明 `httpx>=0.25,<0.26`，而 `langchain-google-genai>=4.0.0` 依赖 `google-genai>=1.53.0`，后者要求 `httpx>=0.28.1`。两者区间不相交，`uv sync` / `pip install` 在同时解析时会失败。这不是版本号写错，而是上游的结构性冲突。

## 决策

1. **本包不直接声明 `httpx`**，由使用方的 extras 间接决定，避免把某个区间钉死。
2. **不提供 `[google]` extra**。Google Gemini 作为可选后端，需要用户显式安装，并在安装说明中给出隔离方案。
3. `pyproject.toml` 的 extras 只保留：`stockdata`、`backtest`、`mcp`、`webui`、`all`、`dev`。
4. 默认 `llm_provider` 使用 MiniMax，不与 `mootdx` 冲突。
5. CI 分两个环境验证：`[all]`（不含 Google）与 `[all] + google-genai`（独立 job，允许对方自行解析）。
6. 文档明确写出冲突与解法，不隐藏、不假装兼容。

## 需要 Gemini 时的做法

```bash
# 方案一：独立虚拟环境，只跑需要 Gemini 的链路
python -m venv .venv-gemini
.venv-gemini/bin/pip install stocksenate google-genai

# 方案二：用 MCP / HTTP 把 Gemini 侧能力外置，主环境保持 mootdx 可装
```

如果未来 `mootdx` 放宽 httpx 区间，再合并为单个 extra；在此之前不冒险。

## 后果

- 默认安装路径始终可解析，`[all]` 不因 Google 而失败。
- 使用 Gemini 的用户需要多一步，但不会遇到「装不上且看不懂原因」。
- 测试工程师的依赖检查只断言「依赖树中不存在 mootdx 与 google-genai 并存」，而非断言某个 httpx 版本。

## 相关

- `pyproject.toml`
- `README.md` 安装章节
- `docs/specs/requirements-traceability.md` §5.5、§17
