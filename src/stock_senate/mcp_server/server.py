"""StockSenate MCP server: stdio and Streamable HTTP.

Naming, ports and authentication follow ``DEC-003``:

* service name ``stock-senate`` (never ``tickhub``), tools prefixed
  ``stock_senate_``,
* HTTP defaults to ``127.0.0.1:8765``; a non-loopback bind is refused unless
  ``STOCKSENATE_MCP_TOKEN`` is set, and then every request must carry it as a
  bearer token,
* the WebUI password never doubles as this token,
* stdio carries nothing but MCP frames on stdout — application logs go to
  stderr through :mod:`stock_senate.observability.logging`.

``build()`` returns a fully registered server without opening a socket, so
tests can exercise ``tools/list`` and ``tools/call`` in-process.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any

from stock_senate.mcp_server import tools as tool_impl
from stock_senate.version import __version__

SERVICE_NAME = "stock-senate"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
HEALTH_PATH = "/healthz"
EXIT_CONFIG = 3  # matches stock_senate.cli.EXIT_CONFIG

TOKEN_ENV = (
    "STOCKSENATE_MCP_TOKEN",
    "STOCKSENATE_MCP_HTTP_TOKEN",
    "QF_COUNCIL_MCP_TOKEN",
    "TRADINGAGENTS_MCP_TOKEN",
)
_OPEN_PATHS = frozenset({HEALTH_PATH})

INSTRUCTIONS = (
    "StockSenate 多角色投研合议系统。只提供研究分析、回测推演与本地报告读取，"
    "不输出可执行价位（建仓价/止损位/目标价/仓位）或交易指令。"
    "数据缺失时如实标注 degraded/missing，不做补数。"
)


class MCPAuthError(RuntimeError):
    """A non-loopback bind was requested without a bearer token."""


def resolve_token() -> str:
    """Return the configured MCP bearer token, or an empty string."""
    for name in TOKEN_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def is_loopback(host: str) -> bool:
    text = str(host or "").strip().strip("[]").lower()
    if not text:
        return False
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def require_token_for(host: str, *, token: str | None = None) -> str:
    """Return the bearer token, refusing an unprotected non-loopback bind."""
    value = (token if token is not None else resolve_token()).strip()
    if value:
        return value
    if is_loopback(host):
        return ""
    raise MCPAuthError(
        f"非本机地址 {host!r} 必须配置 STOCKSENATE_MCP_TOKEN（MCP HTTP 鉴权）；"
        "本机使用请绑定 127.0.0.1。"
    )


class BearerAuthMiddleware:
    """Pure-ASGI bearer check in front of the Streamable HTTP app.

    ``/healthz`` stays open so a container probe can check liveness without a
    credential; it returns no configuration or data.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = str(token)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") in _OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        if self.token and not self._authorised(scope):
            await self._unauthorised(send)
            return
        await self.app(scope, receive, send)

    def _authorised(self, scope: dict[str, Any]) -> bool:
        supplied = ""
        for raw_key, raw_value in scope.get("headers") or []:
            if raw_key.lower() == b"authorization":
                supplied = raw_value.decode("latin-1").strip()
                break
        scheme, _, credentials = supplied.partition(" ")
        if scheme.lower() != "bearer" or not credentials:
            return False
        return hmac.compare_digest(credentials, self.token)

    async def _unauthorised(self, send: Any) -> None:
        body = json.dumps(
            {"error": "unauthorized", "service": SERVICE_NAME}, ensure_ascii=False
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b'Bearer realm="stock-senate"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build(*, config: Any = None, home: Path | str | None = None) -> Any:
    """Build the MCP server with all five ``stock_senate_*`` tools registered."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name=SERVICE_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    @server.tool(
        name="stock_senate_analysis",
        description=(
            "运行一次完整的多角色投研合议（分析师→多空辩论→研究经理→交易员→风险辩论→终审）。"
            "返回研究评级、依据、风险、质量分与数据覆盖；不含可执行价位、仓位或交易指令。"
        ),
        structured_output=False,
    )
    def analysis(
        code: str,
        date: str | None = None,
        hitl: bool = True,
    ) -> dict[str, Any]:
        return tool_impl.analyse(code, date, hitl, config=config, home=home)

    @server.tool(
        name="stock_senate_backtest",
        description=(
            "回测推演：信号在收盘产生、次日开盘成交（T+1），按持有窗口与费率计算模拟指标。"
            "返回 run_id、metrics、per_code 与本地结果文件引用；不是实盘建议。"
        ),
        structured_output=False,
    )
    def backtest(
        codes: list[str],
        start: str,
        end: str,
        window: int = 5,
        slippage: float = 0.0,
        fee: float = 0.0,
    ) -> dict[str, Any]:
        return tool_impl.backtest(
            codes,
            start,
            end,
            window=window,
            slippage=slippage,
            fee=fee,
            config=config,
            home=home,
        )

    @server.tool(
        name="stock_senate_alert_query",
        description="读取本地已投递的告警日志，可按代码、级别、起始日期过滤（最新在后）。",
        structured_output=False,
    )
    def alert_query(
        code: str | None = None,
        level: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        return tool_impl.alert_query(code, level, since, home=home)

    @server.tool(
        name="stock_senate_report_query",
        description=(
            "读取已保存的运行快照：给 run_id，或同时给 code 与 date 取当日最新一次。"
            "返回结构化报告分段与报告文件路径。"
        ),
        structured_output=False,
    )
    def report_query(
        run_id: str | None = None,
        code: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        return tool_impl.report_query(run_id, code, date, config=config, home=home)

    @server.tool(
        name="stock_senate_config_query",
        description="返回生效配置（可指定 section）；所有密钥、token 与 DSN 均已打码。",
        structured_output=False,
    )
    def config_query(section: str | None = None) -> dict[str, Any]:
        return tool_impl.config_query(section, config=config)

    @server.custom_route(HEALTH_PATH, methods=["GET"], name="healthz")
    async def healthz(request: Any) -> Any:  # noqa: ANN401 - Starlette Request
        from starlette.responses import JSONResponse

        return JSONResponse({"service": SERVICE_NAME, "status": "ok", "version": __version__})

    return server


def http_app(server: Any = None, *, token: str, host: str = DEFAULT_HOST) -> Any:
    """Return the Streamable HTTP ASGI app, wrapped in bearer auth when set."""
    server = server or build()
    app = server.streamable_http_app(host=host)
    if token:
        app = BearerAuthMiddleware(app, token)
    return app


def run(
    transport: str = "stdio",
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    config: Any = None,
    home: Path | str | None = None,
) -> None:
    """Serve MCP over ``stdio`` or ``http`` (Streamable HTTP)."""
    kind = str(transport or "stdio").strip().lower()
    server = build(config=config, home=home)

    if kind == "stdio":
        # The MCP stdio transport claims fd 0/1 and diverts fd 1 to stderr for
        # the duration, so no extra redirection is needed here.
        server.run(transport="stdio")
        return

    if kind not in {"http", "streamable-http", "streamable_http"}:
        raise ValueError(f"不支持的 MCP 传输: {transport!r}（可用 stdio / http）")

    try:
        token = require_token_for(host)
    except MCPAuthError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from None

    _serve_http(server, host=host, port=port, token=token)


def _serve_http(server: Any, *, host: str, port: int, token: str) -> None:
    import uvicorn  # shipped with the mcp extra

    from stock_senate.observability.logging import get_logger

    app = http_app(server, token=token, host=host)
    get_logger("mcp").info(
        "mcp http listening",
        extra={
            "event": "mcp.http.start",
            "service": SERVICE_NAME,
            "host": host,
            "port": int(port),
            "auth": bool(token),
        },
    )
    uvicorn.run(app, host=host, port=int(port), log_level="info")


__all__ = [
    "BearerAuthMiddleware",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "EXIT_CONFIG",
    "HEALTH_PATH",
    "MCPAuthError",
    "SERVICE_NAME",
    "build",
    "http_app",
    "is_loopback",
    "require_token_for",
    "resolve_token",
    "run",
]
