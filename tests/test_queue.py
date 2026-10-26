"""Batch queue: concurrency, same-code serialisation, retry and budget trip.

The traceability row for ``queue/manager.py`` is accepted on "50 jobs at
concurrency 3", and ``queue/locks.py`` on "same instrument is never analysed
twice at once"; both are exercised here with a stub runner so the tests do not
depend on a model provider or the network.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from stock_senate.config.loader import load_config
from stock_senate.queue import manager
from stock_senate.roles.llm import LLMRateLimited

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config")


def decision(day: str, *, calls: int = 0) -> dict:
    return {
        "run_id": f"run-{day}",
        "rating": "Neutral",
        "action": "hold",
        "execution": False,
        "partial": False,
        "quality_score": 80,
        "report": f"report-{day}",
        "data": {"status": "ok"},
        "budget": {"calls": calls},
    }


class Probe:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.per_code: dict[str, int] = {}
        self.max_per_code: dict[str, int] = {}
        self.order: list[str] = []

    def enter(self, code: str) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.per_code[code] = self.per_code.get(code, 0) + 1
            self.max_per_code[code] = max(self.max_per_code.get(code, 0), self.per_code[code])
            self.order.append(code)

    def leave(self, code: str) -> None:
        with self.lock:
            self.active -= 1
            self.per_code[code] -= 1


def write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text("\n".join(["code,date,priority", *rows]) + "\n", encoding="utf-8")
    return path


def test_fifty_jobs_run_three_at_a_time(tmp_path: Path) -> None:
    probe = Probe()

    def runner(code: str, day: str):
        probe.enter(code)
        try:
            time.sleep(0.08)
            return {"budget": {"calls": 1}}, decision(f"{code}-{day}")
        finally:
            probe.leave(code)

    rows = [f"{600000 + i},2026-10-01,0" for i in range(50)]
    result = manager.submit_file(
        write_csv(tmp_path / "fifty.csv", rows),
        concurrency=3,
        home=tmp_path,
        config=CONFIG,
        runner=runner,
    )

    assert result["submitted"] == 50
    assert result["counts"] == {"completed": 50}
    assert probe.max_active == 3


def test_priority_duplicates_and_invalid_rows(tmp_path: Path) -> None:
    probe = Probe()

    def runner(code: str, day: str):
        probe.enter(code)
        probe.leave(code)
        return {}, decision(day)

    source = write_csv(
        tmp_path / "prio.csv",
        [
            "600001,2026-10-01,0",
            "600002,2026-10-01,9",
            "600001,2026-10-01,0",
            "600003,2026-10-01,5",
            "bad,not-a-date,0",
        ],
    )
    result = manager.submit_file(
        source, concurrency=1, home=tmp_path, config=CONFIG, runner=runner
    )

    assert probe.order == ["600002", "600003", "600001"]
    assert [row["reason"] for row in result["skipped"]] == ["duplicate_in_file"]
    assert result["errors"][0]["row"] == 6
    assert result["counts"] == {"completed": 3}


def test_same_code_is_never_analysed_twice_at_once(tmp_path: Path) -> None:
    probe = Probe()

    def runner(code: str, day: str):
        probe.enter(code)
        try:
            time.sleep(0.01)
            return {}, decision(day)
        finally:
            probe.leave(code)

    rows = [f"600519,2026-10-0{i},0" for i in range(1, 7)]
    result = manager.submit_file(
        write_csv(tmp_path / "same.csv", rows),
        concurrency=3,
        home=tmp_path,
        config=CONFIG,
        runner=runner,
    )

    assert result["counts"] == {"completed": 6}
    assert probe.max_per_code["600519"] == 1


def test_pause_resume_cancel(tmp_path: Path) -> None:
    source = write_csv(tmp_path / "control.csv", ["600010,2026-10-01,0", "600011,2026-10-01,0"])
    queued = manager.submit_file(
        source, concurrency=1, home=tmp_path, config=CONFIG, runner=lambda c, d: {}, run=False
    )
    first, second = queued["job_ids"]
    assert queued["counts"] == {"queued": 2}

    assert manager.pause(first, home=tmp_path)["status"] == "paused"
    assert manager.pause(first, home=tmp_path)["reason"] == "already_paused"
    assert manager.cancel(second, home=tmp_path)["status"] == "cancelled"

    resumed = manager.resume(
        first, home=tmp_path, config=CONFIG, runner=lambda code, day: ({}, decision(day))
    )
    assert resumed["reason"] == "requeued"
    assert manager.status(first, home=tmp_path)["job"]["status"] == "completed"
    assert manager.cancel(first, home=tmp_path)["reason"] == "terminal"
    assert manager.pause("missing", home=tmp_path)["reason"] == "unknown_job"


def test_rate_limit_is_retried_then_completes(tmp_path: Path) -> None:
    calls: list[int] = []
    lock = threading.Lock()

    def runner(code: str, day: str):
        with lock:
            calls.append(1)
            attempt = len(calls)
        if attempt == 1:
            raise LLMRateLimited("slow down", retry_after=0.01)
        return {}, decision(day)

    result = manager.submit_file(
        write_csv(tmp_path / "rate.csv", ["600020,2026-10-01,0"]),
        concurrency=1,
        home=tmp_path,
        config=CONFIG,
        runner=runner,
    )

    job = manager.status(result["job_ids"][0], home=tmp_path)["job"]
    assert job["status"] == "completed"
    assert job["attempts"] == 2


def test_failures_are_skipped_and_counted(tmp_path: Path) -> None:
    def runner(code: str, day: str):
        raise ValueError("bad payload")

    result = manager.submit_file(
        write_csv(tmp_path / "fail.csv", ["600030,2026-10-01,0", "600031,2026-10-01,0"]),
        concurrency=1,
        home=tmp_path,
        config=CONFIG,
        runner=runner,
    )

    assert result["counts"] == {"failed": 2}
    assert manager.status(home=tmp_path)["queue"]["consecutive_failures"] == 2
    assert result["jobs"][0]["error_type"] == "ValueError"
    assert result["jobs"][0]["retryable"] is False


def test_budget_trip_pauses_queue_and_dispatches_alert(tmp_path: Path) -> None:
    config = copy.deepcopy(CONFIG)
    config["budget"]["per_queue"]["max_llm_calls"] = 1
    config["alert_rules"]["channels"]["webui"]["enabled"] = True
    config["alert_rules"]["channels"]["console"]["enabled"] = False

    def runner(code: str, day: str):
        return {"budget": {"calls": 1}}, decision(day, calls=1)

    result = manager.submit_file(
        write_csv(
            tmp_path / "trip.csv",
            ["600040,2026-10-01,0", "600041,2026-10-01,0", "600042,2026-10-01,0"],
        ),
        concurrency=1,
        home=tmp_path,
        config=config,
        runner=runner,
    )

    state = manager.status(home=tmp_path)
    assert state["queue"]["paused"] is True
    assert state["queue"]["stop_reason"] == "queue_budget"
    assert result["counts"] == {"completed": 1, "paused": 2}
    assert [row["status"] for row in state["jobs"]].count("paused") == 2

    log = tmp_path / "alerts" / "alerts.jsonl"
    event = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert event["rule"]["id"] == "queue_queue_budget"
    assert event["status"] == "triggered"
    assert "disclaimer" in event
    assert state["queue"]["session"]["alert"]["reason"] == "queue_budget"


def test_cli_queue_status_reads_the_home_queue(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["STOCKSENATE_HOME"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "stock_senate", "queue", "status", "--output", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["counts"] == {}
    assert payload["queue"]["paused"] is False
