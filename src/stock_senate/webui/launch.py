"""WebUI launcher: Starlette + uvicorn on port 8788 (DEC-003).

The service is deliberately separate from the MCP process and from TickHub's
admin console. It binds to loopback by default; a non-loopback bind is refused
unless ``STOCKSENATE_WEBUI_PASSWORD`` is set, so an unauthenticated console can
never be exposed to the network by accident.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from stock_senate.config.loader import env
from stock_senate.observability.logging import get_logger
from stock_senate.webui.auth import SecurityHeadersMiddleware, SessionMiddleware, auth_routes
from stock_senate.webui.pages import all_routes
from stock_senate.webui.state import WebRuntime

log = get_logger("webui")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
_WILDCARD_BINDS = {"0.0.0.0", "::", "*"}


class WebUIDependencyError(RuntimeError):
    """Starlette or uvicorn is missing; the message tells the operator what to do."""


def is_loopback(host: str) -> bool:
    value = str(host or "").strip().lower()
    if value in _LOOPBACK:
        return True
    return value.startswith("127.")


def allowed_hosts(extra: Iterable[str] = ()) -> list[str]:
    """Hosts accepted in the ``Host`` header; blocks DNS-rebinding to loopback.

    Extra names come from ``STOCKSENATE_WEBUI_ALLOWED_HOSTS`` (comma separated),
    so a LAN deployment can name the address it is actually reached on.
    """
    configured = [
        item.strip() for item in str(env("WEBUI_ALLOWED_HOSTS") or "").split(",") if item.strip()
    ]
    values = [*_DEFAULT_ALLOWED_HOSTS, *configured, *extra]
    return list(dict.fromkeys(value for value in values if value))


def create_app(runtime: WebRuntime | None = None, *, hosts: Sequence[str] | None = None) -> Any:
    """Build the ASGI app; ``runtime`` and ``hosts`` are injectable for tests."""
    try:
        from starlette.applications import Starlette
        from starlette.middleware.trustedhost import TrustedHostMiddleware
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise WebUIDependencyError(
            "WebUI 需要 starlette；请安装 `pip install -e .[mcp]`（starlette 随 mcp 一起安装）。"
        ) from exc

    service = runtime or WebRuntime()
    routes = [*auth_routes(), *all_routes()]
    app = Starlette(routes=routes)
    app.state.runtime = service
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(hosts or allowed_hosts()),
        www_redirect=False,
    )
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

    extra = [host] if host not in _WILDCARD_BINDS else []
    app = create_app(hosts=allowed_hosts(extra))
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
    if not is_loopback(host) and not env("WEBUI_ALLOWED_HOSTS"):
        print(
            "对外绑定提示：请把实际访问的主机名/IP 写入 "
            "STOCKSENATE_WEBUI_ALLOWED_HOSTS（逗号分隔）。",
            flush=True,
        )
    log.info(
        "webui listening",
        extra={"event": "webui.start", "host": host, "port": int(port)},
    )
    uvicorn.run(app, host=host, port=int(port), log_level="info", access_log=False)


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "WebUIDependencyError",
    "allowed_hosts",
    "create_app",
    "is_loopback",
    "run",
]
