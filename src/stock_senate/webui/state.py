"""Runtime services shared by the WebUI pages.

One process, one :class:`WebRuntime`. It owns the effective configuration, the
state directory and the in-memory registry that powers the progress page.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from stock_senate.config.loader import ensure_home, env, load_config
from stock_senate.data.codes import normalize_code
from stock_senate.observability.logging import get_logger, redact
from stock_senate.webui.auth import WebAuth, audit_write
from stock_senate.workflow.hitl import Reviewer

log = get_logger("webui")

STAGE_ORDER: tuple[str, ...] = (
    "data",
    "analysts",
    "debate",
    "research",
    "trader",
    "hitl_after_trader",
    "risk",
    "hitl_before_final",
    "portfolio_manager",
    "quality_gate",
    "persist",
    "done",
)
MAX_JOBS = 200
MAX_REVIEWS = 200
_DECIDED = frozenset({"approved", "rejected", "abort"})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _review_timeout(config: Mapping[str, Any]) -> float | None:
    """Wait slightly past ``workflow.hitl.timeout_seconds`` so the graph's own
    deadline fires first; a non-positive setting disables the deadline, matching
    :class:`~stock_senate.workflow.hitl.HitlReviewer`."""
    section = ((config or {}).get("workflow") or {}).get("hitl") or {}
    try:
        value = float(section.get("timeout_seconds", 1800))
    except (TypeError, ValueError):
        value = 1800.0
    return value + 5 if value > 0 else None


@dataclass
class AnalysisJob:
    """State of one WebUI-triggered analysis, polled by the progress page."""

    job_id: str
    code: str
    date: str
    started_at: str = field(default_factory=_now)
    status: str = "queued"  # queued | running | done | failed
    stage: str = "queued"
    events: list[dict[str, Any]] = field(default_factory=list)
    run_id: str | None = None
    rating: str | None = None
    error: str | None = None
    finished_at: str | None = None
    review_id: str | None = None

    def snapshot(self) -> dict[str, Any]:
        stages = [stage for stage in STAGE_ORDER if stage != "done"]
        current = self.stage if self.stage in stages else "queued"
        percent = 0
        if self.status == "done":
            percent = 100
        elif current in stages:
            percent = int((stages.index(current) / len(stages)) * 100)
        return {
            "job_id": self.job_id,
            "code": self.code,
            "date": self.date,
            "status": self.status,
            "stage": self.stage,
            "percent": percent,
            "run_id": self.run_id,
            "rating": self.rating,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "review_id": self.review_id,
            "events": list(self.events[-60:]),
        }


@dataclass
class PendingReview:
    """One HITL checkpoint waiting for a human decision in the WebUI.

    The analysis thread blocks on :attr:`event`; approving, rejecting or
    aborting it from the reviews page releases the thread with that answer.
    """

    review_id: str
    job_id: str
    code: str
    date: str
    checkpoint: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    status: str = "pending"  # pending | approved | rejected | abort | expired
    decided_at: str | None = None
    note: str = ""
    answer: dict[str, Any] = field(default_factory=dict)
    event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "job_id": self.job_id,
            "code": self.code,
            "date": self.date,
            "checkpoint": self.checkpoint,
            "created_at": self.created_at,
            "status": self.status,
            "decided_at": self.decided_at,
            "note": self.note,
        }


class WebRuntime:
    def __init__(
        self, *, config_dir: Path | str | None = None, auth: WebAuth | None = None
    ) -> None:
        self.config = load_config(config_dir)
        self.config_dir = Path(str(self.config.get("config_dir") or config_dir or ".")).expanduser()
        self.home = ensure_home(Path(str(self.config.get("home"))))
        self.auth = auth or WebAuth(env("WEBUI_PASSWORD"))
        self.started_at = _now()
        self._jobs: dict[str, AnalysisJob] = {}
        self._reviews: dict[str, PendingReview] = {}
        self._lock = threading.Lock()

    # -- configuration ----------------------------------------------------

    def reload_config(self) -> dict[str, Any]:
        """Re-read configuration so a settings write affects the next run."""
        self.config = load_config(self.config_dir)
        self.home = ensure_home(Path(str(self.config.get("home"))))
        return self.config

    def redacted_config(self) -> dict[str, Any]:
        return redact(copy.deepcopy(self.config))

    # -- analysis jobs ----------------------------------------------------

    def start_analysis(
        self,
        code: str,
        day: str,
        *,
        hitl: bool = True,
        enable: Iterable[str] = (),
        disable: Iterable[str] = (),
        weights: Iterable[str] = (),
    ) -> AnalysisJob:
        normalized = normalize_code(code)
        job = AnalysisJob(job_id=uuid.uuid4().hex[:12], code=normalized, date=day)
        with self._lock:
            self._jobs[job.job_id] = job
            self._prune()
        thread = threading.Thread(
            target=self._run_job,
            args=(job, hitl, list(enable), list(disable), list(weights)),
            name=f"webui-analysis-{job.job_id}",
            daemon=True,
        )
        thread.start()
        return job

    def job(self, job_id: str) -> AnalysisJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self, limit: int = 20) -> list[AnalysisJob]:
        with self._lock:
            values = sorted(self._jobs.values(), key=lambda item: item.started_at, reverse=True)
        return values[:limit]

    def _prune(self) -> None:
        if len(self._jobs) <= MAX_JOBS:
            return
        finished = sorted(
            (job for job in self._jobs.values() if job.status in {"done", "failed"}),
            key=lambda item: item.started_at,
        )
        for job in finished[: len(self._jobs) - MAX_JOBS]:
            self._jobs.pop(job.job_id, None)

    # -- human review -----------------------------------------------------

    def reviews(self, limit: int = 50) -> list[PendingReview]:
        with self._lock:
            values = sorted(self._reviews.values(), key=lambda item: item.created_at, reverse=True)
        return values[:limit]

    def pending_reviews(self) -> list[PendingReview]:
        return [review for review in self.reviews(limit=MAX_REVIEWS) if review.status == "pending"]

    def review(self, review_id: str) -> PendingReview | None:
        with self._lock:
            return self._reviews.get(review_id)

    def decide_review(self, review_id: str, *, decision: str, note: str = "") -> PendingReview:
        """Record a human decision and release the waiting analysis thread."""
        if decision not in _DECIDED:
            raise ValueError(f"未知的复核结论：{decision}")
        with self._lock:
            review = self._reviews.get(review_id)
            if review is None:
                raise KeyError(review_id)
            if review.status != "pending":
                raise ValueError("该复核已处理")
            review.status = decision
            review.decided_at = _now()
            review.note = str(note or "").strip()[:300]
            review.answer = {"decision": decision}
            if review.note:
                review.answer["note"] = review.note
            review.event.set()
            return review

    def _web_reviewer(self, job: AnalysisJob) -> Reviewer:
        """Reviewer that parks each checkpoint in the queue until a human decides."""

        def handler(checkpoint: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
            review = self._open_review(job, checkpoint, payload)
            if not review.event.wait(_review_timeout(self.config)):
                self._close_review(review, "expired", note="等待人工复核超时")
                raise TimeoutError("WebUI 审批超时")
            with self._lock:
                status = review.status
                answer = dict(review.answer)
            if status not in _DECIDED:
                raise TimeoutError("复核已关闭")
            return answer

        return handler

    def _open_review(
        self, job: AnalysisJob, checkpoint: str, payload: Mapping[str, Any]
    ) -> PendingReview:
        review = PendingReview(
            review_id=uuid.uuid4().hex[:12],
            job_id=job.job_id,
            code=job.code,
            date=job.date,
            checkpoint=checkpoint,
            payload=dict(payload),
        )
        with self._lock:
            self._reviews[review.review_id] = review
            job.review_id = review.review_id
            self._prune_reviews()
        return review

    def _close_review(self, review: PendingReview, status: str, *, note: str = "") -> None:
        with self._lock:
            if review.status != "pending":
                return
            review.status = status
            review.decided_at = _now()
            if note:
                review.note = note
            review.event.set()

    def _expire_reviews(self, job_id: str) -> None:
        with self._lock:
            stale = [
                review
                for review in self._reviews.values()
                if review.job_id == job_id and review.status == "pending"
            ]
        for review in stale:
            self._close_review(review, "expired", note="分析任务已结束，复核自动关闭")

    def _prune_reviews(self) -> None:
        if len(self._reviews) <= MAX_REVIEWS:
            return
        closed = sorted(
            (review for review in self._reviews.values() if review.status != "pending"),
            key=lambda item: item.created_at,
        )
        for review in closed[: len(self._reviews) - MAX_REVIEWS]:
            self._reviews.pop(review.review_id, None)

    def _run_job(
        self,
        job: AnalysisJob,
        hitl: bool,
        enable: list[str],
        disable: list[str],
        weights: list[str],
    ) -> None:
        from stock_senate.graph.senate_graph import SenateGraph

        with self._lock:
            job.status = "running"

        def progress(stage: str, status: str, fields: Mapping[str, Any]) -> None:
            with self._lock:
                job.stage = stage
                job.events.append({"ts": _now(), "stage": stage, "status": status, **dict(fields)})
                if status == "done" and stage == "done":
                    job.status = "done"
                    job.run_id = str(fields.get("run_id") or "") or job.run_id
                    job.rating = str(fields.get("rating") or "") or job.rating
                    job.finished_at = _now()

        try:
            config = self.reload_config()
            graph = SenateGraph(config=config)
            overrides = {"enable": enable, "disable": disable, "weights": weights}
            _state, decision = graph.propagate(
                job.code,
                job.date,
                hitl=hitl,
                hitl_handler=self._web_reviewer(job) if hitl else None,
                overrides=overrides,
                progress=progress,
            )
            with self._lock:
                job.status = "done"
                job.stage = "done"
                job.rating = str(decision.get("rating") or job.rating or "")
                job.run_id = str(decision.get("run_id") or job.run_id or "") or job.run_id
                job.finished_at = job.finished_at or _now()
        except Exception as exc:  # noqa: BLE001 - surfaced on the progress page
            log.warning(
                "webui analysis failed", extra={"event": "webui_analysis_failed", "job": job.job_id}
            )
            with self._lock:
                job.status = "failed"
                job.error = str(redact(exc))
                job.finished_at = _now()
        finally:
            self._expire_reviews(job.job_id)

    # -- history favourites ----------------------------------------------

    @property
    def favorites_path(self) -> Path:
        return self.home / "webui" / "favorites.json"

    def favorites(self) -> list[str]:
        path = self.favorites_path
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        values = payload.get("run_ids") if isinstance(payload, dict) else payload
        return [str(item) for item in values or [] if isinstance(item, str)]

    def toggle_favorite(self, run_id: str) -> bool:
        """Flip a run's favourite flag and audit the write; returns the new state."""
        current = self.favorites()
        marked = run_id not in current
        current = [item for item in current if item != run_id]
        if marked:
            current.insert(0, run_id)
        path = self.favorites_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"run_ids": current}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
        audit_write(self.home, "history.favorite", run_id=run_id, marked=marked)
        return marked


def today() -> str:
    return date.today().isoformat()


__all__ = ["AnalysisJob", "PendingReview", "STAGE_ORDER", "WebRuntime", "today"]
