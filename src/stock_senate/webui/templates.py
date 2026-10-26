"""Server-rendered HTML helpers for the standalone WebUI.

There is deliberately no template engine dependency. Every dynamic value is
escaped unless it is explicitly wrapped in :class:`Html`, so a model-written
report or a user-supplied stock code can never inject markup.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

DISCLAIMER = (
    "StockSenate 仅用于投研研究与回测推演，不构成任何投资建议，"
    "也不输出建仓价、止损位、目标价、仓位等可执行价位或交易指令。"
)


class Html(str):
    """A fragment that is already safe to embed."""


NAV: tuple[tuple[str, str], ...] = (
    ("/", "分析"),
    ("/reports", "报告"),
    ("/diff", "Diff"),
    ("/backtest", "回测"),
    ("/alerts", "告警"),
    ("/queue", "队列"),
    ("/settings", "设置"),
    ("/history", "历史"),
    ("/ops", "运维"),
)

_STYLE = """
:root {
  --bg: #f6f7f9; --panel: #ffffff; --ink: #1f2328; --muted: #6b7280;
  --line: #d8dee4; --accent: #1d4ed8; --ok: #15803d; --warn: #b45309; --bad: #b91c1c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.6 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
header { background: var(--panel); border-bottom: 1px solid var(--line); padding: 10px 22px;
  display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
header .brand { font-weight: 700; letter-spacing: .3px; }
header nav { display: flex; gap: 4px; flex-wrap: wrap; }
header nav a { color: var(--muted); text-decoration: none; padding: 4px 10px; border-radius: 6px; }
header nav a:hover { background: #eef2ff; color: var(--accent); }
header nav a.active { background: #e0e7ff; color: var(--accent); font-weight: 600; }
header .right { margin-left: auto; display: flex; gap: 10px; align-items: center;
  color: var(--muted); }
main { max-width: 1180px; margin: 22px auto 60px; padding: 0 18px; }
h1 { font-size: 20px; margin: 0 0 16px; }
h2 { font-size: 16px; margin: 22px 0 10px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px 18px; margin-bottom: 18px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 18px; }
.notice { border-radius: 8px; padding: 10px 14px; margin-bottom: 16px; }
.notice.ok { background: #ecfdf5; color: var(--ok); border: 1px solid #a7f3d0; }
.notice.error { background: #fef2f2; color: var(--bad); border: 1px solid #fecaca; }
.notice.warn { background: #fffbeb; color: var(--warn); border: 1px solid #fde68a; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
  letter-spacing: .04em; }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
form.inline { display: inline; }
label { display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }
input[type=text], input[type=date], input[type=password], input[type=number], select,
textarea {
  width: 100%; padding: 7px 9px; border: 1px solid var(--line); border-radius: 7px;
  font: inherit; background: #fff; color: var(--ink); }
textarea { min-height: 96px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12px; }
.row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px;
  align-items: end; }
.check { display: flex; align-items: center; gap: 6px; color: var(--ink); font-size: 13px; }
.check input { width: auto; }
button { padding: 8px 16px; border-radius: 7px; border: 1px solid var(--accent);
  background: var(--accent); color: #fff; font: inherit; cursor: pointer; }
button.ghost { background: #fff; color: var(--accent); }
button.danger { background: #fff; color: var(--bad); border-color: var(--bad); }
button.small { padding: 3px 9px; font-size: 12px; }
.muted { color: var(--muted); }
.tag { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px;
  background: #eef2ff; color: var(--accent); }
.tag.ok { background: #ecfdf5; color: var(--ok); }
.tag.warn { background: #fffbeb; color: var(--warn); }
.tag.bad { background: #fef2f2; color: var(--bad); }
pre { background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 8px; overflow: auto;
  font: 12px/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap; }
details summary { cursor: pointer; color: var(--accent); }
footer { color: var(--muted); font-size: 12px; text-align: center; padding: 18px; }
.banner { background: #fffbeb; border-bottom: 1px solid #fde68a; color: var(--warn);
  padding: 6px 22px; font-size: 12px; }
.progress { height: 10px; background: #e5e7eb; border-radius: 999px; overflow: hidden; }
.progress > span { display: block; height: 100%; background: var(--accent); width: 0;
  transition: width .3s; }
dl.kv { display: grid; grid-template-columns: 180px 1fr; gap: 6px 14px; margin: 0; }
dl.kv dt { color: var(--muted); }
dl.kv dd { margin: 0; word-break: break-word; }
@media (prefers-color-scheme: dark) {
  body.auto-dark { --bg: #0b1020; --panel: #121a2e; --ink: #e5e7eb; --muted: #94a3b8;
    --line: #24304a; }
}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe(value: Any) -> str:
    return str(value) if isinstance(value, Html) else esc(value)


def tag(value: Any, kind: str = "") -> Html:
    cls = f"tag {kind}".strip()
    return Html(f'<span class="{cls}">{esc(value)}</span>')


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    empty: str = "暂无数据",
    numeric: Iterable[int] = (),
) -> Html:
    numeric_cols = set(numeric)
    body = list(rows)
    if not body:
        return Html(f'<p class="muted">{esc(empty)}</p>')
    head = "".join(
        f'<th class="{"num" if index in numeric_cols else ""}">{esc(name)}</th>'
        for index, name in enumerate(headers)
    )
    cells = []
    for row in body:
        tds = "".join(
            f'<td class="{"num" if index in numeric_cols else ""}">{safe(value)}</td>'
            for index, value in enumerate(row)
        )
        cells.append(f"<tr>{tds}</tr>")
    return Html(f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(cells)}</tbody></table>")


def kv_table(data: Mapping[str, Any]) -> Html:
    items = "".join(f"<dt>{esc(key)}</dt><dd>{safe(value)}</dd>" for key, value in data.items())
    return Html(f'<dl class="kv">{items}</dl>')


def pre(text: Any) -> Html:
    return Html(f"<pre>{esc(text)}</pre>")


def json_block(payload: Any) -> Html:
    return pre(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def page(
    request: Any,
    title: str,
    body: str | Html,
    *,
    active: str = "",
    notice: str | None = None,
    error: str | None = None,
    extra_head: str = "",
    chrome: bool = True,
) -> Html:
    """Wrap ``body`` in the shared shell using the signed-in session."""
    csrf = getattr(request.state, "csrf", "")
    login_required = getattr(request.state, "login_required", True)
    messages = ""
    if notice:
        messages += f'<div class="notice ok">{esc(notice)}</div>'
    if error:
        messages += f'<div class="notice error">{esc(error)}</div>'
    if not chrome:
        return Html(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc(title)} · StockSenate</title><style>{_STYLE}</style></head>"
            f'<body><main style="max-width:520px;margin-top:12vh">{messages}{safe(body)}</main>'
            f"<footer>{esc(DISCLAIMER)}</footer></body></html>"
        )
    links = "".join(
        f'<a class="{"active" if href == active else ""}" href="{href}">{esc(label)}</a>'
        for href, label in NAV
    )
    logout = (
        f'<form class="inline" method="post" action="/logout">'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
        f'<button class="ghost small" type="submit">退出</button></form>'
        if login_required
        else ""
    )
    banner = (
        ""
        if login_required
        else '<div class="banner">未设置 STOCKSENATE_WEBUI_PASSWORD：当前为本机免登录模式，'
        "服务只允许绑定回环地址。设置口令后需重新登录。</div>"
    )
    return Html(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} · StockSenate</title><style>{_STYLE}</style>{extra_head}</head>"
        f'<body><header><span class="brand">StockSenate</span><nav>{links}</nav>'
        f'<span class="right">{logout}</span></header>{banner}<main>'
        f"<h1>{esc(title)}</h1>{messages}{safe(body)}</main>"
        f"<footer>{esc(DISCLAIMER)}</footer></body></html>"
    )


def csrf_input(request: Any) -> Html:
    return Html(
        f'<input type="hidden" name="csrf" value="{esc(getattr(request.state, "csrf", ""))}">'
    )


__all__ = [
    "DISCLAIMER",
    "Html",
    "csrf_input",
    "esc",
    "json_block",
    "kv_table",
    "page",
    "pre",
    "safe",
    "table",
    "tag",
]
