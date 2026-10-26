"""Batch queue: persistence, scheduling, pause/resume/cancel and budget trip.

There is no daemon. ``submit_file`` enqueues the CSV and then drains the queue
in the foreground with ``concurrency`` workers, so a CLI invocation always ends
with a final state on disk. ``resume`` drains again; ``pause``/``cancel`` take
effect on jobs that have not started, and on a running drain at the next
attempt boundary (the state file is re-read before every attempt).

Same-code jobs never run at once: the scheduler takes a per-code file lock in
``<home>/locks``, the same mechanism a second process uses, so a duplicate is
deferred instead of analysed twice.

``budget.toml`` ``[per_queue]`` ceilings are enforced across one queue session
(one ``submit``/``resume`` drain): when calls, tokens or wall clock run out the
whole queue pauses and an alert event goes through the configured channels.
Repeated failures open the circuit breaker the same way, following
``[circuit_breaker]``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from stock_senate.alerts.channels import dispatch as dispatch_alert
from stock_senate.alerts.engine import DISCLAIMER as ALERT_DISCLAIMER
from stock_senate.config.loader import ensure_home, load_config
from stock_senate.data.envelope import date_only, now_iso
from stock_senate.observability.logging import audit, get_logger, redact
from stock_senate.roles.llm import LLMConfigError, LLMRateLimited
from stock_senate.roles.registry import RoleError

from . import locks

log = get_logger("queue")

Runner = Callable[[str, str], Any]

_TERMINAL = {"completed", "failed", "skipped", "cancelled"}
_ACTIVE = {"queued", "running", "retrying", "paused"}
_CLAIMABLE = "queued"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_CONCURRENCY = 32
_MAX_RETRY_SECONDS = 300.0

_CODE_KEYS = ("code", "代码", "symbol", "标的")
_DATE_KEYS = ("date", "日期", "trade_date")
_PRIORITY_KEYS = ("priority", "优先级")


@dataclass(frozen=True)
class QueueOptions:
    max_attempts: int = 3
    retry_delay_seconds: float = 5.0
    on_failure: str = "skip"
    max_llm_calls: int = 1200
    max_total_tokens: int = 8_000_000
    max_wall_clock_seconds: float = 28_800.0
    stale_running_seconds: float = 6 * 3600.0
    breaker_enabled: bool = True
    failure_threshold: int = 5


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def status(
    job_id: str | None = None,
    *,
    home: Path | str | None = None,
    config: Mapping[str, Any] | str | Path | None = None,
) -> dict[str, Any]:
    """Queue summary, or one job when ``job_id`` is given."""
    root = _root(home)
    state = _load(root)
    summary = _summary(root, state)
    if not job_id:
        return {"ok": True, **summary}
    job = _find(state, str(job_id))
    return {
        "ok": job is not None,
        "found": job is not None,
        "job_id": str(job_id),
        "job": job,
        "queue": summary["queue"],
        "counts": summary["counts"],
    }


def submit_file(
    file: Path | str,
    *,
    concurrency: int = 1,
    home: Path | str | None = None,
    config: Mapping[str, Any] | str | Path | None = None,
    runner: Runner | None = None,
    run: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Enqueue a CSV (``code[,date][,priority]``) and drain it.

    ``priority`` is an integer; larger values run first, ties keep CSV order.
    Invalid rows are reported under ``errors`` and never become jobs. A row is
    skipped when the same ``(code, date)`` is already active or repeated in the
    file, which is what keeps one instrument from being analysed twice at once.
    """
    path = Path(file)
    if not path.is_file():
        return {"ok": False, "error": f"找不到 CSV 文件：{path}", "file": str(path)}

    cfg = load_config(config)
    options = _options(cfg)
    root = _root(home)
    workers = _concurrency(concurrency)
    rows, errors = _read_rows(path, _today(now))

    if runner is None:
        try:
            runner = _graph_runner(cfg)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            return {
                "ok": False,
                "error": f"无法初始化分析引擎：{redact(exc)}",
                "error_type": type(exc).__name__,
                "file": str(path),
            }

    skipped: list[dict[str, Any]] = []
    added: list[str] = []

    def enqueue(state: dict[str, Any]) -> None:
        active = {
            (str(job.get("code")), str(job.get("date")))
            for job in state["jobs"]
            if job.get("status") not in _TERMINAL
        }
        seen: set[tuple[str, str]] = set()
        order = int(state["queue"].get("next_order") or 1)
        for row in rows:
            key = (row["code"], row["date"])
            if key in seen:
                skipped.append({**_row_ref(row), "reason": "duplicate_in_file"})
                continue
            seen.add(key)
            if key in active:
                skipped.append({**_row_ref(row), "reason": "already_active"})
                continue
            job = {
                "id": f"q{order:05d}-{row['code']}-{row['date']}",
                "order": order,
                "code": row["code"],
                "date": row["date"],
                "priority": row["priority"],
                "status": "queued",
                "attempts": 0,
                "max_attempts": options.max_attempts,
                "concurrency": workers,
                "submitted_at": now_iso(),
                "started_at": "",
                "finished_at": "",
                "next_attempt_at": "",
                "error": "",
                "error_type": "",
                "cancel_requested": False,
                "pause_requested": False,
            }
            state["jobs"].append(job)
            active.add(key)
            added.append(job["id"])
            order += 1
        state["queue"]["next_order"] = order

    _mutate(root, enqueue)

    if added and run:
        report = _drain(root, cfg, options, workers, runner)
    else:
        report = _summary(root, _load(root))

    return {
        "ok": True,
        "file": str(path),
        "submitted": len(added),
        "skipped": skipped,
        "errors": errors,
        "job_ids": added,
        "concurrency": workers,
        **report,
    }


def cancel(
    job_id: str,
    *,
    home: Path | str | None = None,
    config: Mapping[str, Any] | str | Path | None = None,
) -> dict[str, Any]:
    """Cancel a job that has not finished; a running job stops after its attempt."""
    return _control(job_id, "cancel", home=home)


def pause(
    job_id: str,
    *,
    home: Path | str | None = None,
    config: Mapping[str, Any] | str | Path | None = None,
) -> dict[str, Any]:
    """Pause a queued job; a running job pauses after its current attempt."""
    return _control(job_id, "pause", home=home)


def resume(
    job_id: str,
    *,
    home: Path | str | None = None,
    config: Mapping[str, Any] | str | Path | None = None,
    runner: Runner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Requeue a paused/failed job and drain the queue again."""
    cfg = load_config(config)
    options = _options(cfg)
    root = _root(home)

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        job = _find(state, str(job_id))
        if job is None:
            return {"ok": False, "reason": "unknown_job"}
        status_value = str(job.get("status"))
        if status_value == "paused" and job.get("result"):
            # The analysis finished before the pause request was applied.
            job["status"] = "completed"
            job["pause_requested"] = False
            job["finished_at"] = job.get("finished_at") or now_iso()
            return {"ok": True, "reason": "already_finished", "job": dict(job)}
        if status_value not in {"paused", "failed", "cancelled"}:
            return {"ok": False, "reason": f"不能恢复状态为 {status_value} 的任务"}
        job.update(
            status="queued",
            attempts=0,
            error="",
            error_type="",
            next_attempt_at="",
            cancel_requested=False,
            pause_requested=False,
            started_at="",
            finished_at="",
            last_reason="resumed",
        )
        return {"ok": True, "reason": "requeued", "job": dict(job)}

    outcome = _mutate(root, mutate)
    if not outcome.get("ok"):
        return outcome

    state = _load(root)
    if any(job.get("status") == _CLAIMABLE for job in state["jobs"]):
        if runner is None:
            try:
                runner = _graph_runner(cfg)
            except Exception as exc:  # noqa: BLE001
                return {
                    **outcome,
                    "error": f"无法初始化分析引擎：{redact(exc)}",
                    "error_type": type(exc).__name__,
                }
        workers = _concurrency(
            int(state["queue"].get("last_concurrency") or outcome["job"].get("concurrency") or 1)
        )
        return {**outcome, **_drain(root, cfg, options, workers, runner)}
    return {**outcome, **_summary(root, state)}


# --------------------------------------------------------------------------- #
# control
# --------------------------------------------------------------------------- #


def _control(job_id: str, action: str, *, home: Path | str | None) -> dict[str, Any]:
    root = _root(home)

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        job = _find(state, str(job_id))
        if job is None:
            return {"ok": False, "job_id": str(job_id), "reason": "unknown_job"}
        current = str(job.get("status"))
        if current in _TERMINAL:
            return {
                "ok": False,
                "job_id": str(job_id),
                "status": current,
                "reason": "terminal",
            }
        if action == "cancel":
            if current == "running":
                job["cancel_requested"] = True
                return {
                    "ok": True,
                    "job_id": str(job_id),
                    "status": current,
                    "deferred": "running",
                    "note": "当前尝试结束后取消",
                }
            job.update(
                status="cancelled",
                cancel_requested=False,
                finished_at=now_iso(),
                last_reason="cancelled",
            )
            return {"ok": True, "job_id": str(job_id), "status": "cancelled"}
        if current == "paused":
            return {
                "ok": False,
                "job_id": str(job_id),
                "status": current,
                "reason": "already_paused",
            }
        if current == "running":
            job["pause_requested"] = True
            return {
                "ok": True,
                "job_id": str(job_id),
                "status": current,
                "deferred": "running",
                "note": "当前尝试结束后暂停",
            }
        job.update(status="paused", pause_requested=False, last_reason="paused")
        return {"ok": True, "job_id": str(job_id), "status": "paused"}

    result = _mutate(root, mutate)
    result.setdefault("job_id", str(job_id))
    return result


# --------------------------------------------------------------------------- #
# drain
# --------------------------------------------------------------------------- #


class _Drain:
    def __init__(
        self,
        root: Path,
        cfg: Mapping[str, Any],
        options: QueueOptions,
        concurrency: int,
        runner: Runner,
    ) -> None:
        self.root = root
        self.cfg = dict(cfg)
        self.options = options
        self.concurrency = concurrency
        self.runner = runner
        self._started = time.monotonic()
        self._threads: list[threading.Thread] = []

    def run(self) -> None:
        self._start_session()
        for index in range(self.concurrency):
            thread = threading.Thread(
                target=self._worker,
                name=f"queue-worker-{index + 1}",
                daemon=False,
            )
            self._threads.append(thread)
            thread.start()
        for thread in self._threads:
            thread.join()
        self._finish_session()

    # -- session ---------------------------------------------------------- #

    def _start_session(self) -> None:
        def mutate(state: dict[str, Any]) -> None:
            queue = state["queue"]
            stale = _recover_stale(state, self.options.stale_running_seconds)
            queue["session"] = {
                "id": uuid.uuid4().hex[:12],
                "started_at": now_iso(),
                "finished_at": "",
                "concurrency": self.concurrency,
                "usage": {
                    "jobs": 0,
                    "llm_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                },
                "tripped": "",
                "alert": None,
                "recovered": stale,
            }
            queue["paused"] = False
            queue["stop_reason"] = ""
            queue["tripped_at"] = ""
            queue["consecutive_failures"] = 0
            queue["last_concurrency"] = self.concurrency

        _mutate(self.root, mutate, options=self.options)

    def _finish_session(self) -> None:
        def mutate(state: dict[str, Any]) -> None:
            session = state["queue"].get("session") or {}
            session["finished_at"] = now_iso()
            session["elapsed_seconds"] = round(self._elapsed(), 3)
            state["queue"]["session"] = session

        _mutate(self.root, mutate, options=self.options)

    def _elapsed(self) -> float:
        return time.monotonic() - self._started

    # -- scheduling ------------------------------------------------------- #

    def _worker(self) -> None:
        blocked: set[str] = set()
        while True:
            if self._queue_paused():
                return
            job = self._next_candidate(blocked)
            if job is None:
                return
            holder = locks.code_lock(job["code"], home=self.root)
            try:
                holder.__enter__()
            except locks.LockBusy:
                # Another process owns this instrument; leave the job queued.
                self._update(
                    job["id"],
                    last_reason="locked",
                    blocked_at=now_iso(),
                )
                blocked.add(job["id"])
                continue
            try:
                claimed = self._claim(job["id"])
                if claimed is not None:
                    self._execute(claimed)
            except Exception as exc:  # noqa: BLE001 - a worker cannot die silently
                log.exception("queue worker failed")
                self._fail(job["id"], exc, retryable=False)
            finally:
                holder.__exit__(None, None, None)
            blocked.clear()

    def _queue_paused(self) -> bool:
        return bool((_load(self.root)["queue"] or {}).get("paused"))

    def _next_candidate(self, blocked: set[str]) -> dict[str, Any] | None:
        state = _load(self.root)
        if (state["queue"] or {}).get("paused"):
            return None
        candidates = [
            job
            for job in state["jobs"]
            if job.get("status") == _CLAIMABLE and job.get("id") not in blocked
        ]
        candidates.sort(
            key=lambda job: (-int(job.get("priority") or 0), int(job.get("order") or 0))
        )
        return candidates[0] if candidates else None

    def _claim(self, job_id: str) -> dict[str, Any] | None:
        def mutate(state: dict[str, Any]) -> dict[str, Any] | None:
            if (state["queue"] or {}).get("paused"):
                return None
            job = _find(state, job_id)
            if job is None or job.get("status") != _CLAIMABLE:
                return None
            job.update(
                status="running",
                attempts=int(job.get("attempts") or 0) + 1,
                started_at=now_iso(),
                finished_at="",
                error="",
                error_type="",
                next_attempt_at="",
                last_reason="",
            )
            return dict(job)

        return _mutate(self.root, mutate, options=self.options)

    # -- one job ---------------------------------------------------------- #

    def _execute(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        attempt = int(job.get("attempts") or 1)
        while True:
            current = _job(self.root, job_id) or {}
            if current.get("cancel_requested"):
                self._update(
                    job_id, status="cancelled", finished_at=now_iso(), last_reason="cancelled"
                )
                return
            if current.get("pause_requested"):
                self._update(job_id, status="paused", finished_at=now_iso(), last_reason="paused")
                return
            try:
                outcome, decision = self._invoke(job["code"], job["date"])
            except LLMRateLimited as exc:
                wait = float(exc.retry_after) if exc.retry_after else self._retry_delay(attempt)
                if not self._retry(job_id, attempt, exc, wait):
                    return
                attempt += 1
                continue
            except (LLMConfigError, RoleError, ValueError) as exc:
                self._fail(job_id, exc, retryable=False)
                return
            except Exception as exc:  # noqa: BLE001 - retried, then recorded
                if not self._retry(job_id, attempt, exc, self._retry_delay(attempt)):
                    return
                attempt += 1
                continue
            self._complete(job_id, outcome, decision)
            return

    def _invoke(self, code: str, day: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        result = self.runner(code, day)
        if isinstance(result, (tuple, list)) and len(result) == 2:
            state, decision = result
            return (
                state if isinstance(state, Mapping) else {},
                decision if isinstance(decision, Mapping) else {},
            )
        if isinstance(result, Mapping):
            return {}, result
        raise TypeError(
            f"runner 必须返回 (state, decision) 或 decision，收到 {type(result).__name__}"
        )

    def _retry(self, job_id: str, attempt: int, exc: BaseException, wait: float) -> bool:
        if attempt >= self.options.max_attempts:
            self._fail(job_id, exc, retryable=True)
            return False
        wait = max(0.0, min(wait, _MAX_RETRY_SECONDS))
        self._update(
            job_id,
            status="retrying",
            last_reason="retry",
            error=redact(str(exc))[:400],
            error_type=type(exc).__name__,
            next_attempt_at=_stamp_after(wait),
        )
        outcome = self._wait(job_id, wait)
        if outcome == "cancel":
            self._update(job_id, status="cancelled", finished_at=now_iso(), last_reason="cancelled")
            return False
        if outcome == "pause":
            self._update(job_id, status="paused", finished_at=now_iso(), last_reason="paused")
            return False
        if outcome != "ok":
            return False
        self._update(
            job_id,
            status="running",
            attempts=attempt + 1,
            next_attempt_at="",
            started_at=now_iso(),
        )
        return True

    def _wait(self, job_id: str, seconds: float) -> str:
        deadline = time.monotonic() + seconds
        while True:
            current = _job(self.root, job_id)
            if current is None:
                return "gone"
            if current.get("cancel_requested"):
                return "cancel"
            if current.get("pause_requested"):
                return "pause"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "ok"
            time.sleep(min(0.25, remaining))

    def _retry_delay(self, attempt: int) -> float:
        base = max(0.0, float(self.options.retry_delay_seconds))
        return min(_MAX_RETRY_SECONDS, base * (2 ** max(0, attempt - 1)))

    # -- outcomes --------------------------------------------------------- #

    def _complete(
        self,
        job_id: str,
        state: Mapping[str, Any],
        decision: Mapping[str, Any],
    ) -> None:
        raw_budget = state.get("budget")
        budget: Mapping[str, Any] = raw_budget if isinstance(raw_budget, Mapping) else {}
        calls = int(budget.get("calls") or 0)
        input_tokens = int(budget.get("input_tokens") or 0)
        output_tokens = int(budget.get("output_tokens") or 0)
        raw_data = decision.get("data")
        data: Mapping[str, Any] = raw_data if isinstance(raw_data, Mapping) else {}
        summary = {
            "run_id": decision.get("run_id"),
            "rating": decision.get("rating"),
            "action": decision.get("action"),
            "partial": bool(decision.get("partial")),
            "data_status": data.get("status"),
            "quality_score": decision.get("quality_score"),
            "report": decision.get("report"),
        }

        def mutate(current: dict[str, Any]) -> None:
            job = _find(current, job_id)
            if job is None:
                return
            now = now_iso()
            if job.get("cancel_requested"):
                job.update(status="cancelled", finished_at=now, last_reason="cancelled")
                return
            paused = bool(job.get("pause_requested"))
            job.update(
                status="paused" if paused else "completed",
                finished_at=now,
                pause_after_result=paused,
                last_reason="paused" if paused else "done",
                run_id=summary["run_id"],
                rating=summary["rating"],
                partial=summary["partial"],
                data_status=summary["data_status"],
                result=summary,
            )
            session = current["queue"].get("session") or {}
            usage = session.setdefault("usage", _empty_usage())
            usage["jobs"] = int(usage.get("jobs") or 0) + 1
            usage["llm_calls"] = int(usage.get("llm_calls") or 0) + calls
            usage["input_tokens"] = int(usage.get("input_tokens") or 0) + input_tokens
            usage["output_tokens"] = int(usage.get("output_tokens") or 0) + output_tokens
            usage["total_tokens"] = int(usage.get("input_tokens") or 0) + int(
                usage.get("output_tokens") or 0
            )
            session["usage"] = usage
            current["queue"]["session"] = session
            current["queue"]["consecutive_failures"] = 0

        _mutate(self.root, mutate, options=self.options)
        audit("queue.job.completed", job_id=job_id, rating=summary["rating"])
        self._check_limits()

    def _fail(self, job_id: str, exc: BaseException, *, retryable: bool) -> None:
        def mutate(state: dict[str, Any]) -> None:
            job = _find(state, job_id)
            if job is None:
                return
            if job.get("cancel_requested"):
                job.update(status="cancelled", finished_at=now_iso(), last_reason="cancelled")
                return
            job.update(
                status="failed",
                finished_at=now_iso(),
                error=redact(str(exc))[:500],
                error_type=type(exc).__name__,
                retryable=retryable,
                last_reason="failed",
            )
            queue = state["queue"]
            queue["consecutive_failures"] = int(queue.get("consecutive_failures") or 0) + 1

        _mutate(self.root, mutate, options=self.options)
        audit("queue.job.failed", job_id=job_id, error_type=type(exc).__name__)
        self._check_limits()
        if self.options.on_failure == "stop":
            self._trip("on_failure", {"job_id": job_id, "error_type": type(exc).__name__})

    # -- limits ----------------------------------------------------------- #

    def _check_limits(self) -> None:
        state = _load(self.root)
        queue = state["queue"]
        if queue.get("paused"):
            return
        session = queue.get("session") or {}
        usage = session.get("usage") or {}
        calls = int(usage.get("llm_calls") or 0)
        tokens = int(usage.get("total_tokens") or 0)
        elapsed = self._elapsed()

        if calls >= self.options.max_llm_calls and calls > 0:
            self._trip(
                "queue_budget",
                {"limit": "max_llm_calls", "calls": calls, "ceiling": self.options.max_llm_calls},
            )
            return
        if tokens >= self.options.max_total_tokens and tokens > 0:
            self._trip(
                "queue_budget",
                {
                    "limit": "max_total_tokens",
                    "tokens": tokens,
                    "ceiling": self.options.max_total_tokens,
                },
            )
            return
        if elapsed >= self.options.max_wall_clock_seconds:
            self._trip(
                "queue_budget",
                {
                    "limit": "max_wall_clock_seconds",
                    "elapsed_seconds": round(elapsed, 3),
                    "ceiling": self.options.max_wall_clock_seconds,
                },
            )
            return
        failures = int(queue.get("consecutive_failures") or 0)
        if self.options.breaker_enabled and failures >= self.options.failure_threshold:
            self._trip(
                "circuit_breaker",
                {
                    "consecutive_failures": failures,
                    "failure_threshold": self.options.failure_threshold,
                },
            )

    def _trip(self, reason: str, detail: Mapping[str, Any]) -> None:
        def mutate(state: dict[str, Any]) -> dict[str, Any] | None:
            queue = state["queue"]
            if queue.get("paused"):
                return None
            now = now_iso()
            queue.update(paused=True, stop_reason=reason, tripped_at=now)
            for job in state["jobs"]:
                if job.get("status") in {"queued", "retrying"}:
                    job.update(
                        status="paused",
                        pause_reason=reason,
                        paused_at=now,
                        last_reason=reason,
                    )
            session = queue.get("session") or {}
            session["tripped"] = reason
            queue["session"] = session
            return _alert_event(reason, detail, session, self.cfg.get("alert_rules") or {})

        event = _mutate(self.root, mutate, options=self.options)
        if not event:
            return
        section = dict(self.cfg.get("alert_rules") or {})
        delivery = dispatch_alert(event, section, home=self.root)
        audit("queue.tripped", reason=reason, delivery=delivery)

        def remember(state: dict[str, Any]) -> None:
            session = state["queue"].get("session") or {}
            session["alert"] = {"reason": reason, "delivery": delivery}
            state["queue"]["session"] = session

        _mutate(self.root, remember, options=self.options)

    # -- persistence ------------------------------------------------------ #

    def _update(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        def mutate(state: dict[str, Any]) -> dict[str, Any] | None:
            job = _find(state, job_id)
            if job is None:
                return None
            job.update(fields)
            return dict(job)

        return _mutate(self.root, mutate, options=self.options)


def _drain(
    root: Path,
    cfg: Mapping[str, Any],
    options: QueueOptions,
    concurrency: int,
    runner: Runner,
) -> dict[str, Any]:
    drain = _Drain(root, cfg, options, concurrency, runner)
    drain.run()
    return _summary(root, _load(root))


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def _root(home: Path | str | None) -> Path:
    return ensure_home(Path(home).expanduser() if home else None)


def _paths(root: Path) -> tuple[Path, Path, Path]:
    directory = root / "queue"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "state.json", directory / ".state.lock", directory / "state.tmp"


def _load(root: Path) -> dict[str, Any]:
    path, _, _ = _paths(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw_queue = raw.get("queue")
    raw_jobs = raw.get("jobs")
    queue: dict[str, Any] = raw_queue if isinstance(raw_queue, dict) else {}
    jobs: list[Any] = raw_jobs if isinstance(raw_jobs, list) else []
    queue.setdefault("next_order", len(jobs) + 1)
    queue.setdefault("session", {})
    queue.setdefault("paused", False)
    queue.setdefault("stop_reason", "")
    queue.setdefault("last_concurrency", 1)
    queue.setdefault("consecutive_failures", 0)
    return {"version": 1, "queue": queue, "jobs": [job for job in jobs if isinstance(job, dict)]}


def _mutate(
    root: Path,
    callback: Callable[[dict[str, Any]], Any],
    *,
    options: QueueOptions | None = None,
) -> Any:
    path, lock_path, temporary = _paths(root)
    timeout = 30.0 if options is None else max(30.0, options.max_wall_clock_seconds)
    with locks.file_lock(lock_path, timeout=min(timeout, 60.0)):
        state = _load(root)
        result = callback(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _replace(temporary, path)
        return result


def _replace(temporary: Path, path: Path, *, attempts: int = 100, delay: float = 0.02) -> None:
    # On Windows a reader holding the state file open makes MoveFileEx fail;
    # writes are already serialised by the state lock, so retrying the swap is
    # safe and every reader still sees a complete file.
    for attempt in range(1, attempts + 1):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(delay)


def _find(state: Mapping[str, Any], job_id: str) -> dict[str, Any] | None:
    for job in state.get("jobs") or []:
        if job.get("id") == job_id:
            return job
    return None


def _job(root: Path, job_id: str) -> dict[str, Any] | None:
    return _find(_load(root), job_id)


def _empty_usage() -> dict[str, int]:
    return {
        "jobs": 0,
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def _recover_stale(state: dict[str, Any], stale_seconds: float) -> int:
    cutoff = datetime.now() - timedelta(seconds=max(0.0, stale_seconds))
    recovered = 0
    for job in state["jobs"]:
        if job.get("status") not in {"running", "retrying"}:
            continue
        started = _parse(job.get("started_at")) or _parse(job.get("submitted_at"))
        if started is not None and started >= cutoff:
            continue
        job.update(
            status="queued",
            last_reason="recovered_stale_running",
            cancel_requested=False,
            pause_requested=False,
        )
        recovered += 1
    return recovered


def _summary(root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    jobs = [dict(job) for job in state.get("jobs") or []]
    counts: dict[str, int] = {}
    for job in jobs:
        key = str(job.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    queue = dict(state.get("queue") or {})
    session = dict(queue.get("session") or {})
    started = _parse(session.get("started_at"))
    if started and not session.get("finished_at"):
        session["elapsed_seconds"] = round((datetime.now() - started).total_seconds(), 3)
    queue["session"] = session
    return {
        "path": str(_paths(root)[0]),
        "queue": queue,
        "total": len(jobs),
        "counts": counts,
        "jobs": jobs,
    }


def _alert_event(
    reason: str,
    detail: Mapping[str, Any],
    session: Mapping[str, Any],
    section: Mapping[str, Any],
) -> dict[str, Any] | None:
    from stock_senate.alerts.channels import _SENDERS  # noqa: PLC0415 - avoid a cycle

    section = dict(section)
    configured = dict(section.get("channels") or {})
    channels = [
        name
        for name in configured
        if name in _SENDERS and dict(configured.get(name) or {}).get("enabled", True)
    ]
    if not channels:
        return None
    titles = {
        "queue_budget": "队列预算熔断",
        "circuit_breaker": "队列熔断器打开",
        "on_failure": "队列失败即停",
    }
    title = titles.get(reason, reason)
    usage = dict(session.get("usage") or {})
    seed = f"{reason}|{session.get('id')}|{len(detail)}"
    return {
        "id": hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32],
        "rule": {"id": f"queue_{reason}", "name": title},
        "level": "high",
        "code": "",
        "as_of": date.today().isoformat(),
        "status": "triggered",
        "observed": {"reason": reason, **dict(detail), "usage": usage},
        "message": f"[high] {title}：{dict(detail)}；本次队列已调用 {usage.get('llm_calls', 0)} 次",
        "channels": channels,
        "created_at": now_iso(),
        "disclaimer": ALERT_DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# CSV / options
# --------------------------------------------------------------------------- #


def _read_rows(path: Path, default_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [{"row": 0, "error": f"无法读取 CSV：{exc}"}]
    reader = csv.reader(io.StringIO(text))
    raw = [row for row in reader if any(str(cell).strip() for cell in row)]
    if not raw:
        return [], [{"row": 0, "error": "CSV 为空"}]

    header = [str(cell).strip().lower() for cell in raw[0]]
    if any(name in header for name in _CODE_KEYS):
        index = {name: position for position, name in enumerate(header)}
        body = raw[1:]
        first = 2
    else:
        index = {"code": 0, "date": 1, "priority": 2}
        body = raw
        first = 1

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for offset, row in enumerate(body):
        number = first + offset
        code = _cell(row, index, _CODE_KEYS)
        if not code:
            errors.append({"row": number, "error": "缺少 code"})
            continue
        day = _cell(row, index, _DATE_KEYS) or default_date
        if not _DATE.match(day):
            errors.append(
                {"row": number, "code": code, "error": f"日期必须是 YYYY-MM-DD，收到 {day!r}"}
            )
            continue
        priority_text = _cell(row, index, _PRIORITY_KEYS)
        try:
            priority = int(float(priority_text)) if priority_text else 0
        except ValueError:
            errors.append(
                {
                    "row": number,
                    "code": code,
                    "error": f"priority 必须是整数，收到 {priority_text!r}",
                }
            )
            continue
        rows.append({"code": code, "date": day, "priority": priority})
    return rows, errors


def _cell(row: Iterable[str], index: Mapping[str, int], keys: Iterable[str]) -> str:
    cells = list(row)
    for key in keys:
        position = index.get(key)
        if position is not None and position < len(cells):
            value = str(cells[position]).strip()
            if value:
                return value
    return ""


def _row_ref(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"code": row.get("code"), "date": row.get("date")}


def _options(cfg: Mapping[str, Any]) -> QueueOptions:
    workflow = dict(cfg.get("workflow") or {})
    queue_cfg = dict(workflow.get("queue") or {})
    budget = dict(cfg.get("budget") or {})
    per_queue = dict(budget.get("per_queue") or {})
    breaker = dict(budget.get("circuit_breaker") or {})
    llm = dict((cfg.get("llm_config") or {}).get("llm") or {})
    return QueueOptions(
        max_attempts=max(1, int(queue_cfg.get("max_attempts", 3) or 3)),
        retry_delay_seconds=max(
            0.0, float(queue_cfg.get("retry_delay_seconds", llm.get("llm_retry_delay", 5)) or 0)
        ),
        on_failure=str(queue_cfg.get("on_failure") or "skip"),
        max_llm_calls=int(per_queue.get("max_llm_calls", 1200) or 1200),
        max_total_tokens=int(per_queue.get("max_total_tokens", 8_000_000) or 8_000_000),
        max_wall_clock_seconds=float(per_queue.get("max_wall_clock_seconds", 28_800) or 28_800),
        stale_running_seconds=float(queue_cfg.get("stale_running_seconds", 21_600) or 21_600),
        breaker_enabled=bool(breaker.get("enabled", True)),
        failure_threshold=max(1, int(breaker.get("failure_threshold", 5) or 5)),
    )


def _concurrency(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 1
    return max(1, min(number, _MAX_CONCURRENCY))


def _today(now: datetime | None) -> str:
    return date_only((now or datetime.now()).date().isoformat())


def _stamp_after(seconds: float) -> str:
    moment = datetime.now() + timedelta(seconds=max(0.0, seconds))
    return moment.isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _graph_runner(cfg: Mapping[str, Any]) -> Runner:
    from stock_senate.graph import SenateGraph

    graph = SenateGraph(config=dict(cfg))

    def run(code: str, day: str) -> Any:
        return graph.propagate(code, day, hitl=True)

    return run


__all__ = ["QueueOptions", "cancel", "pause", "resume", "status", "submit_file"]
