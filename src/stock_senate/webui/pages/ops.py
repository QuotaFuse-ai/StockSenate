"""Ops page: read-only runtime facts and links to the TickHub admin console.

Nothing on this page writes: there is deliberately no POST route.
"""

from __future__ import annotations

import platform
import sys

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from stock_senate.queue import manager as queue_manager
from stock_senate.report import store
from stock_senate.version import __version__
from stock_senate.webui.pages.common import runtime
from stock_senate.webui.templates import Html, esc, page, table, tag

# DEC-003: one port per service; these are fixed contracts.
SERVICES = (
    ("WebUI", 8788, "本服务"),
    ("MCP HTTP", 8765, "stock-senate · stock_senate_*"),
    ("TickHub Admin", 8787, "外部服务，只提供跳转"),
    ("TickHub MCP", 8000, "外部服务"),
    ("Wire", 8790, "外部服务（HTTP）"),
    ("Wire", 8791, "外部服务（备用）"),
)


def _facts(service: object) -> Html:
    rows = [
        ["版本", esc(__version__)],
        ["进程启动", esc(service.started_at)],
        ["Python", esc(platform.python_version())],
        ["平台", esc(f"{platform.system()} {platform.release()}")],
        ["解释器", Html(f"<code>{esc(sys.executable)}</code>")],
        ["状态目录", Html(f"<code>{esc(service.home)}</code>")],
        ["配置目录", Html(f"<code>{esc(service.config_dir)}</code>")],
        ["配置指纹", Html(f"<code>{esc(service.config.get('config_hash'))}</code>")],
        ["登录模式", tag("已设口令", "ok") if service.auth.login_required else tag("本机免登录", "warn")],
    ]
    return table(["项", "值"], rows)


def _ports() -> Html:
    rows = [
        [esc(name), esc(port), esc(note)] for name, port, note in SERVICES
    ]
    link = (
        '<p><a href="http://127.0.0.1:8787" target="_blank" rel="noopener noreferrer">'
        "打开 TickHub Admin（127.0.0.1:8787）</a></p>"
    )
    return Html(str(table(["服务", "端口", "说明"], rows)) + link)


def _counts(service: object) -> Html:
    runs = store.list_runs(home=service.home, limit=None)
    queue = queue_manager.status(home=service.home)
    counts = queue.get("counts") or {}
    rows = [
        ["报告快照", esc(len(runs))],
        ["收藏", esc(len(service.favorites()))],
        ["队列任务", esc(queue.get("total"))],
        [
            "队列状态",
            esc(", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "—"),
        ],
        ["队列暂停", tag("是", "warn") if (queue.get("queue") or {}).get("paused") else "否"],
    ]
    return table(["项", "值"], rows)


async def view(request: Request) -> Response:
    service = runtime(request)
    body = Html(
        "<h2>运行时</h2>"
        + str(_facts(service))
        + "<h2>端口</h2>"
        + str(_ports())
        + "<h2>数据规模</h2>"
        + str(await run_in_threadpool(_counts, service))
        + '<p class="muted">本页只读；健康检查 <a href="/healthz">/healthz</a> 返回服务名与版本，'
        "不含任何密钥。运维写操作请在 TickHub Admin 完成。</p>"
    )
    return HTMLResponse(str(page(request, "运维", body, active="/ops")))


def routes() -> list[Route]:
    return [Route("/ops", view, methods=["GET"])]


__all__ = ["routes"]
