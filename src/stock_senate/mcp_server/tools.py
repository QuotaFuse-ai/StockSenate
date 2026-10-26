"""The five ``stock_senate_*`` MCP tools.

Each tool is a plain function over its arguments plus the loaded configuration
and the run home. Failures are reported *inside* the returned mapping, so a
client sees a readable reason instead of a transport-level error.

Every result is JSON-serialisable, never contains an executable price, a
position size or an order, and never echoes a credential: ``config_query``
passes its payload through :func:`stock_senate.observability.logging.redact`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from stock_senate.config.loader import ensure_home, load_config
from stock_senate.observability.logging import redact

DISCLAIMER = "研究结果，不构成投资建议，也不包含可执行价位、仓位或交易指令。"

CONFIG_SECTIONS: tuple[str, ...] = (
    "agent_roles",
    "workflow",
    "output_template",
    "data_source",
    "llm_config",
    "alert_rules",
    "budget",
)

_ENVELOPE_FIELDS = (
    "domain",
    "code",
    "data_as_of",
    "source",
    "source_detail",
    "fetched_at",
    "freshness",
    "confidence",
    "data_source_status",
)


def _error(exc: BaseException) -> dict[str, Any]:
    from stock_senate.roles.llm import LLMRateLimited, LLMUnavailable

    if isinstance(exc, LLMRateLimited):
        kind = "rate_limited"
    elif isinstance(exc, LLMUnavailable):
        kind = "llm_unavailable"
    elif isinstance(exc, (FileNotFoundError, LookupError)):
        kind = "not_found"
    elif isinstance(exc, (ValueError, TypeError)):
        kind = "invalid_request"
    else:
        kind = "tool_error"
    error: dict[str, Any] = {
        "kind": kind,
        "type": type(exc).__name__,
        "reason": str(redact(str(exc), mask_paths=True)),
    }
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        error["retry_after"] = retry_after
    return {"ok": False, "error": error, "disclaimer": DISCLAIMER}


def _root(loaded: Mapping[str, Any], home: Path | str | None) -> Path:
    configured = home if home is not None else str(loaded.get("home") or "").strip() or None
    return ensure_home(Path(configured).expanduser() if configured else None)


def _today() -> str:
    return date_cls.today().isoformat()


def _value(raw: Any) -> Any:
    return getattr(raw, "value", raw)


def _out(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask credentials and absolute local paths at the MCP boundary."""
    return redact(payload, mask_paths=True)


def _envelope_row(raw: Any) -> dict[str, Any]:
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, Mapping):
        return {"detail": str(raw)}
    row = {key: _value(raw.get(key)) for key in _ENVELOPE_FIELDS if key in raw}
    row["usable"] = raw.get("data_source_status") not in {"missing", "SourceStatus.MISSING"}
    return row


def _envelopes(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    domains = ((state.get("data") or {}).get("domains")) or {}
    return {str(name): _envelope_row(raw) for name, raw in domains.items()}


def _targets(codes: Any) -> list[str]:
    if codes is None:
        return []
    if isinstance(codes, str):
        parts: Iterable[Any] = codes.replace(";", ",").split(",")
    else:
        parts = codes
    return [str(item).strip() for item in parts if str(item).strip()]


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


def analyse(
    code: str,
    date: str | None = None,
    hitl: bool = True,
    *,
    config: Any = None,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Run the full senate for one code and return the decision plus data coverage."""
    from stock_senate.graph.senate_graph import SenateGraph

    try:
        graph = SenateGraph(config=config)
        state, decision = graph.propagate(code, date or _today(), hitl=bool(hitl))
    except Exception as exc:  # noqa: BLE001 - reported as a structured tool error
        return _error(exc)

    files = dict(decision.get("report") or {})
    return _out({
        "ok": True,
        "run_id": decision.get("run_id"),
        "trace_id": decision.get("trace_id"),
        "code": decision.get("code"),
        "date": decision.get("date"),
        "decision": decision,
        "report_path": files.get("report"),
        "report_files": files,
        "data_envelopes": _envelopes(state),
        "data": decision.get("data"),
        "partial": bool(decision.get("partial")),
        "disclaimer": decision.get("disclaimer") or DISCLAIMER,
    })


def backtest(
    codes: list[str],
    start: str,
    end: str,
    window: int = 5,
    slippage: float = 0.0,
    fee: float = 0.0,
    *,
    config: Any = None,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Replay past sessions and return simulated metrics plus the artifact reference."""
    from stock_senate.backtest import engine as backtest_engine

    targets = _targets(codes)
    if not targets:
        return _error(ValueError("backtest 需要至少一个标的代码"))
    try:
        loaded = load_config(config)
        root = _root(loaded, home)
        result = backtest_engine.run(
            codes=targets,
            start=start,
            end=end,
            window=int(window),
            slippage=float(slippage),
            fee=float(fee),
            config=loaded,
            home=root,
        )
    except Exception as exc:  # noqa: BLE001 - reported as a structured tool error
        return _error(exc)

    artifacts = dict(result.get("artifacts") or {})
    return _out({
        "ok": True,
        "mode": "backtest",
        "simulation": True,
        "run_id": result.get("run_key"),
        "codes": targets,
        "start": result.get("start"),
        "end": result.get("end"),
        "metrics": result.get("metrics"),
        "per_code": result.get("per_code"),
        "data": result.get("data"),
        "partial": bool(result.get("partial")),
        "equity_curve_ref": artifacts.get("sqlite"),
        "artifacts": artifacts,
        "disclaimer": result.get("disclaimer") or DISCLAIMER,
    })


def alert_query(
    code: str | None = None,
    level: str | None = None,
    since: str | None = None,
    *,
    home: Path | str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Read delivered alerts from the local alert log, newest last."""
    from stock_senate.alerts.channels import webui as webui_channel

    try:
        loaded = load_config(None)
        root = _root(loaded, home)
        rows = webui_channel.recent(max(1, int(limit)), home=root)
    except Exception as exc:  # noqa: BLE001 - reported as a structured tool error
        return _error(exc)

    wanted_code = str(code).strip() if code else ""
    wanted_level = str(level).strip().lower() if level else ""
    floor = str(since).strip() if since else ""
    matched: list[dict[str, Any]] = []
    for row in rows:
        if wanted_code and str(row.get("code")) != wanted_code:
            continue
        if wanted_level and str(row.get("level") or "").lower() != wanted_level:
            continue
        if floor and str(row.get("created_at") or row.get("as_of") or "")[:10] < floor:
            continue
        matched.append(row)

    return _out({
        "ok": True,
        "alerts": matched,
        "count": len(matched),
        "log": str(webui_channel.alerts_log(root)),
        "disclaimer": DISCLAIMER,
    })


def report_query(
    run_id: str | None = None,
    code: str | None = None,
    date: str | None = None,
    *,
    config: Any = None,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Return a saved snapshot as structured sections, by ``run_id`` or ``code+date``."""
    from stock_senate.report import store
    from stock_senate.report.render import render_summary

    try:
        loaded = load_config(config)
        root = _root(loaded, home)
        if run_id:
            state = store.load(str(run_id), home=root)
        elif code and date:
            candidates = [
                row
                for row in store.list_runs(code=str(code), home=root)
                if str(row.get("date")) == str(date)
            ]
            if not candidates:
                raise store.ReportNotFound(f"没有找到 {code} 在 {date} 的报告快照")
            state = store.load(str(candidates[0]["run_id"]), home=root)
        else:
            raise ValueError("report_query 需要 run_id，或同时提供 code 与 date")
    except Exception as exc:  # noqa: BLE001 - reported as a structured tool error
        return _error(exc)

    files = dict(state.get("report") or {})
    return _out({
        "ok": True,
        "run_id": state.get("run_id"),
        "summary": render_summary(state),
        "sections": {
            "analysts": list(state.get("analysts") or []),
            "debate": dict(state.get("debate") or {}),
            "research": dict(state.get("research") or {}),
            "trade": dict(state.get("trade") or {}),
            "risk": dict(state.get("risk") or {}),
            "decision": dict(state.get("decision") or {}),
            "quality": dict(state.get("quality") or {}),
            "hitl": dict(state.get("hitl") or {}),
        },
        "report_path": files.get("report"),
        "disclaimer": DISCLAIMER,
    })


def config_query(
    section: str | None = None,
    *,
    config: Any = None,
) -> dict[str, Any]:
    """Return the effective configuration with all credentials masked."""
    try:
        loaded = load_config(config)
    except Exception as exc:  # noqa: BLE001 - reported as a structured tool error
        return _error(exc)

    if section:
        key = str(section).strip()
        if key not in CONFIG_SECTIONS:
            return {
                "ok": False,
                "error": {
                    "kind": "invalid_request",
                    "type": "ValueError",
                    "reason": f"未知配置段 {section!r}",
                    "available": list(CONFIG_SECTIONS),
                },
                "disclaimer": DISCLAIMER,
            }
        payload = {key: loaded.get(key)}
    else:
        payload = {key: loaded.get(key) for key in CONFIG_SECTIONS}

    return {
        "ok": True,
        "section": section or "all",
        "config": redact(payload, mask_paths=True),
        "config_hash": loaded.get("config_hash"),
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "CONFIG_SECTIONS",
    "DISCLAIMER",
    "alert_query",
    "analyse",
    "backtest",
    "config_query",
    "report_query",
]
