"""Analysis page: submit one code/date and hand off to the progress view."""

from __future__ import annotations

from datetime import datetime

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from stock_senate.webui.pages.common import as_bool, checked_form, redirect, runtime, split_csv
from stock_senate.webui.state import today
from stock_senate.webui.templates import Html, csrf_input, esc, page, table, tag

_FORM = """
<div class="panel">
<form method="post" action="/analysis">
  {csrf}
  <div class="row">
    <div><label for="code">标的代码</label>
      <input id="code" type="text" name="code" value="{code}" placeholder="600519" required></div>
    <div><label for="date">分析日期</label>
      <input id="date" type="date" name="date" value="{date}" required></div>
    <div class="check"><input id="hitl" type="checkbox" name="hitl" value="1" {hitl}>
      <label for="hitl" style="margin:0">HITL 检查点</label></div>
    <div><button type="submit">开始分析</button></div>
  </div>
  <div class="row" style="margin-top:12px">
    <div><label for="enable">临时启用（逗号分隔，可留空）</label>
      <input id="enable" type="text" name="enable" placeholder="custom_macro, custom_esg"></div>
    <div><label for="disable">临时停用（逗号分隔，可留空）</label>
      <input id="disable" type="text" name="disable" placeholder="social"></div>
    <div><label for="weight">权重（如 bull=0.6, bear=0.4）</label>
      <input id="weight" type="text" name="weight" placeholder="bull=0.6"></div>
  </div>
  <p class="muted">勾选 HITL 后，分析会在检查点暂停并等待处理，
     请到 <a href="/reviews">审批</a> 页同意或驳回；超时按配置自动放行并在报告中留痕。</p>
  <p class="muted" style="margin-bottom:0">输出只含研究结论与风险提示，
     不含可执行价位或交易指令。</p>
</form>
</div>"""


def _jobs_table(request: Request) -> Html:
    rows = []
    for job in runtime(request).jobs(limit=10):
        status = {
            "done": tag("完成", "ok"),
            "failed": tag("失败", "bad"),
            "running": tag("进行中", "warn"),
            "queued": tag("排队", ""),
        }.get(job.status, tag(job.status))
        link = Html(f'<a href="/progress/{esc(job.job_id)}">{esc(job.job_id)}</a>')
        report = (
            Html(f'<a href="/reports/{esc(job.run_id)}">{esc(job.run_id)}</a>')
            if job.run_id
            else Html("—")
        )
        rows.append([link, esc(job.code), esc(job.date), status, esc(job.stage), report])
    return table(
        ["任务", "标的", "日期", "状态", "阶段", "报告"], rows, empty="本次进程内还没有分析任务"
    )


async def index(request: Request) -> Response:
    notice = request.query_params.get("notice")
    body = Html(
        _FORM.format(
            csrf=csrf_input(request),
            code="600519",
            date=esc(today()),
            hitl="checked",
        )
    )
    body = Html(str(body) + "<h2>最近任务</h2>" + str(_jobs_table(request)))
    return HTMLResponse(str(page(request, "分析", body, active="/", notice=notice)))


async def submit(request: Request) -> Response:
    form, failure = await checked_form(request)
    if failure is not None:
        return failure
    code = str(form.get("code") or "").strip()
    day = str(form.get("date") or "").strip() or today()
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return _form_error(request, code, day, "日期格式应为 YYYY-MM-DD。")
    if parsed.date().isoformat() != day:
        return _form_error(request, code, day, "日期不合法。")

    service = runtime(request)
    try:
        job = service.start_analysis(
            code,
            day,
            hitl=as_bool(form.get("hitl"), default=False),
            enable=split_csv(form.get("enable")),
            disable=split_csv(form.get("disable")),
            weights=split_csv(form.get("weight")),
        )
    except ValueError as exc:
        return _form_error(request, code, day, str(exc))
    return redirect(f"/progress/{job.job_id}", notice=f"已启动 {job.code} {job.date} 的分析")


def _form_error(request: Request, code: str, day: str, message: str) -> HTMLResponse:
    body = Html(
        _FORM.format(
            csrf=csrf_input(request),
            code=esc(code),
            date=esc(day),
            hitl="checked",
        )
    )
    return HTMLResponse(
        str(page(request, "分析", body, active="/", error=message)),
        status_code=400,
    )


def routes() -> list[Route]:
    return [
        Route("/", index, methods=["GET"]),
        Route("/analysis", submit, methods=["POST"]),
    ]


__all__ = ["routes"]
