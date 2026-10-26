"""Runtime services shared by the WebUI pages.

One process, one :class:`WebRuntime`. It owns the effective configuration, the
state directory and the in-memory registry that powers the progress page.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from stock_senate.config.loader import ensure_home, env, load_config
from stock_senate.data.codes import normalize_code
from stock_senate.observability.logging import get_logger, redact
from stock_senate.webui.auth import WebAuth, audit_write

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            "events": list(self.events[-60:]),
        }


class WebRuntime:
    def __init__(self, *, config_dir: Path | str | None = None, auth: WebAuth | None = None) -> None:
        self.config = load_config(config_dir)
        self.config_dir = Path(str(self.config.get("config_dir") or config_dir or ".")).expanduser()
        self.home = ensure_home(Path(str(self.config.get("home"))))
        self.auth = auth or WebAuth(env("WEBUI_PASSWORD"))
        self.started_at = _now()
        self._jobs: dict[str, AnalysisJob] = {}
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
            log.warning("webui analysis failed", extra={"event": "webui_analysis_failed", "job": job.job_id})
            with self._lock:
                job.status = "failed"
                job.error = str(redact(exc))
                job.finished_at = _now()

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


__all__ = ["AnalysisJob", "STAGE_ORDER", "WebRuntime", "today"]
