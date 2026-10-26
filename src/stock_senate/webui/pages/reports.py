"""Report pages: snapshot list, detail view and machine-readable export."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from stock_senate.report import store
from stock_senate.report.render import render_summary
from stock_senate.webui.pages.common import runtime
from stock_senate.webui.templates import Html, esc, json_block, page, pre, table, tag


def _rating_tag(rating: object) -> Html:
    value = str(rating or "—").lower()
    kind = {"buy": "ok", "sell": "bad", "hold": "warn"}.get(value, "")
    return tag(value or "—", kind)


async def index(request: Request) -> Response:
    service = runtime(request)
    code = request.query_params.get("code") or None
    rows = store.list_runs(code=code, home=service.home, limit=200)
    body_rows = []
    for row in rows:
        body_rows.append(
            [
                Html(f'<a href="/reports/{esc(row.get("run_id"))}">{esc(row.get("run_id"))}</a>'),
                esc(row.get("code")),
                esc(row.get("date")),
                _rating_tag(row.get("rating")),
                esc(row.get("data", {}).get("status") if isinstance(row.get("data"), dict) else ""),
                tag("partial", "warn") if row.get("partial") else Html("—"),
            ]
        )
    body = Html(
        f'<form method="get" action="/reports" class="panel"><div class="row">'
        f'<div><label for="code">按标的过滤</label>'
        f'<input id="code" type="text" name="code" value="{esc(code or "")}" placeholder="600519"></div>'
        f'<div><button type="submit">筛选</button></div></div></form>'
        + str(table(["运行", "标的", "日期", "评级", "数据", "标记"], body_rows, empty="还没有报告快照"))
    )
    return HTMLResponse(str(page(request, "报告", body, active="/reports")))


async def detail(request: Request) -> Response:
    service = runtime(request)
    run_id = request.path_params["run_id"]
    try:
        state = store.load(run_id, home=service.home)
        markdown = store.render(run_id, home=service.home)
    except store.ReportNotFound:
        return HTMLResponse(
            str(page(request, "报告", Html("<p>找不到这份报告。</p>"), active="/reports", error="报告不存在")),
            status_code=404,
        )
    except ValueError:
        return HTMLResponse(
            str(page(request, "报告", Html("<p>运行 ID 不合法。</p>"), active="/reports", error="运行 ID 不合法")),
            status_code=400,
        )

    summary = render_summary(state)
    decision = state.get("decision") or {}
    meta = {
        "运行 ID": esc(summary.get("run_id")),
        "标的 / 日期": f'{esc(summary.get("code"))} · {esc(summary.get("date"))}',
        "评级": _rating_tag(summary.get("rating")),
        "持有期": esc(summary.get("horizon")),
        "置信度": esc(summary.get("confidence")),
        "数据状态": tag(
            summary.get("data", {}).get("status") if isinstance(summary.get("data"), dict) else "—",
            "bad",
        ),
        "partial": tag("是", "warn") if summary.get("partial") else "否",
        "质量分": esc(summary.get("quality_score")),
        "trace_id": esc(summary.get("trace_id")),
        "生成时间": esc(summary.get("generated_at")),
    }
    reasons = "".join(f"<li>{esc(item)}</li>" for item in decision.get("reasons") or [])
    risks = "".join(f"<li>{esc(item)}</li>" for item in decision.get("risks") or [])
    body = f"""
<div class="panel">
  <dl class="kv">
    {''.join(f'<dt>{key}</dt><dd>{value}</dd>' for key, value in meta.items())}
  </dl>
</div>
<div class="grid">
  <div class="panel"><h2 style="margin-top:0">研究理由</h2><ul>{reasons or '<li class="muted">—</li>'}</ul></div>
  <div class="panel"><h2 style="margin-top:0">风险提示</h2><ul>{risks or '<li class="muted">—</li>'}</ul></div>
</div>
<h2>完整报告（Markdown）</h2>
{pre(markdown)}
<h2>数据附录</h2>
{json_block(state.get("data") or {})}
<p class="muted">
  <a href="/reports/{esc(run_id)}/markdown">下载 Markdown</a> ·
  <a href="/reports/{esc(run_id)}/json">下载 JSON</a> ·
  <a href="/history">在历史中收藏</a>
</p>"""
    return HTMLResponse(str(page(request, f"报告 · {run_id}", Html(body), active="/reports")))


async def markdown(request: Request) -> Response:
    service = runtime(request)
    run_id = request.path_params["run_id"]
    try:
        text = store.render(run_id, home=service.home)
    except (store.ReportNotFound, ValueError):
        return Response("报告不存在", status_code=404, media_type="text/plain; charset=utf-8")
    return Response(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'},
    )


async def api(request: Request) -> Response:
    service = runtime(request)
    run_id = request.path_params["run_id"]
    try:
        state = store.load(run_id, home=service.home)
    except (store.ReportNotFound, ValueError):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True, "state": json.loads(json.dumps(state, default=str))})


def routes() -> list[Route]:
    return [
        Route("/reports", index, methods=["GET"]),
        Route("/reports/{run_id}", detail, methods=["GET"]),
        Route("/reports/{run_id}/markdown", markdown, methods=["GET"]),
        Route("/reports/{run_id}/json", api, methods=["GET"]),
        Route("/api/reports/{run_id}", api, methods=["GET"]),
    ]


__all__ = ["routes"]
