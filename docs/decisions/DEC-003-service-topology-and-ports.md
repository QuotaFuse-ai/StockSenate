# DEC-003 服务拓扑、端口与隔离

- 状态：已采纳
- 日期：2026-10-26
- 影响版本：0.1.0+

## 背景

StockSenate 有三类常驻/半常驻进程：WebUI、MCP Server，以及与 TickHub 生态共存的 Admin Console。三者若共用端口、stdout 或鉴权，会互相污染日志与安全边界。Wire 资讯感知层也占用了固定端口，需要一次性分配。

## 决策

### 端口分配

| 服务 | 端口 | 进程 | 说明 |
|---|---|---|---|
| TickHub Admin Console | 8787 | TickHub 侧 | 运维与底座管理，**不属于本仓库** |
| StockSenate WebUI | 8788 | `ssenate webui` | 独立进程，`--host/--port` 可改 |
| StockSenate MCP HTTP | 8765 | `ssenate mcp http` | Streamable HTTP，默认 `127.0.0.1` |
| TickHub MCP | 8000 | TickHub 侧 | 数据工具，独立服务名 |
| Wire | 8790 / 8791 | Wire 侧 | 资讯感知层，不复用 |

### 隔离要求

1. **进程隔离**：WebUI 与 MCP 各自独立启动，独立容器；WebUI 不内嵌 MCP，MCP 不渲染页面。
2. **命名隔离**：MCP 服务名 `stock-senate`，工具前缀 `stock_senate_*`，与 `tickhub-*` 不重名。
3. **鉴权隔离**：MCP HTTP 非本机访问必须携带 bearer token（`STOCKSENATE_MCP_TOKEN`）；WebUI 登录态与 MCP token 不共用。
4. **日志隔离**：MCP stdio 模式下 stdout **只允许** JSON-RPC 报文，所有日志走 stderr；WebUI 日志走自身 sink。
5. **可观测串联**：跨进程用 `trace_id` 关联，不依赖共享日志文件。

### 职责边界

| 组件 | 做 | 不做 |
|---|---|---|
| StockSenate WebUI | 发起分析、看报告/Diff/回测/告警/队列、只读运维概览 | 不做底座运维写操作 |
| StockSenate MCP | 暴露分析/回测/告警/报告/配置查询工具 | 不替代 TickHub MCP 取数 |
| TickHub Admin | 底座的采集、缓存、限流、运维 | 不做投研合议 |

## 后果

- 端口表进入 `.env.example` 与 compose，测试工程师按表做 `netstat` 核验。
- stdio 纯净性是可测契约：捕获 stdout 必须逐行可解析。
- WebUI 的运维页只读并跳转 8787，避免权限越界。

## 相关

- `docs/specs/requirements-traceability.md` §3、§10、§16.5、§20
- `docker-compose.yml`、`.env.example`
