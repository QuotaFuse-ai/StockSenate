"""Settings page: read-only config view, role switches and the audit trail."""

from __future__ import annotations

import json

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from stock_senate.config import edit as config_edit
from stock_senate.config import schema as config_schema
from stock_senate.roles.registry import RoleError, RoleRegistry
from stock_senate.webui.auth import audit_write, recent_writes
from stock_senate.webui.pages.common import checked_form, redirect, runtime
from stock_senate.webui.templates import Html, esc, json_block, page, table, tag

_BOOKKEEPING = {"ts", "event", "actor", "action"}


def _validation(report: dict) -> Html:
    if report.get("ok"):
        return Html('<p class="notice ok">配置校验通过。</p>')
    errors = report.get("errors") or []
    rows = [
        [esc(item.get("file")), esc(item.get("key")), esc(item.get("message"))]
        for item in errors
    ]
    return Html(
        '<div class="notice error">配置校验失败</div>'
        + str(table(["文件", "键", "问题"], rows))
    )


def _warnings(report: dict) -> Html:
    warnings = report.get("warnings") or []
    if not warnings:
        return Html("")
    rows = [
        [esc(item.get("file")), esc(item.get("key")), esc(item.get("message"))]
        for item in warnings
    ]
    return Html("<h2>警告</h2>" + str(table(["文件", "键", "提示"], rows)))


def _roles_table(request: Request, roles: list[dict]) -> Html:
    csrf = str(getattr(request.state, "csrf", ""))
    rows = []
    for role in roles:
        name = str(role.get("role"))
        enabled = bool(role.get("enabled"))
        target = "0" if enabled else "1"
        label = "停用" if enabled else "启用"
        style = "danger" if enabled else "ghost"
        action = (
            f'<form class="inline" method="post" action="/settings/roles">'
            f'<input type="hidden" name="name" value="{esc(name)}">'
            f'<input type="hidden" name="enabled" value="{target}">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<button class="small {style}" type="submit">{label}</button></form>'
        )
        rows.append(
            [
                Html(f"<code>{esc(name)}</code>"),
                esc(role.get("display_name")),
                esc(role.get("kind")),
                tag("自定义", "") if role.get("custom") else Html("内置"),
                tag("启用", "ok") if enabled else tag("停用", "bad"),
                esc(role.get("weight")),
                Html(action),
            ]
        )
    return table(
        ["角色键", "名称", "类型", "席位", "状态", "权重", "操作"],
        rows,
        empty="没有角色配置",
    )


def _role_registry(config: dict) -> tuple[list[dict], str | None]:
    try:
        registry = RoleRegistry.from_config(config)
    except RoleError as exc:
        return [], str(exc)
    return registry.summary(), None


def _writes_table(rows: list[dict]) -> Html:
    body = []
    for row in rows:
        extra = {key: value for key, value in row.items() if key not in _BOOKKEEPING}
        body.append(
            [
                esc(row.get("ts")),
                esc(row.get("action")),
                Html(f"<code>{esc(json.dumps(extra, ensure_ascii=False, default=str))}</code>"),
            ]
        )
    return table(["时间", "动作", "详情"], body, empty="还没有 WebUI 写操作")


def _config_view(config: dict) -> Html:
    sections = [
        ("工作流", config.get("workflow") or {}),
        ("数据源", config.get("data_source") or {}),
        ("预算", config.get("budget") or {}),
        ("输出", config.get("output") or config.get("output_template") or {}),
        ("告警", config.get("alert_rules") or {}),
        ("模型", config.get("llm_config") or {}),
    ]
    parts = ['<p class="muted">密钥类字段已脱敏；完整合并配置见下方原始视图。</p>']
    for title, section in sections:
        parts.append(f"<h2>{esc(title)}</h2>")
        parts.append(str(json_block(section)))
    parts.append("<details><summary>完整配置（已脱敏）</summary>")
    parts.append(str(json_block(config)))
    parts.append("</details>")
    return Html("".join(parts))


async def view(request: Request) -> Response:
    service = runtime(request)
    report = await run_in_threadpool(config_schema.validate_dir, service.config_dir)
    roles, role_error = _role_registry(service.config)
    body = Html(
        str(_validation(report))
        + str(_warnings(report))
        + '<div class="panel"><dl class="kv">'
        f"<dt>配置目录</dt><dd><code>{esc(service.config_dir)}</code></dd>"
        f"<dt>状态目录</dt><dd><code>{esc(service.home)}</code></dd>"
        f"<dt>配置指纹</dt><dd><code>{esc(service.config.get('config_hash'))}</code></dd>"
        "</dl></div>"
        + "<h2>角色席位</h2>"
        + (f'<div class="notice error">角色配置不合法：{esc(role_error)}</div>' if role_error else "")
        + str(_roles_table(request, roles))
        + '<p class="muted">调整角色只写 <code>agent_roles.toml</code>，写入前会重新校验席位上限（DEC-005）。</p>'
        + "<h2>最近写操作</h2>"
        + str(_writes_table(recent_writes(service.home, limit=20)))
        + "<h2>配置</h2>"
        + str(_config_view(service.redacted_config()))
    )
    return HTMLResponse(
        str(page(request, "设置", body, active="/settings", notice=request.query_params.get("notice")))
    )


async def toggle_role(request: Request) -> Response:
    form, failure = await checked_form(request)
    if failure is not None:
        return failure
    service = runtime(request)
    name = str(form.get("name") or "").strip()
    enabled = str(form.get("enabled") or "") in {"1", "true", "on", "yes"}
    if not name:
        return _error(request, "缺少角色名。")
    try:
        payload = await run_in_threadpool(
            config_edit.set_enabled, name, enabled, config_dir=service.config_dir
        )
    except config_edit.ConfigEditError as exc:
        return _error(request, str(exc))
    service.reload_config()
    audit_write(
        service.home,
        "settings.role",
        role=payload.get("role") or name,
        enabled=enabled,
        changed=payload.get("changed"),
    )
    state = "启用" if enabled else "停用"
    suffix = "" if payload.get("changed") else "（状态未变化）"
    return redirect("/settings", notice=f"已{state}角色 {payload.get('role') or name}{suffix}")


def _error(request: Request, message: str) -> HTMLResponse:
    service = runtime(request)
    roles, _ = _role_registry(service.config)
    body = Html(
        str(_roles_table(request, roles))
        + "<h2>最近写操作</h2>"
        + str(_writes_table(recent_writes(service.home, limit=20)))
    )
    return HTMLResponse(
        str(page(request, "设置", body, active="/settings", error=message)),
        status_code=400,
    )


def routes() -> list[Route]:
    return [
        Route("/settings", view, methods=["GET"]),
        Route("/settings/roles", toggle_role, methods=["POST"]),
    ]


__all__ = ["routes"]
