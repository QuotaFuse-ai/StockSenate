"""Progress page: per-node status streamed from the graph callback."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from stock_senate.webui.pages.common import runtime
from stock_senate.webui.templates import Html, esc, page, table, tag


def _status_tag(status: str) -> Html:
    kind = {"done": "ok", "failed": "bad", "running": "warn"}.get(status, "")
    return tag(status, kind)


async def index(request: Request) -> Response:
    rows = []
    for job in runtime(request).jobs(limit=25):
        rows.append(
            [
                Html(f'<a href="/progress/{esc(job.job_id)}">{esc(job.job_id)}</a>'),
                esc(job.code),
                esc(job.date),
                _status_tag(job.status),
                esc(job.stage),
                esc(job.error or ""),
            ]
        )
    body = table(["任务", "标的", "日期", "状态", "阶段", "错误"], rows, empty="还没有任务")
    return HTMLResponse(str(page(request, "进度", body, active="/")))


async def detail(request: Request) -> Response:
    job_id = request.path_params["job_id"]
    job = runtime(request).job(job_id)
    if job is None:
        return HTMLResponse(
            str(page(request, "进度", Html("<p>任务不存在或已过期。</p>"), active="/", error="任务不存在")),
            status_code=404,
        )
    snapshot = job.snapshot()
    job_literal = json.dumps(job_id)
    report_cell = (
        f'<a href="/reports/{esc(snapshot["run_id"])}">{esc(snapshot["run_id"])}</a>'
        if snapshot["run_id"]
        else "—"
    )
    body = f"""
<div class="panel">
  <dl class="kv">
    <dt>任务</dt><dd>{esc(snapshot["job_id"])}</dd>
    <dt>标的 / 日期</dt><dd>{esc(snapshot["code"])} · {esc(snapshot["date"])}</dd>
    <dt>状态</dt><dd id="status">{esc(snapshot["status"])}</dd>
    <dt>当前阶段</dt><dd id="stage">{esc(snapshot["stage"])}</dd>
    <dt>报告</dt><dd id="report">{report_cell}</dd>
  </dl>
  <p id="error" class="notice error" style="display:none"></p>
  <div class="progress" style="margin-top:14px"><span id="bar"></span></div>
  <p class="muted" id="summary"></p>
</div>
<h2>节点事件</h2>
<table><thead><tr><th>时间</th><th>阶段</th><th>状态</th><th>详情</th></tr></thead>
<tbody id="events"></tbody></table>
<script>
const JOB = {job_literal};
function cell(value) {{ const td = document.createElement('td'); td.textContent = value; return td; }}
function tick() {{
  fetch('/api/progress/' + encodeURIComponent(JOB), {{credentials: 'same-origin'}})
    .then(function (response) {{ return response.ok ? response.json() : null; }})
    .then(function (data) {{
      if (!data) return;
      document.getElementById('bar').style.width = data.percent + '%';
      document.getElementById('status').textContent = data.status;
      document.getElementById('stage').textContent = data.stage;
      document.getElementById('summary').textContent =
        '进度 ' + data.percent + '% · 事件 ' + data.events.length + ' 条' +
        (data.rating ? ' · 结论 ' + data.rating : '');
      const error = document.getElementById('error');
      if (data.error) {{ error.textContent = data.error; error.style.display = 'block'; }}
      const body = document.getElementById('events');
      body.textContent = '';
      data.events.forEach(function (event) {{
        const row = document.createElement('tr');
        row.appendChild(cell(event.ts || ''));
        row.appendChild(cell(event.stage || ''));
        row.appendChild(cell(event.status || ''));
        const detail = Object.assign({{}}, event);
        delete detail.ts; delete detail.stage; delete detail.status;
        row.appendChild(cell(JSON.stringify(detail)));
        body.appendChild(row);
      }});
    }})
    .catch(function () {{ /* a transient poll failure is not a run failure */ }});
}}
tick();
setInterval(tick, 1000);
</script>"""
    return HTMLResponse(str(page(request, f"进度 · {job_id}", Html(body), active="/")))


async def api(request: Request) -> Response:
    job = runtime(request).job(request.path_params["job_id"])
    if job is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True, **job.snapshot()})


def routes() -> list[Route]:
    return [
        Route("/progress", index, methods=["GET"]),
        Route("/progress/{job_id}", detail, methods=["GET"]),
        Route("/api/progress/{job_id}", api, methods=["GET"]),
    ]


__all__ = ["routes"]
