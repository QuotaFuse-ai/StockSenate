"""WebUI launcher: Starlette + uvicorn on port 8788 (DEC-003).

The service is deliberately separate from the MCP process and from TickHub's
admin console. It binds to loopback by default; a non-loopback bind is refused
unless ``STOCKSENATE_WEBUI_PASSWORD`` is set, so an unauthenticated console can
never be exposed to the network by accident.
"""

from __future__ import annotations

from typing import Any

from stock_senate.observability.logging import get_logger
from stock_senate.webui.auth import SessionMiddleware, auth_routes
from stock_senate.webui.pages import all_routes
from stock_senate.webui.state import WebRuntime

log = get_logger("webui")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class WebUIDependencyError(RuntimeError):
    """Starlette or uvicorn is missing; the message tells the operator what to do."""


def is_loopback(host: str) -> bool:
    value = str(host or "").strip().lower()
    if value in _LOOPBACK:
        return True
    return value.startswith("127.")


def create_app(runtime: WebRuntime | None = None) -> Any:
    """Build the ASGI app; ``runtime`` is injectable for tests."""
    try:
        from starlette.applications import Starlette
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise WebUIDependencyError(
            "WebUI 需要 starlette；请安装 `pip install -e .[mcp]`（starlette 随 mcp 一起安装）。"
        ) from exc

    service = runtime or WebRuntime()
    routes = [*auth_routes(), *all_routes()]
    app = Starlette(routes=routes)
    app.state.runtime = service
    app.add_middleware(SessionMiddleware, auth=service.auth)
    return app


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Start the WebUI. Raises ``ValueError`` for an unsafe bind."""
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise WebUIDependencyError(
            "WebUI 需要 uvicorn；请安装 `pip install -e .[mcp]`（uvicorn 随 mcp 一起安装）。"
        ) from exc

    app = create_app()
    service: WebRuntime = app.state.runtime
    if not is_loopback(host) and not service.auth.login_required:
        raise ValueError(
            f"拒绝绑定非回环地址 {host}：请先设置 STOCKSENATE_WEBUI_PASSWORD 再对外提供服务"
        )

    scheme = "http://"
    shown = host if is_loopback(host) else "<host>"
    print(f"StockSenate WebUI 已启动：{scheme}{shown}:{port}", flush=True)
    if not service.auth.login_required:
        print("未设置 STOCKSENATE_WEBUI_PASSWORD：本机免登录模式。", flush=True)
    log.info(
        "webui listening",
        extra={"event": "webui.start", "host": host, "port": int(port)},
    )
    uvicorn.run(app, host=host, port=int(port), log_level="info", access_log=False)


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "WebUIDependencyError",
    "create_app",
    "is_loopback",
    "run",
]
