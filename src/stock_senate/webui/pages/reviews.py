"""Reviews page: decide pending HITL checkpoints from the browser.

With HITL enabled, an analysis thread parks at each checkpoint until someone
approves, rejects or aborts it. This page is the browser counterpart of the
terminal prompt in ``ssenate analyze``: the decision is recorded in the run's
audit trail exactly as a terminal answer would be.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from stock_senate.webui.auth import audit_write
from stock_senate.webui.pages.common import checked_form, redirect, runtime
from stock_senate.webui.state import PendingReview
from stock_senate.webui.templates import Html, csrf_input, esc, json_block, page, table, tag

_CHECKPOINTS = {"after_trader": "交易计划完成后", "before_final": "终审前"}
_DECISIONS = {"approve": "approved", "reject": "rejected", "abort": "abort"}
_STATUS = {
    "pending": ("待复核", "warn"),
    "approved": ("已同意", "ok"),
    "rejected": ("已驳回", "bad"),
    "abort": ("已中止", "bad"),
    "expired": ("已过期", ""),
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _summary(review: PendingReview) -> Html:
    payload = review.payload
    research = _mapping(payload.get("research"))
    trade = _mapping(payload.get("trade"))
    risks = payload.get("risks") or []
    rows = [
        ["复核点", esc(_CHECKPOINTS.get(review.checkpoint, review.checkpoint))],
        ["标的 / 日期", esc(f"{review.code} · {review.date}")],
        ["任务", Html(f'<a href="/progress/{esc(review.job_id)}">{esc(review.job_id)}</a>')],
        ["创建时间", esc(review.created_at)],
        ["研究立场", esc(research.get("stance") or "—")],
        ["交易方向", esc(trade.get("direction") or "—")],
        ["风险条数", esc(len(risks))],
    ]
    return table(["项", "值"], rows)


def _decision_form(request: Request, review: PendingReview) -> Html:
    note_id = f"note-{review.review_id}"
    return Html(
        '<form method="post" action="/reviews/decide">'
        + str(csrf_input(request))
        + f'<input type="hidden" name="review_id" value="{esc(review.review_id)}">'
        + '<div class="row">'
        + f'<div><label for="{note_id}">备注（可选，最多 300 字）</label>'
        + f'<input id="{note_id}" type="text" name="note" maxlength="300"></div>'
        + "<div>"
        + '<button type="submit" name="decision" value="approve">同意</button> '
        + '<button class="ghost" type="submit" name="decision" value="reject">驳回</button> '
        + '<button class="danger" type="submit" name="decision" value="abort">中止</button>'
        + "</div></div></form>"
    )


def _card(request: Request, review: PendingReview) -> Html:
    return Html(
        '<div class="panel">'
        + str(_summary(review))
        + str(_decision_form(request, review))
        + "<details><summary>查看复核内容</summary>"
        + str(json_block(review.payload))
        + "</details></div>"
    )


def _pending(request: Request, reviews: list[PendingReview]) -> Html:
    if not reviews:
        return Html('<p class="muted">当前没有待复核的检查点。</p>')
    return Html("".join(str(_card(request, review)) for review in reviews))


def _decided(reviews: list[PendingReview]) -> Html:
    rows = []
    for review in reviews:
        if review.status == "pending":
            continue
        label, kind = _STATUS.get(review.status, (review.status, ""))
        rows.append(
            [
                esc(f"{review.code} · {review.date}"),
                esc(_CHECKPOINTS.get(review.checkpoint, review.checkpoint)),
                tag(label, kind),
                esc(review.note or "—"),
                esc(review.decided_at or "—"),
                Html(f'<a href="/progress/{esc(review.job_id)}">{esc(review.job_id)}</a>'),
            ]
        )
    return table(
        ["标的 / 日期", "复核点", "结论", "备注", "处理时间", "任务"],
        rows,
        empty="还没有处理过的复核",
    )


def _body(request: Request) -> Html:
    service = runtime(request)
    everything = service.reviews(limit=50)
    pending = [review for review in everything if review.status == "pending"]
    return Html(
        f"<h2>待复核（{len(pending)}）</h2>"
        + str(_pending(request, pending))
        + "<h2>最近处理</h2>"
        + str(_decided(everything))
        + '<p class="muted">同意与驳回都会写入报告的人工复核痕迹；'
        "超时未处理时按 <code>workflow.hitl.on_timeout</code> 处理并标注为自动放行。</p>"
    )


async def view(request: Request) -> Response:
    return HTMLResponse(
        str(
            page(
                request,
                "审批",
                _body(request),
                active="/reviews",
                notice=request.query_params.get("notice"),
            )
        )
    )


async def decide(request: Request) -> Response:
    form, failure = await checked_form(request)
    if failure is not None:
        return failure
    review_id = str(form.get("review_id") or "").strip()
    decision = _DECISIONS.get(str(form.get("decision") or "").strip())
    note = str(form.get("note") or "").strip()
    if not review_id:
        return _error(request, "缺少复核编号。", status_code=400)
    if decision is None:
        return _error(request, "未知的复核结论。", status_code=400)
    service = runtime(request)
    try:
        review = service.decide_review(review_id, decision=decision, note=note)
    except KeyError:
        return _error(request, "复核不存在或已过期。", status_code=404)
    except ValueError as exc:
        return _error(request, str(exc), status_code=409)
    audit_write(
        service.home,
        "review.decide",
        review_id=review.review_id,
        decision=decision,
        job_id=review.job_id,
        checkpoint=review.checkpoint,
    )
    label = _STATUS.get(review.status, (review.status, ""))[0]
    return redirect("/reviews", notice=f"{review.code} {review.checkpoint} 已记录：{label}")


def _error(request: Request, message: str, *, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        str(page(request, "审批", _body(request), active="/reviews", error=message)),
        status_code=status_code,
    )


def routes() -> list[Route]:
    return [
        Route("/reviews", view, methods=["GET"]),
        Route("/reviews/decide", decide, methods=["POST"]),
    ]


__all__ = ["routes"]
