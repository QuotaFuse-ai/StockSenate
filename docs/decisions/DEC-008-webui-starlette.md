# DEC-008 WebUI 采用 Starlette + uvicorn，不引入 Streamlit

- 状态：已采纳
- 日期：2026-10-26
- 影响版本：0.1.0+

## 背景

上游 TradingAgents 的交互界面是 Streamlit 脚本：页面逻辑与进程启动方式绑在 `streamlit run` 上，状态放 `st.session_state`，无法在同一进程里挂载自定义鉴权中间件，也不方便用 `TestClient` 做端到端断言。本仓库 v1.2 §16 要求 WebUI 具备独立进程/端口（8788）、登录态、CSRF、写操作审计、无密钥回显，且与 MCP 服务（8765）鉴权严格分离。这些要求与 Streamlit 的编程模型冲突：Streamlit 的会话模型不暴露请求级中间件，写操作审计只能在页面函数里零散记录，登录态也没有标准位置存放。

## 决策

1. WebUI 用 **Starlette** 实现路由与中间件，用 **uvicorn** 启动，端口默认 8788（DEC-003）。
2. 登录态用签名的 HTTP-only cookie（`SessionMiddleware`），密码来自 `STOCKSENATE_WEBUI_PASSWORD`；MCP 的 bearer token（`STOCKSENATE_MCP_HTTP_TOKEN`）**不作为** WebUI 登录凭据，反之亦然。
3. 所有写操作走 POST + CSRF token 校验，写入前先落审计记录（`webui/auth.py`）。
4. 页面渲染用 Python 端字符串模板（`webui/templates.py`），不引入 Jinja2/Streamlit 运行时依赖。
5. `pyproject.toml` 的 `[webui]` extra 从 `streamlit + pandas` 改为 `starlette + uvicorn`，与实现一致；`[all]` 随之生效。
6. 报告、Diff、历史三页只读 `report/store` 的落盘快照，与 CLI / MCP 读同一份数据，因此 16.7 的"三处一致"由 `tests/test_webui_parity.py` 断言。

## 后果

- 优点：登录态、CSRF、审计、端口隔离都在标准 ASGI 中间件里完成，可用 `starlette.testclient.TestClient` 在进程内断言，`tests/test_webui.py` 的十页可达、鉴权、审计、无密钥回显测试得以自动化。
- 优点：不再依赖 Streamlit 的自动重跑模型，页面行为可预测，便于和 CLI/MCP 的结果对齐。
- 代价：放弃了 Streamlit 的即时交互组件，图表与筛选需要自己写 HTML/JSON 接口；后续若要做复杂交互，需继续在 ASGI 层扩展，而不是换回 Streamlit。
- 代价：`uvicorn` 与 `starlette` 同时也被 `[mcp]` 传递安装，`[webui]` 单独安装时显式声明二者，避免"装了 webui 却缺 starlette"。

## 相关

- `src/stock_senate/webui/`（`launch.py`、`auth.py`、`state.py`、`templates.py`、`pages/`）
- `tests/test_webui.py`、`tests/test_webui_parity.py`
- `pyproject.toml` `[project.optional-dependencies]`
- `docs/decisions/DEC-003-service-topology-and-ports.md`
- `docs/specs/requirements-traceability.md` §1.1、§16
