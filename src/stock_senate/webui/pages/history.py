"""History page: filter past runs and keep favourites."""

from __future__ import annotations

from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from stock_senate.report import store
from stock_senate.webui.pages.common import checked_form, runtime
from stock_senate.webui.templates import Html, csrf_input, esc, page, table, tag

_RATINGS = ("buy", "hold", "sell")


def _rating_tag(rating: object) -> Html:
    value = str(rating or "—").lower()
    return tag(value or "—", {"buy": "ok", "sell": "bad", "hold": "warn"}.get(value, ""))


def _filters(request: Request, *, code: str, day: str, rating: str, only_favorites: bool) -> Html:
    options = ['<option value="">全部</option>']
    for value in _RATINGS:
        selected = " selected" if value == rating else ""
        options.append(f'<option value="{value}"{selected}>{value}</option>')
    checked = " checked" if only_favorites else ""
    return Html(
        f'<form method="get" action="/history" class="panel"><div class="row">'
        f'<div><label for="code">标的</label>'
        f'<input id="code" type="text" name="code" value="{esc(code)}" placeholder="600519"></div>'
        f'<div><label for="date">日期</label>'
        f'<input id="date" type="date" name="date" value="{esc(day)}"></div>'
        f'<div><label for="rating">评级</label>'
        f'<select id="rating" name="rating">{"".join(options)}</select></div>'
        f'<div class="check"><input id="fav" type="checkbox" name="favorites" value="1"{checked}>'
        f'<label for="fav" style="margin:0">只看收藏</label></div>'
        f'<div><button type="submit">筛选</button></div>'
        "</div></form>"
    )


def _rows(request: Request, rows: list[dict], favorites: set[str], next_path: str) -> Html:
    csrf = str(getattr(request.state, "csrf", ""))
    body = []
    for row in rows:
        run_id = str(row.get("run_id"))
        marked = run_id in favorites
        toggle = (
            f'<form class="inline" method="post" action="/history/favorite">'
            f'<input type="hidden" name="run_id" value="{esc(run_id)}">'
            f'<input type="hidden" name="next" value="{esc(next_path)}">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<button class="small ghost" type="submit">{"取消收藏" if marked else "收藏"}</button>'
            "</form>"
        )
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        body.append(
            [
                Html("★" if marked else "☆"),
                Html(f'<a href="/reports/{esc(run_id)}">{esc(run_id)}</a>'),
                esc(row.get("code")),
                esc(row.get("date")),
                _rating_tag(row.get("rating")),
                esc(data.get("status") or "—"),
                tag("partial", "warn") if row.get("partial") else Html("—"),
                esc(row.get("quality_score")),
                Html(toggle),
            ]
        )
    return table(
        ["收藏", "运行", "标的", "日期", "评级", "数据", "标记", "质量分", "操作"],
        body,
        empty="没有符合条件的报告",
    )


def _next_path(request: Request, code: str, day: str, rating: str, only_favorites: bool) -> str:
    values = []
    if code:
        values.append(f"code={code}")
    if day:
        values.append(f"date={day}")
    if rating:
        values.append(f"rating={rating}")
    if only_favorites:
        values.append("favorites=1")
    return "/history" + (("?" + "&".join(values)) if values else "")


async def view(request: Request) -> Response:
    service = runtime(request)
    code = (request.query_params.get("code") or "").strip()
    day = (request.query_params.get("date") or "").strip()
    rating = (request.query_params.get("rating") or "").strip()
    only_favorites = request.query_params.get("favorites") in {"1", "true", "on"}

    favorites = set(service.favorites())
    rows = await run_in_threadpool(store.list_runs, code=code or None, home=service.home, limit=500)
    if day:
        rows = [row for row in rows if str(row.get("date")) == day]
    if rating:
        rows = [row for row in rows if str(row.get("rating") or "").lower() == rating]
    if only_favorites:
        rows = [row for row in rows if str(row.get("run_id")) in favorites]

    body = Html(
        str(_filters(request, code=code, day=day, rating=rating, only_favorites=only_favorites))
        + f'<p class="muted">共 {len(rows)} 份报告，收藏 {len(favorites)} 份。</p>'
        + str(_rows(request, rows, favorites, _next_path(request, code, day, rating, only_favorites)))
    )
    return HTMLResponse(
        str(page(request, "历史", body, active="/history", notice=request.query_params.get("notice")))
    )


async def favorite(request: Request) -> Response:
    form, failure = await checked_form(request)
    if failure is not None:
        return failure
    service = runtime(request)
    run_id = str(form.get("run_id") or "").strip()
    if not run_id:
        return HTMLResponse(
            str(page(request, "历史", Html("<p>缺少运行 ID。</p>"), active="/history", error="缺少运行 ID")),
            status_code=400,
        )
    marked = await run_in_threadpool(service.toggle_favorite, run_id)
    target = str(form.get("next") or "/history")
    if not target.startswith("/history"):
        target = "/history"
    separator = "&" if "?" in target else "?"
    notice = f"已收藏 {run_id}" if marked else f"已取消收藏 {run_id}"
    return RedirectResponse(f"{target}{separator}notice={quote(notice)}", status_code=303)


def routes() -> list[Route]:
    return [
        Route("/history", view, methods=["GET"]),
        Route("/history/favorite", favorite, methods=["POST"]),
    ]


__all__ = ["routes"]
