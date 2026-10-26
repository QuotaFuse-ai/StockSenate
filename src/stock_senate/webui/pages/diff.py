"""Field-level report diff between two dates for one code."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from stock_senate.report import store
from stock_senate.webui.pages.common import runtime
from stock_senate.webui.templates import Html, esc, json_block, page, table, tag

_FORM = """
<div class="panel">
<form method="get" action="/diff">
  <div class="row">
    <div><label for="code">标的代码</label>
      <input id="code" type="text" name="code" value="{code}" placeholder="600519" required></div>
    <div><label for="from">起始日期</label>
      <input id="from" type="date" name="from" value="{start}" required></div>
    <div><label for="to">结束日期</label>
      <input id="to" type="date" name="to" value="{end}" required></div>
    <div><button type="submit">对比</button></div>
  </div>
</form>
</div>"""


def _render_diff(payload: dict) -> Html:
    from_rows = [
        [esc(item.get("field")), esc(item.get("from")), esc(item.get("to")), tag("变化", "warn")]
        for item in payload.get("fields") or []
    ]
    analyst_rows = [
        [esc(item.get("role")), esc(item.get("from")), esc(item.get("to"))]
        for item in payload.get("analysts") or []
    ]
    risks = payload.get("risks") or {}
    risk_rows = []
    for item in risks.get("added") or []:
        risk_rows.append([tag("新增", "warn"), esc(item.get("field")), esc(item.get("value"))])
    for item in risks.get("removed") or []:
        risk_rows.append([tag("移除", "bad"), esc(item.get("field")), esc(item.get("value"))])
    for item in risks.get("changed") or []:
        risk_rows.append(
            [
                tag("变化", ""),
                esc(item.get("field")),
                f"{esc(item.get('from'))} → {esc(item.get('to'))}",
            ]
        )
    conclusion = payload.get("conclusion") or {}
    conclusion_tag = tag(
        "是" if conclusion.get("changed") else "否",
        "warn" if conclusion.get("changed") else "ok",
    )
    head = (
        f"<p>对比 <code>{esc(payload.get('code'))}</code>："
        f"{esc((payload.get('from') or {}).get('date'))}"
        f"（{esc((payload.get('from') or {}).get('rating'))}） → "
        f"{esc((payload.get('to') or {}).get('date'))}"
        f"（{esc((payload.get('to') or {}).get('rating'))}）</p>"
        f"<p>变化字段 {esc(payload.get('changed_count'))} 个；结论是否变化："
        f"{conclusion_tag}</p>"
    )
    return Html(
        head
        + "<h2>字段级差异</h2>"
        + str(table(["字段", "起始", "结束", ""], from_rows, empty="没有字段变化"))
        + "<h2>分析师结论</h2>"
        + str(table(["角色", "起始", "结束"], analyst_rows, empty="没有分析师变化"))
        + "<h2>风险项</h2>"
        + str(table(["变更", "字段", "内容"], risk_rows, empty="风险项没有变化"))
        + "<h2>原始负载</h2>"
        + str(json_block(payload))
    )


async def view(request: Request) -> Response:
    service = runtime(request)
    code = request.query_params.get("code") or ""
    start = request.query_params.get("from") or ""
    end = request.query_params.get("to") or ""
    form = Html(_FORM.format(code=esc(code), start=esc(start), end=esc(end)))

    if not (code and start and end):
        return HTMLResponse(str(page(request, "报告 Diff", form, active="/diff")))

    try:
        payload = store.diff(code=code, from_date=start, to_date=end, home=service.home)
    except store.ReportNotFound as exc:
        return HTMLResponse(
            str(page(request, "报告 Diff", form, active="/diff", error=str(exc))),
            status_code=404,
        )
    except ValueError as exc:
        return HTMLResponse(
            str(page(request, "报告 Diff", form, active="/diff", error=str(exc))),
            status_code=400,
        )
    return HTMLResponse(
        str(
            page(request, "报告 Diff", Html(str(form) + str(_render_diff(payload))), active="/diff")
        )
    )


async def api(request: Request) -> Response:
    service = runtime(request)
    code = request.query_params.get("code")
    start = request.query_params.get("from")
    end = request.query_params.get("to")
    if not (code and start and end):
        return JSONResponse({"ok": False, "error": "code/from/to required"}, status_code=400)
    try:
        payload = store.diff(code=code, from_date=start, to_date=end, home=service.home)
    except store.ReportNotFound as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **payload})


def routes() -> list[Route]:
    return [
        Route("/diff", view, methods=["GET"]),
        Route("/api/diff", api, methods=["GET"]),
    ]


__all__ = ["routes"]
