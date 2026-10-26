"""Run snapshots on disk: ``<home>/reports/<run_id>/``.

Each run writes three files:

* ``summary.json``  — the machine-readable conclusion,
* ``complete_report.md`` — the rendered five-section report plus data appendix,
* ``state.json``    — the full snapshot, from which the markdown can be
  re-rendered without a model or a provider.

``run_id`` reaches this module from the CLI, so it is validated before it is
joined to a path; a snapshot can only ever live one level below ``reports``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from stock_senate.config.loader import home_dir
from stock_senate.report.render import render_markdown, render_summary

_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_RUN_FILES = ("state.json", "summary.json", "complete_report.md")


class ReportNotFound(FileNotFoundError):
    """The requested snapshot does not exist."""


def reports_root(home: Path | str | None = None) -> Path:
    root = Path(home).expanduser() if home else home_dir()
    return root / "reports"


def _check_run_id(run_id: str) -> str:
    text = str(run_id or "").strip()
    if not _RUN_ID.match(text) or text in {".", ".."}:
        raise ValueError(f"非法 run_id: {run_id!r}")
    return text


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def save_run(
    state: Mapping[str, Any],
    *,
    home: Path | str | None = None,
    template: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    run_id = _check_run_id(str(state.get("run_id") or ""))
    directory = reports_root(home) / run_id
    directory.mkdir(parents=True, exist_ok=True)

    summary = render_summary(state)
    markdown = render_markdown(state, template)
    summary["files"] = {
        "state": str(directory / "state.json"),
        "summary": str(directory / "summary.json"),
        "report": str(directory / "complete_report.md"),
    }

    _write_json(directory / "state.json", dict(state))
    (directory / "complete_report.md").write_text(markdown, encoding="utf-8")
    _write_json(directory / "summary.json", summary)
    return dict(summary["files"])


def run_dir(run_id: str, *, home: Path | str | None = None) -> Path:
    return reports_root(home) / _check_run_id(run_id)


def list_runs(
    code: str | None = None,
    *,
    home: Path | str | None = None,
    limit: int | None = None,
    newest_first: bool = True,
) -> list[dict[str, Any]]:
    root = reports_root(home)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for directory in root.iterdir():
        if not directory.is_dir() or not _RUN_ID.match(directory.name):
            continue
        summary_path = directory / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if code and str(summary.get("code")) != str(code):
            continue
        summary["run_id"] = summary.get("run_id") or directory.name
        summary["path"] = str(directory)
        rows.append(summary)
    rows.sort(key=lambda item: (str(item.get("date") or ""), str(item.get("generated_at") or "")),
              reverse=newest_first)
    return rows[:limit] if limit else rows


def load(run_id: str, *, home: Path | str | None = None) -> dict[str, Any]:
    directory = run_dir(run_id, home=home)
    state_path = directory / "state.json"
    if not state_path.is_file():
        raise ReportNotFound(f"找不到报告快照 {run_id}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["path"] = str(directory)
    return state


def _pick_run(code: str, day: str, *, home: Path | str | None) -> dict[str, Any]:
    """Newest snapshot of ``code`` dated exactly ``day``."""
    candidates = [
        row
        for row in list_runs(code=code, home=home)
        if str(row.get("date")) == str(day)
    ]
    if not candidates:
        raise ReportNotFound(f"没有找到 {code} 在 {day} 的报告快照")
    return candidates[0]


def diff(
    code: str,
    from_date: str,
    to_date: str,
    *,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Field-level diff between the latest run of two dates."""
    from stock_senate.report.diff import diff_states

    before_row = _pick_run(code, from_date, home=home)
    after_row = _pick_run(code, to_date, home=home)
    before = load(str(before_row["run_id"]), home=home)
    after = load(str(after_row["run_id"]), home=home)
    payload = diff_states(before, after)
    payload["code"] = code
    return payload


def render(
    run_id: str,
    *,
    home: Path | str | None = None,
    template: Mapping[str, Any] | None = None,
    compact: bool = False,
) -> str:
    return render_markdown(load(run_id, home=home), template, compact=compact)


def export(
    run_id: str,
    fmt: str = "md",
    *,
    out_path: Path | str | None = None,
    home: Path | str | None = None,
    template: Mapping[str, Any] | None = None,
) -> Path:
    """Write an export and return its path.

    ``pdf``/``docx`` need optional extras; when they are absent the export
    degrades to markdown and a line in the file says so, instead of failing.
    """
    directory = run_dir(run_id, home=home)
    if not (directory / "state.json").is_file():
        raise ReportNotFound(f"找不到报告快照 {run_id}")
    state = load(run_id, home=home)
    kind = str(fmt or "md").lower()

    if kind == "json":
        path = Path(out_path).expanduser() if out_path else directory / "summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, render_summary(state))
        return path

    if kind not in {"md", "markdown", "pdf", "docx"}:
        raise ValueError(f"不支持的导出格式: {fmt}")

    text = render_markdown(state, template)
    if kind in {"pdf", "docx"}:
        text += f"\n\n> 提示：{kind} 导出依赖可选 extra，当前未安装，已降级为 Markdown。\n"
    path = Path(out_path).expanduser() if out_path else directory / f"export.{'md'}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


__all__ = [
    "ReportNotFound",
    "diff",
    "export",
    "list_runs",
    "load",
    "render",
    "reports_root",
    "run_dir",
    "save_run",
]
