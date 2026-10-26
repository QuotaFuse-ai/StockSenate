"""Queue page: inspect batch jobs, submit a CSV, and drive pause/resume/cancel."""

from __future__ import annotations

import uuid

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from stock_senate.queue import manager as queue_manager
from stock_senate.webui.auth import audit_write
from stock_senate.webui.pages.common import checked_form, redirect, runtime
from stock_senate.webui.templates import Html, csrf_input, esc, json_block, page, table, tag

_STATUS_TAGS = {
    "completed": "ok",
    "failed": "bad",
    "cancelled": "bad",
    "running": "warn",
    "retrying": "warn",
    "paused": "warn",
    "queued": "",
    "skipped": "",
}
_ACTIVE = {"queued", "running", "retrying"}
_PAUSABLE = {"queued", "running", "retrying"}
_MAX_UPLOAD_CHARS = 1_000_000
_JOBS_SHOWN = 60


def _submit_form(request: Request) -> Html:
    return Html(
        '<div class="panel"><form method="post" action="/queue/submit" '
        'enctype="multipart/form-data">' + str(csrf_input(request)) + '<div class="row">'
        '<div><label for="file">CSV 文件（列：code[,date][,priority]）</label>'
        '<input id="file" type="file" name="file" accept=".csv,text/csv"></div>'
        '<div><label for="concurrency">并发数</label>'
        '<input id="concurrency" type="number" name="concurrency" value="1" min="1" max="32"></div>'
        '<div><button type="submit">上传并执行</button></div>'
        "</div>"
        '<p class="muted" style="margin-bottom:6px">也可以直接粘贴 CSV 内容：</p>'
        '<textarea name="csv" placeholder="code,date&#10;600519,2026-10-24'
        '&#10;000001,2026-10-24"></textarea>'
        '<p class="muted">提交后会在本进程内同步执行完队列；同一标的不会被并发分析两次。</p>'
        "</form></div>"
    )


def _summary(payload: dict) -> Html:
    queue = payload.get("queue") or {}
    session = queue.get("session") or {}
    usage = session.get("usage") or {}
    paused = bool(queue.get("paused"))
    head = Html(
        (tag("队列已暂停", "warn") if paused else tag("队列可用", "ok"))
        + " "
        + (
            tag(f"停止原因：{queue.get('stop_reason')}", "bad")
            if paused and queue.get("stop_reason")
            else ""
        )
    )
    counts = payload.get("counts") or {}
    stats = [
        ["任务总数", esc(payload.get("total"))],
        [
            "状态分布",
            esc(", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "—"),
        ],
        [
            "本次会话",
            esc(
                f"调用 {usage.get('llm_calls', 0)} 次 · "
                f"tokens {usage.get('total_tokens', 0)} · "
                f"耗时 {session.get('elapsed_seconds', 0)}s"
            ),
        ],
        ["状态文件", Html(f"<code>{esc(payload.get('path'))}</code>")],
    ]
    return Html(str(head) + str(table(["项", "值"], stats)))


def _actions(job: dict, csrf: str) -> Html:
    job_id = str(job.get("id") or "")
    status_value = str(job.get("status") or "")
    buttons: list[str] = []
    if status_value in _ACTIVE:
        if status_value in _PAUSABLE:
            buttons.append(_control_form("/queue/pause", job_id, "暂停", "ghost", csrf))
        buttons.append(_control_form("/queue/cancel", job_id, "取消", "danger", csrf))
    elif status_value in {"paused", "failed", "cancelled"}:
        buttons.append(_control_form("/queue/resume", job_id, "恢复", "ghost", csrf))
    return Html(" ".join(buttons) or "—")


def _control_form(action: str, job_id: str, label: str, style: str, csrf: str) -> str:
    return (
        f'<form class="inline" method="post" action="{action}">'
        f'<input type="hidden" name="job_id" value="{esc(job_id)}">'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
        f'<button class="small {style}" type="submit">{label}</button></form>'
    )


def _jobs_table(request: Request, payload: dict) -> Html:
    csrf = str(getattr(request.state, "csrf", ""))
    rows = []
    for job in reversed(payload.get("jobs") or []):
        status_value = str(job.get("status") or "")
        run_id = job.get("run_id")
        run_cell = (
            Html(f'<a href="/reports/{esc(run_id)}">{esc(run_id)}</a>') if run_id else Html("—")
        )
        actions = str(_actions(job, csrf))
        rows.append(
            [
                Html(f"<code>{esc(job.get('id'))}</code>"),
                esc(job.get("code")),
                esc(job.get("date")),
                tag(status_value, _STATUS_TAGS.get(status_value, "")),
                esc(job.get("attempts")),
                run_cell,
                esc(job.get("error") or "—"),
                Html(actions),
            ]
        )
        if len(rows) >= _JOBS_SHOWN:
            break
    return table(
        ["任务", "标的", "日期", "状态", "尝试", "报告", "错误", "操作"],
        rows,
        empty="队列里还没有任务",
    )


def _result(payload: dict) -> Html:
    if payload.get("ok") is False:
        return Html(f'<div class="notice error">{esc(payload.get("error") or "提交失败")}</div>')
    parts = [
        f"<p>已提交 {esc(payload.get('submitted'))} 个任务"
        f"（并发 {esc(payload.get('concurrency'))}）。</p>"
    ]
    skipped = payload.get("skipped") or []
    if skipped:
        parts.append(
            table(
                ["标的", "日期", "跳过原因"],
                [
                    [esc(item.get("code")), esc(item.get("date")), esc(item.get("reason"))]
                    for item in skipped
                ],
            )
        )
    errors = payload.get("errors") or []
    if errors:
        parts.append(
            '<div class="notice error">部分行未入库：</div>'
            + str(
                table(
                    ["行", "标的", "错误"],
                    [
                        [esc(item.get("row")), esc(item.get("code")), esc(item.get("error"))]
                        for item in errors
                    ],
                )
            )
        )
    parts.append("<details><summary>原始结果</summary>" + str(json_block(payload)) + "</details>")
    return Html("".join(parts))


async def view(request: Request) -> Response:
    service = runtime(request)
    payload = await run_in_threadpool(queue_manager.status, home=service.home)
    body = Html(
        str(_summary(payload))
        + str(_submit_form(request))
        + "<h2>任务</h2>"
        + str(_jobs_table(request, payload))
    )
    return HTMLResponse(
        str(page(request, "队列", body, active="/queue", notice=request.query_params.get("notice")))
    )


async def submit(request: Request) -> Response:
    form, failure = await checked_form(request)
    if failure is not None:
        return failure
    service = runtime(request)
    text = ""
    upload = form.get("file")
    if isinstance(upload, UploadFile) and upload.filename:
        raw = await upload.read()
        if len(raw) > _MAX_UPLOAD_CHARS:
            return _error(request, "CSV 文件过大（上限约 1MB）。")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return _error(request, "CSV 必须是 UTF-8 编码。")
    else:
        text = str(form.get("csv") or "")
    if not text.strip():
        return _error(request, "请选择 CSV 文件或粘贴 CSV 内容。")
    if len(text) > _MAX_UPLOAD_CHARS:
        return _error(request, "CSV 内容过长（上限约 1MB）。")

    uploads = service.home / "queue" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / f"webui-{uuid.uuid4().hex[:10]}.csv"
    path.write_text(text, encoding="utf-8")

    concurrency = _concurrency(form.get("concurrency"))
    audit_write(service.home, "queue.submit", file=str(path), concurrency=concurrency)
    payload = await run_in_threadpool(
        queue_manager.submit_file,
        path,
        concurrency=concurrency,
        home=service.home,
        config=service.config,
    )
    summary = await run_in_threadpool(queue_manager.status, home=service.home)
    body = Html(
        str(_result(payload))
        + str(_summary(summary))
        + "<h2>任务</h2>"
        + str(_jobs_table(request, summary))
    )
    return HTMLResponse(str(page(request, "队列", body, active="/queue")))


async def _control(request: Request, action: str) -> Response:
    form, failure = await checked_form(request)
    if failure is not None:
        return failure
    service = runtime(request)
    job_id = str(form.get("job_id") or "").strip()
    if not job_id:
        return _error(request, "缺少 job_id。")
    handler = {
        "pause": queue_manager.pause,
        "cancel": queue_manager.cancel,
        "resume": queue_manager.resume,
    }[action]
    payload = await run_in_threadpool(handler, job_id, home=service.home, config=service.config)
    audit_write(service.home, f"queue.{action}", job_id=job_id, ok=payload.get("ok"))
    if payload.get("ok") is False:
        reason = payload.get("reason") or payload.get("error") or "操作未生效"
        return _redirect(request, f"{job_id}：{reason}")
    note = payload.get("note") or payload.get("reason") or "已完成"
    return _redirect(request, f"{job_id}：{note}")


def _redirect(request: Request, note: str) -> Response:
    return redirect("/queue", notice=note)


def _concurrency(value: object) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        number = 1
    return max(1, min(number, 32))


def _error(request: Request, message: str) -> HTMLResponse:
    service = runtime(request)
    payload = queue_manager.status(home=service.home)
    body = Html(str(_submit_form(request)) + "<h2>任务</h2>" + str(_jobs_table(request, payload)))
    return HTMLResponse(
        str(page(request, "队列", body, active="/queue", error=message)),
        status_code=400,
    )


async def pause(request: Request) -> Response:
    return await _control(request, "pause")


async def cancel(request: Request) -> Response:
    return await _control(request, "cancel")


async def resume(request: Request) -> Response:
    return await _control(request, "resume")


def routes() -> list[Route]:
    return [
        Route("/queue", view, methods=["GET"]),
        Route("/queue/submit", submit, methods=["POST"]),
        Route("/queue/pause", pause, methods=["POST"]),
        Route("/queue/cancel", cancel, methods=["POST"]),
        Route("/queue/resume", resume, methods=["POST"]),
    ]


__all__ = ["routes"]
