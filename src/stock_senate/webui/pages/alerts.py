"""Alerts page: rules, validation, dry-run and the delivered-alert log."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from stock_senate.alerts import engine as alert_engine
from stock_senate.alerts.channels import webui as webui_channel
from stock_senate.config import schema as config_schema
from stock_senate.observability.logging import redact
from stock_senate.webui.auth import audit_write
from stock_senate.webui.pages.common import checked_form, runtime, split_csv
from stock_senate.webui.templates import Html, csrf_input, esc, json_block, page, table, tag

_LEVELS = {"high": "bad", "mid": "warn", "low": ""}
_STATUSES = {"would_fire": "bad", "unknown": "warn", "not_fired": "ok", "invalid": "bad"}


def _rules_section(config: dict) -> Html:
    section = config.get("alert_rules") or {}
    global_rows = [
        ["enabled", esc(section.get("global", {}).get("enabled", False))],
        ["dedup_hours", esc(section.get("global", {}).get("dedup_hours"))],
        ["quiet_hours", esc(", ".join(section.get("global", {}).get("quiet_hours") or []))],
        ["max_triggers_per_day", esc(section.get("global", {}).get("max_triggers_per_day"))],
    ]
    rules = section.get("rules") or []
    rule_rows = [
        [
            esc(rule.get("id")),
            esc(rule.get("name")),
            tag(rule.get("level", "mid"), _LEVELS.get(str(rule.get("level")), "")),
            Html(f"<code>{esc(rule.get('when'))}</code>"),
            esc(rule.get("cooldown_minutes")),
            esc(", ".join(rule.get("channels") or [])),
        ]
        for rule in rules
    ]
    channels = redact(section.get("channels") or {})
    channel_rows = [
        [
            esc(name),
            esc(settings.get("enabled")),
            esc(settings.get("level")),
            esc(_channel_detail(settings)),
        ]
        for name, settings in channels.items()
        if isinstance(settings, dict)
    ]
    return Html(
        "<h2>全局策略</h2>"
        + str(table(["项", "值"], global_rows))
        + "<h2>渠道</h2>"
        + str(
            table(
                ["渠道", "启用", "级别", "配置"],
                channel_rows,
                empty="没有配置渠道",
            )
        )
        + '<p class="muted">Webhook 发送前签名（X-StockSenate-Signature）并携带幂等键；'
        "密钥只从 <code>*_env</code> 指定的环境变量读取，页面不回显。</p>"
        + "<h2>规则</h2>"
        + str(
            table(["ID", "名称", "级别", "条件", "冷却(分钟)", "渠道"], rule_rows, empty="没有规则")
        )
    )


def _channel_detail(settings: dict) -> str:
    keys = ("url_env", "secret_env", "max_retries", "signature_header", "idempotency_header")
    parts = [f"{key}={settings[key]}" for key in keys if key in settings]
    return " · ".join(parts) or "—"


def _validation(config: dict) -> Html:
    report = config_schema.validate_alert_rules(config)
    if report.get("ok"):
        return Html('<p class="notice ok">规则校验通过。</p>')
    errors = report.get("errors") or []
    return Html(
        '<div class="notice error">规则校验失败：'
        + "; ".join(
            f"{esc(item.get('file'))}:{esc(item.get('key'))} {esc(item.get('message'))}"
            for item in errors
        )
        + "</div>"
    )


def _dry_run_form(request: Request) -> Html:
    return Html(
        '<div class="panel"><form method="post" action="/alerts/dry-run">'
        + str(csrf_input(request))
        + '<div class="row">'
        '<div><label for="rule">规则 ID（留空表示全部）</label>'
        '<input id="rule" type="text" name="rule" placeholder="price_limit_up"></div>'
        '<div><label for="codes">标的（留空表示监控列表）</label>'
        '<input id="codes" type="text" name="codes" placeholder="600519,000001"></div>'
        '<div><button type="submit">试跑（不推送）</button></div>'
        "</div></form></div>"
    )


def _dry_run_result(payload: dict) -> Html:
    if payload.get("ok") is False:
        return Html(f'<div class="notice error">{esc(payload.get("error"))}</div>')
    rows = [
        [
            esc(item.get("rule")),
            esc(item.get("name")),
            esc(item.get("code")),
            tag(item.get("status"), _STATUSES.get(str(item.get("status")), "")),
            esc(_observed(item)),
        ]
        for item in payload.get("results") or []
    ]
    note = f'<p class="notice warn">{esc(payload.get("note"))}</p>' if payload.get("note") else ""
    head = (
        f"<p>试跑日期 {esc(payload.get('as_of'))} · "
        f"标的 {esc(', '.join(payload.get('codes') or []))} · "
        f"触发 {esc(payload.get('triggered'))} · 未知 {esc(payload.get('unknown'))}</p>"
    )
    return Html(
        head
        + note
        + str(table(["规则", "名称", "标的", "状态", "观测值"], rows, empty="没有可评估的规则"))
        + "<details><summary>原始结果</summary>"
        + str(json_block(payload))
        + "</details>"
    )


def _observed(row: dict) -> str:
    observed = row.get("observed")
    if isinstance(observed, dict):
        return ", ".join(f"{key}={value}" for key, value in observed.items())
    return "—" if observed is None else str(observed)


def _delivered(home: object) -> Html:
    rows = []
    for event in webui_channel.recent(limit=50, home=home):  # type: ignore[arg-type]
        rows.append(
            [
                esc(event.get("created_at") or event.get("delivered_at")),
                esc(
                    (event.get("rule") or {}).get("id")
                    if isinstance(event.get("rule"), dict)
                    else event.get("rule")
                ),
                esc(event.get("code")),
                tag(str(event.get("level") or "mid"), _LEVELS.get(str(event.get("level")), "")),
                esc(event.get("message")),
            ]
        )
    return table(["时间", "规则", "标的", "级别", "内容"], rows, empty="还没有落库的告警")


async def view(request: Request) -> Response:
    service = runtime(request)
    body = Html(
        str(_rules_section(service.config))
        + str(_validation(service.config))
        + str(_dry_run_form(request))
        + "<h2>已投递告警</h2>"
        + str(_delivered(service.home))
    )
    return HTMLResponse(str(page(request, "告警", body, active="/alerts")))


async def dry_run(request: Request) -> Response:
    form, failure = await checked_form(request)
    if failure is not None:
        return failure
    rule = str(form.get("rule") or "").strip() or None
    codes = split_csv(form.get("codes")) or None
    service = runtime(request)
    payload = await run_in_threadpool(
        alert_engine.dry_run,
        rule,
        config=service.config,
        home=service.home,
        codes=codes,
    )
    audit_write(
        service.home,
        "alerts.dry_run",
        rule=rule,
        codes=codes or [],
        triggered=payload.get("triggered"),
        unknown=payload.get("unknown"),
    )
    body = Html(
        str(_rules_section(service.config))
        + str(_validation(service.config))
        + str(_dry_run_form(request))
        + "<h2>试跑结果</h2>"
        + str(_dry_run_result(payload))
        + "<h2>已投递告警</h2>"
        + str(_delivered(service.home))
    )
    return HTMLResponse(str(page(request, "告警", body, active="/alerts")))


def routes() -> list[Route]:
    return [
        Route("/alerts", view, methods=["GET"]),
        Route("/alerts/dry-run", dry_run, methods=["POST"]),
    ]


__all__ = ["routes"]
