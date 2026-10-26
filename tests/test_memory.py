"""Long-term reflection memory: what survives a run, and what never does.

The contract these tests pin:

* a finished run becomes one compact reflection - rating, stance, quality and
  the lessons the next run should act on;
* nothing executable survives the write: price levels and order wording are
  filtered at the memory boundary, not only in reports;
* the store is append-only JSONL, filtered by code, bounded by ``max_entries``
  and tolerant of corrupt lines and a missing file;
* disabling memory makes both reads and writes no-ops;
* the second analysis of the same code receives the first run's reflection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stock_senate import SenateGraph
from stock_senate.workflow.memory import (
    MemoryStore,
    build_reflection,
    injectable,
    memory_settings,
)

CODE = "600519"
DAY = "2026-10-24"


def _decision(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "rating": "hold",
        "confidence": 0.62,
        "partial": False,
        "low_quality": False,
        "reasons": ["营收增速与现金流匹配。"],
        "risks": ["行业需求仍有波动。"],
    }
    base.update(overrides)
    return base


def _reflection(**overrides: Any) -> dict[str, Any]:
    payload = build_reflection(
        code=CODE,
        date=DAY,
        run_id="2026-10-24-600519-abcdef12",
        decision=_decision(),
        quality={"score": 82.0, "low_quality": False},
        research={"stance": "中性偏多"},
        trade={"direction": "观望"},
        hitl={"decision": "auto_approved"},
    )
    payload.update(overrides)
    return payload


def test_reflection_summarises_the_run() -> None:
    record = _reflection()

    assert record["code"] == CODE
    assert record["date"] == DAY
    assert record["rating"] == "hold"
    assert record["stance"] == "中性偏多"
    assert record["direction"] == "观望"
    assert record["confidence"] == 0.62
    assert record["quality_score"] == 82.0
    assert record["low_quality"] is False
    assert record["partial"] is False
    assert record["hitl"] == "auto_approved"
    assert record["lessons"] == []


def test_reflection_turns_weak_runs_into_lessons() -> None:
    record = build_reflection(
        code=CODE,
        date=DAY,
        run_id="run-1",
        decision=_decision(rating="unknown", partial=True, low_quality=True, risks=[]),
        quality={"score": 41.0, "low_quality": True},
        research={"stance": ""},
        trade={"direction": ""},
    )

    assert record["rating"] == "unknown"
    assert record["stance"] == "unknown"
    assert record["direction"] == "unknown"
    assert record["low_quality"] is True
    assert record["partial"] is True
    assert len(record["lessons"]) == 3
    assert any("关键数据域" in item for item in record["lessons"])
    assert any("缺失证据" in item for item in record["lessons"])
    assert any("跳过角色" in item for item in record["lessons"])


def test_memory_never_stores_executable_prices() -> None:
    record = build_reflection(
        code=CODE,
        date=DAY,
        run_id="run-1",
        decision=_decision(
            reasons=["营收增速与现金流匹配。目标价 12.00 元。"],
            risks=["行业需求仍有波动。止损位 9.50 元。", "仓位建议 30%。"],
        ),
        quality={"score": 82.0, "low_quality": False},
        research={"stance": "中性偏多"},
        trade={"direction": "观望"},
    )

    assert "目标价" not in record["reasons"][0]
    assert "营收增速与现金流匹配。" in record["reasons"][0]
    assert record["risks"] == ["行业需求仍有波动。"]
    assert all("仓位建议" not in item for item in record["risks"])


def test_store_round_trips_and_filters_by_code(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, {"max_entries": 2})
    store.remember(_reflection(date="2026-10-20"))
    store.remember(_reflection(date="2026-10-21"))
    store.remember(_reflection(date="2026-10-22"))
    store.remember({"code": "000001", "date": "2026-10-23", "rating": "buy"})

    entries = store.recent(code=CODE)

    assert [entry["date"] for entry in entries] == ["2026-10-21", "2026-10-22"]
    assert all(entry["code"] == CODE for entry in entries)
    assert [entry["rating"] for entry in store.recent(code="000001")] == ["buy"]
    assert store.path == tmp_path / "memory" / "reflections.jsonl"


def test_corrupt_lines_are_skipped_and_a_missing_file_is_empty(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.recent(code=CODE) == []

    store.remember(_reflection(date="2026-10-20"))
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
        handle.write("[1, 2, 3]\n")
        handle.write("\n")
    store.remember(_reflection(date="2026-10-22"))

    assert [entry["date"] for entry in store.recent(code=CODE)] == ["2026-10-20", "2026-10-22"]


def test_disabled_memory_neither_reads_nor_writes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, {"enabled": False})

    assert store.enabled is False
    assert store.remember(_reflection()) is False
    assert store.recent(code=CODE) == []
    assert not store.path.exists()


def test_a_failed_write_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "memory"
    blocker.write_text("not a directory", encoding="utf-8")

    assert MemoryStore(tmp_path).remember(_reflection()) is False


def test_injectable_projects_the_prompt_shape() -> None:
    entries = [
        _reflection(
            date=f"2026-10-2{i}",
            lessons=["a", "b", "c"],
        )
        for i in range(4)
    ]

    projected = injectable(entries, 2)

    assert [item["date"] for item in projected] == ["2026-10-22", "2026-10-23"]
    assert set(projected[0]) == {
        "date",
        "rating",
        "stance",
        "quality_score",
        "low_quality",
        "lessons",
    }
    assert projected[0]["lessons"] == ["a", "b"]


def test_memory_settings_read_the_workflow_section() -> None:
    config = {"workflow": {"memory": {"enabled": False, "max_entries": 2}}}

    assert memory_settings(config) == {"enabled": False, "max_entries": 2}
    assert memory_settings(None) == {}


@pytest.fixture()
def offline_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("STOCKSENATE_HOME", str(home))
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    return home


def test_a_second_run_receives_the_first_runs_reflection(offline_home: Path) -> None:
    graph = SenateGraph()

    first, _ = graph.propagate(CODE, DAY, hitl=False)
    assert first["memory"] == {"enabled": True, "injected": 0, "stored": True}

    second, _ = graph.propagate(CODE, DAY, hitl=False)
    assert second["memory"] == {"enabled": True, "injected": 1, "stored": True}

    lines = (offline_home / "memory" / "reflections.jsonl").read_text(encoding="utf-8")
    stored = [json.loads(line) for line in lines.splitlines() if line.strip()]
    assert [entry["run_id"] for entry in stored] == [first["run_id"], second["run_id"]]
