"""Backtest page: replay a window and show the persisted metrics."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from stock_senate.webui.pages.common import checked_form, runtime, split_csv
from stock_senate.webui.state import today
from stock_senate.webui.templates import Html, csrf_input, esc, page, table, tag

_FORM = """
<div class="panel">
<form method="post" action="/backtest">
  {csrf}
  <div class="row">
    <div><label for="codes">标的（逗号分隔）</label>
      <input id="codes" type="text" name="codes" value="{codes}"
             placeholder="600519,000001" required></div>
    <div><label for="start">开始</label>
      <input id="start" type="date" name="start" value="{start}" required></div>
    <div><label for="end">结束</label>
      <input id="end" type="date" name="end" value="{end}" required></div>
    <div><label for="window">持有交易日</label>
      <input id="window" type="number" name="window" min="1" value="{window}"></div>
    <div><label for="slippage">滑点（比例）</label>
      <input id="slippage" type="text" name="slippage" value="{slippage}" placeholder="0.001"></div>
    <div><label for="fee">手续费（单边）</label>
      <input id="fee" type="text" name="fee" value="{fee}" placeholder="0.0003"></div>
    <div><button type="submit">运行回测</button></div>
  </div>
  <p class="muted" style="margin-bottom:0">回测是历史推演，不是 future 收益承诺；
     数据缺失只会降级并标 partial。</p>
</form>
</div>"""


def _form(request: Request, values: dict[str, str]) -> Html:
    return Html(_FORM.format(csrf=csrf_input(request), **values))


def _result(payload: dict) -> Html:
    metrics = payload.get("metrics") or {}
    metric_rows = [[esc(key), esc(_fmt(value))] for key, value in metrics.items()]
    per_code = []
    for code, summary in (payload.get("per_code") or {}).items():
        per_code.append(
            [
                esc(code),
                esc(_fmt(summary.get("total_return"))),
                esc(_fmt(summary.get("win_rate"))),
                esc(summary.get("trades", "—")),
                esc(_fmt(summary.get("max_drawdown"))),
            ]
        )
    head = (
        f"<p>{esc(payload.get('start'))} → {esc(payload.get('end'))} · "
        f"{esc(len(payload.get('codes') or []))} 个标的 · "
        f"持有 {esc(payload.get('window'))} 个交易日</p>"
        f"<p>数据状态 {tag((payload.get('data') or {}).get('status', '—'), 'bad')} "
        f"{'· partial' if payload.get('partial') else ''}</p>"
    )
    missing = (payload.get("data") or {}).get("missing_codes") or []
    missing_html = (
        f'<p class="notice warn">行情缺失：{esc(", ".join(missing))}</p>' if missing else ""
    )
    artifacts = payload.get("artifacts") or {}
    return Html(
        head
        + missing_html
        + "<h2>总体指标</h2>"
        + str(table(["指标", "值"], metric_rows, empty="没有指标"))
        + "<h2>分标的</h2>"
        + str(table(["代码", "收益", "胜率", "交易数", "最大回撤"], per_code, empty="没有结果"))
        + "<h2>产物</h2>"
        + str(table(["项", "路径"], [[esc(key), esc(value)] for key, value in artifacts.items()]))
        + '<p class="muted">回测结果保存在状态目录的 <code>backtests/</code> 下，'
        + "可用 sqlite 复查。</p>"
    )


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


async def view(request: Request) -> Response:
    values = {
        "codes": "600519",
        "start": today(),
        "end": today(),
        "window": "5",
        "slippage": "0.001",
        "fee": "0.0003",
    }
    return HTMLResponse(str(page(request, "回测", _form(request, values), active="/backtest")))


async def run(request: Request) -> Response:
    form, failure = await checked_form(request)
    if failure is not None:
        return failure
    values = {
        "codes": str(form.get("codes") or "").strip(),
        "start": str(form.get("start") or "").strip(),
        "end": str(form.get("end") or "").strip(),
        "window": str(form.get("window") or "5").strip(),
        "slippage": str(form.get("slippage") or "0").strip(),
        "fee": str(form.get("fee") or "0").strip(),
    }
    codes = split_csv(values["codes"])
    if not codes:
        return _error(request, values, "至少需要一个标的代码。")
    try:
        window = int(values["window"])
        slippage = float(values["slippage"])
        fee = float(values["fee"])
    except ValueError:
        return _error(request, values, "持有天数、滑点、手续费必须是数字。")

    from stock_senate.backtest import engine

    service = runtime(request)
    try:
        payload = await run_in_threadpool(
            engine.run,
            codes=codes,
            start=values["start"],
            end=values["end"],
            window=window,
            slippage=slippage,
            fee=fee,
            config=service.config,
            home=service.home,
        )
    except ValueError as exc:
        return _error(request, values, str(exc))
    body = Html(str(_form(request, values)) + str(_result(payload)))
    notice = "回测完成" + ("（partial：数据不完整）" if payload.get("partial") else "")
    return HTMLResponse(str(page(request, "回测", body, active="/backtest", notice=notice)))


def _error(request: Request, values: dict[str, str], message: str) -> HTMLResponse:
    return HTMLResponse(
        str(page(request, "回测", _form(request, values), active="/backtest", error=message)),
        status_code=400,
    )


def routes() -> list[Route]:
    return [
        Route("/backtest", view, methods=["GET"]),
        Route("/backtest", run, methods=["POST"]),
    ]


__all__ = ["routes"]
