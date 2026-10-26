# TASK-QUOT-59：B 组工程化收口

范围按 DEC-010：5.10（Linux CI 修复）、5.11（uv.lock）、17（release workflow）、18.3（compose 实测）、19（链接核验）。
QUOT-60（5.9 日志 / 1.1 checkpoint）与 QUOT-61（18.2）不在本任务内。

## 5.10 Linux pytest 失败

失败现场是 CI run `40279568657`（commit `d5977f7`）：ubuntu 3.11/3.12 在 pytest 阶段挂掉，lint / typecheck / audit / 依赖隔离与 windows 全绿。

根因是平台环境变量触发的渲染差异，不是断言写错：

1. GitHub Actions 设置 `GITHUB_ACTIONS=true`，Typer 的 `rich_utils` 据此把 `FORCE_TERMINAL` 置为 `True`，即使 stdout/stderr 是管道也强制走 Rich 终端渲染。
2. Rich 的 `OptionHighlighter` 用 `(^|\W)(?P<switch>-\w+)` 等正则着色，`--start` 会被切成 `-` 与 `-start` 两段并插入 ANSI 转义。
3. 于是 `assert "--start" in result.stderr` 在 Linux runner 上看到的是带转义的分段文本，Windows 本地/CD 环境不强制终端所以能过。

修复只改测试子进程的环境（`tests/cli_support.py` 的 `cli_env()`）：

```python
env["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
```

这是 Typer 自带的关闭开关，断言与被测用例一行未改，也没有新增 `skipif`：

- 未用 skipif、未改任何断言；合并 main 后本地 `GITHUB_ACTIONS=true CI=true` 下 225 passed（连跑 5 次）；
- `ruff check` / `ruff format --check` 干净（105 files）；
- PR 最终 CI：run `40291436449`，lint / typecheck / 四个 test 矩阵 / lockfile / audit / package / dependency-isolation 共 10 个 job 全部 success。

## CI 复核：合并 main 后暴露的基线问题

PR #1 合入 `79768c9` 后，CI 又暴露两处与该提交相关的问题（不是 5.10 改动引入）：

1. 收集阶段 `ModuleNotFoundError: No module named 'tests'`：`tests/test_code_traversal.py` 与 `tests/test_dotenv.py` 以 `from tests.cli_support import ...` 引辅助模块，而仓库没有 `tests/__init__.py`；CI 跑的是裸 `pytest` 控制台脚本，不会把仓库根加入 `sys.path`（`python -m pytest` 会，所以本地容易漏掉）。已在 `pyproject.toml` 声明 `pythonpath = ["."]`，用例与断言零改动。
2. `test_mcp_stdio_stdout_is_still_only_jsonrpc_with_a_dotenv` 竞态：`subprocess.run(input=...)` 立即关闭 stdin，stdio server 可能在 `tools/list` 仍在派发时因 EOF 退出，全套跑约 3 次红 1 次。改为先读到两条 JSON-RPC 响应再关闭 stdin；断言（恰好 2 条、均为 `jsonrpc: 2.0`、stdout/stderr 不含 sentinel）完全保留。
3. `ruff format --check src tests` 对该提交新增/修改的 5 个文件报格式差异（CI 与本地同为 ruff 0.17.2），已按 `ruff format` 修正，仅格式变化。

## 5.11 uv.lock

- 用 uv 0.13.5 生成 `uv.lock`（解析 107 个包）并提交。
- 干净虚拟环境执行 `uv sync --frozen --extra dev` 安装后，导入 typer / rich / starlette / mcp 正常，pytest 225 passed。
- CI 新增 `lockfile` job：`uv sync --frozen --extra dev` + `uv run --frozen pytest`。`--frozen` 保证手改依赖范围或锁文件过期时直接失败，而不是静默漂移；该 job 在 run `40291436449` 为 success。

## 17 release workflow

新增 `.github/workflows/release.yml`，触发条件为 `v*` tag 或手动 dispatch：

- `build` job：`python -m build` → `twine check dist/*` → 上传 artifact；
- `publish` job：仅 tag 触发，读取 `secrets.PYPI_API_TOKEN`，变量为空则跳过上传并打印配置提示。

仓库里没有任何凭据值。**使用方需要在仓库 Settings → Secrets 里自行创建 `PYPI_API_TOKEN`**，否则发布步骤只会跳过。

## 18.3 compose 实测

环境：WSL2 Ubuntu 24.04，Docker 29.1.3，Compose 2.40.3。

实测暴露两处会导致 `docker compose up` 起不来的真实问题，均已修复：

1. `Dockerfile` 首行 `# syntax=docker/dockerfile:1.7` 会先拉取 Dockerfile 前端镜像，在本环境的镜像源上长时间卡住。该 Dockerfile 只用标准语法，已删除这一行。
2. 容器内必须绑定 `0.0.0.0` 才能被端口映射访问，而 `webui` 与 `mcp` 都拒绝无鉴权的非回环绑定（`launch.py` / `mcp_server/server.py` 的护栏）。原 compose 没有传达这一点，webui 会以 `ValueError: 拒绝绑定非回环地址 0.0.0.0` 退出、并被 `restart: unless-stopped` 反复拉起。现已在 `docker-compose.yml` 与 `.env.example` 标明 `STOCKSENATE_WEBUI_PASSWORD` 与 `STOCKSENATE_MCP_TOKEN` 在容器部署下必填（守护逻辑本身未放宽）。

验证步骤（口令为本地临时生成，`.env` 已被 `.gitignore` 忽略，未提交）：

```bash
printf 'STOCKSENATE_WEBUI_PASSWORD=%s\nSTOCKSENATE_MCP_TOKEN=%s\n' \
  "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" > .env
docker build --network host -t stocksenate:latest --build-arg EXTRAS=all .
docker compose up -d --no-build
docker compose ps
```

结果：

```text
NAME                IMAGE                COMMAND                  SERVICE   STATUS                    PORTS
stocksenate-mcp     stocksenate:latest   "ssenate-entrypoint …"   mcp       Up (healthy)              127.0.0.1:8765->8765/tcp
stocksenate-webui   stocksenate:latest   "ssenate-entrypoint …"   webui     Up (healthy)              127.0.0.1:8788->8788/tcp

GET  http://127.0.0.1:8788/healthz -> 200 {"ok":true,"service":"stock-senate-webui","version":"0.1.0"}
GET  http://127.0.0.1:8788/          -> 303 /login?next=%2F
GET  http://127.0.0.1:8765/healthz -> 200 {"service":"stock-senate","status":"ok","version":"0.1.0"}
POST http://127.0.0.1:8765/mcp（无 token）-> 401
```

说明：本环境 Docker 桥接网络的 DNS 不可用（容器内解析 `deb.debian.org` 报 `EAI_AGAIN`，宿主机正常），因此构建用 `--network host` 绕过。这是测试机网络问题，不是仓库缺陷；`docker compose up -d --no-build` 与运行期健康检查不受影响。

## 19 链接核验

- `docs/specs/references.md`：项目入口、研究语境与许可证、规范标准、运行时依赖官方文档、数据底座、本地规范文档，共 25 条链接。
- `scripts/check_links.py`：纯标准库，HEAD 优先、405/403 回退 GET，跟随重定向，末尾标点自动剥离，聚合报告 broken 链接并以退出码表达结果；`--offline` 或 `STOCKSENATE_SKIP_LINK_CHECK=1` 可在无网环境跳过。
- 核验中发现并修正的失效/不可用链接：`keepachangelog.com/1.1.0/`（404）、`www.starlette.io` 与 `www.uvicorn.org`（本网络 TLS 失败）、`docs.anthropic.com`（证书过期且区域受限，改指官方 SDK 仓库）。最终 25/25 可访问。

## 变更文件

- `tests/cli_support.py`（5.10）
- `.github/workflows/ci.yml`（5.11 lockfile job）
- `.github/workflows/release.yml`（17）
- `uv.lock`（5.11）
- `Dockerfile`、`docker-compose.yml`、`.env.example`（18.3）
- `docs/specs/references.md`、`scripts/check_links.py`（19）
- `docs/specs/requirements-traceability.md`（5.10 / 5.11 / 17 / 18.3 / 19 五行状态）
- `pyproject.toml`（pytest `pythonpath`）、`tests/test_dotenv.py`（stdio 竞态）
- ruff 格式化的 5 个基线文件：`src/stock_senate/cli.py`、`src/stock_senate/graph/senate_graph.py`、`src/stock_senate/mcp_server/tools.py`、`tests/test_cli.py`、`tests/test_config_schema.py`

## 遗留事项

- 容器部署首次启动前必须自行设置 `STOCKSENATE_WEBUI_PASSWORD` 与 `STOCKSENATE_MCP_TOKEN`（compose 头部注释给了生成命令）。
- 本机若容器 DNS 正常，可省略 `--network host` 直接 `docker compose up -d --build`。
