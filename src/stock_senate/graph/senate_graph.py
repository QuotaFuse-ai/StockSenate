"""The real multi-role research pipeline.

``SenateGraph.propagate(code, date)`` keeps a stable contract: it returns
``(final_state, decision)``. Report files are a derived artifact of the same
run; nothing in the decision depends on them existing.

Order is fixed by ``config/workflow.toml``: analysts → bull/bear debate →
research manager → trader → risk debate → portfolio manager, with the quality
gate scoring the finished chain. HITL checkpoints sit after the trader and
before the final verdict and are recorded even when skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from stock_senate.config.loader import ensure_home, load_config
from stock_senate.data.codes import normalize_code
from stock_senate.data.envelope import date_only
from stock_senate.data.router import DataAccessLayer, summarize
from stock_senate.observability.logging import new_trace_id, redact
from stock_senate.roles.llm import LLMConfigError, LLMRateLimited
from stock_senate.roles.registry import RoleError, RoleRegistry
from stock_senate.roles.schemas import FinalDecision
from stock_senate.roles.tool_gate import ToolGate
from stock_senate.workflow.budget import budget_from_config
from stock_senate.workflow.context import RunContext
from stock_senate.workflow.digest import build_digest
from stock_senate.workflow.hitl import HitlReviewer, hitl_blocked
from stock_senate.workflow.nodes import (
    NodeConfigError,
    run_analysts,
    run_bull_bear_debate,
    run_portfolio_manager,
    run_quality_gate,
    run_research_manager,
    run_risk_debate,
    run_trader,
)

log = logging.getLogger("stock_senate.graph")

ProgressCallback = Callable[[str, str, Mapping[str, Any]], None]

_RATING_ACTION = {
    "buy": "买入",
    "overweight": "增持",
    "hold": "持有",
    "underweight": "减持",
    "sell": "卖出",
    "unknown": "未知",
}


class SenateGraph:
    """Multi-role research graph.

    Parameters
    ----------
    debug:
        Emit node-level progress to stderr.
    config:
        Overrides merged over the packaged defaults and the user directory.
        A directory path, the mapping produced by ``cli._load_config_dir`` and
        a partial override dict are all accepted.
    """

    def __init__(self, debug: bool = False, config: Any = None) -> None:
        self.debug = debug
        self.config: dict[str, Any] = load_config(config)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def propagate(
        self,
        code: str,
        date: str,
        *,
        hitl: bool = True,
        overrides: Mapping[str, Any] | None = None,
        progress: ProgressCallback | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run the full graph for ``code`` as of ``date``.

        ``hitl=False`` records every checkpoint as skipped; timeouts fall back
        to ``auto_approve`` and are recorded as ``auto_approved``. ``overrides``
        carries ``enable`` / ``disable`` / ``weights`` from the CLI.
        ``progress`` is an optional ``(stage, status, fields)`` callback used by
        the WebUI progress page; a failing callback never interrupts a run.
        """
        code = str(code or "").strip()
        if not code:
            raise ValueError("analyze 需要标的代码")
        try:
            code = normalize_code(code)
        except ValueError as exc:
            # Fail before any provider or model is touched; the generic message
            # keeps a hostile code out of logs and reports.
            raise ValueError("analyze 需要合法的 A 股代码（6 位数字，可带 sh/sz/bj 前缀）") from exc
        as_of = date_only(date)
        if len(as_of) != 10 or as_of[4] != "-" or as_of[7] != "-":
            raise ValueError(f"分析日期必须是 YYYY-MM-DD，收到 {date!r}")

        trace_id = new_trace_id()
        run_id = f"{as_of}-{code}-{trace_id[:8]}"
        configured_home = str(self.config.get("home") or "").strip()
        home = ensure_home(Path(configured_home).expanduser() if configured_home else None)

        registry = self._registry(overrides)
        gate = ToolGate(registry)
        budget = budget_from_config(self.config)
        resolver = _resolver(self.config, budget)
        data = DataAccessLayer(self.config, home=home, as_of=as_of)
        envelopes = data.bundle(code, as_of=as_of, domains=self._domains(gate))
        digest = build_digest(envelopes, as_of=as_of)
        data_status = summarize(envelopes)
        _progress(
            progress, "data", "done", domains=len(envelopes), data_status=data_status["status"]
        )

        ctx = RunContext(
            code=code,
            date=as_of,
            config=self.config,
            registry=registry,
            gate=gate,
            resolver=resolver,
            data=data,
            envelopes=envelopes,
            digest=digest,
            budget=budget,
            trace_id=trace_id,
            home=home,
        )

        reviewer = HitlReviewer(self.config, enabled=hitl)
        traces: list[dict[str, Any]] = []
        hitl_payload: dict[str, Any] = reviewer.summary(traces)
        stage = "analysts"
        try:
            _progress(progress, stage, "running")
            analysts = run_analysts(ctx)
            _progress(progress, stage, "done", seats=len(analysts))

            stage = "debate"
            _progress(progress, stage, "running")
            debate = run_bull_bear_debate(ctx, analysts)
            _progress(progress, stage, "done")

            stage = "research"
            _progress(progress, stage, "running")
            research = run_research_manager(ctx, analysts, debate)
            _progress(progress, stage, "done")

            stage = "trader"
            _progress(progress, stage, "running")
            trade = run_trader(ctx, research, analysts)
            _progress(progress, stage, "done")

            stage = "hitl_after_trader"
            _progress(progress, stage, "running", checkpoint="after_trader")
            traces.append(
                reviewer.review(
                    "after_trader",
                    {
                        "code": code,
                        "date": as_of,
                        "research": research.model_dump(),
                        "trade": trade.model_dump(),
                        "data": ctx.data_summary(),
                    },
                )
            )
            _progress(progress, stage, "done", checkpoint="after_trader")

            stage = "risk"
            _progress(progress, stage, "running")
            risk = run_risk_debate(ctx, research, trade)
            _progress(progress, stage, "done", arguments=len(risk.arguments))

            stage = "hitl_before_final"
            _progress(progress, stage, "running", checkpoint="before_final")
            traces.append(
                reviewer.review(
                    "before_final",
                    {
                        "code": code,
                        "date": as_of,
                        "research": research.model_dump(),
                        "trade": trade.model_dump(),
                        "risks": [argument.model_dump() for argument in risk.arguments],
                    },
                )
            )
            _progress(progress, stage, "done", checkpoint="before_final")

            hitl_payload = reviewer.summary(traces)
            stage = "portfolio_manager"
            _progress(progress, stage, "running")
            if hitl_blocked(traces):
                decision = FinalDecision(
                    role="portfolio_manager",
                    partial=True,
                    rating="unknown",
                    horizon="unknown",
                    reasons=["人工复核未通过，终审被拦截。"],
                    risks=["HITL 决策为拒绝或中止，本次不给出研究评级。"],
                    confidence=0.0,
                    low_quality=True,
                    hitl=hitl_payload,
                    notes="终审因人工介入被拦截",
                )
            else:
                decision = run_portfolio_manager(
                    ctx, research, trade, risk.arguments, hitl=hitl_payload
                )
            _progress(progress, stage, "done", rating=decision.rating)

            stage = "quality_gate"
            _progress(progress, stage, "running")
            quality = run_quality_gate(ctx, analysts, research, trade, risk.arguments, decision)
            if quality.low_quality and not decision.low_quality:
                decision = decision.model_copy(update={"low_quality": True})
            _progress(progress, stage, "done", score=quality.score)
        except LLMRateLimited as exc:
            _progress(progress, stage, "failed", error=str(redact(exc)))
            log.warning("run %s rate limited: %s", run_id, exc)
            raise
        except LLMConfigError as exc:
            _progress(progress, stage, "failed", error=str(redact(exc)))
            log.error("run %s configuration error: %s", run_id, exc)
            raise
        except NodeConfigError as exc:
            _progress(progress, stage, "failed", error=str(redact(exc)))
            log.error("run %s role configuration error: %s", run_id, exc)
            raise
        except RoleError as exc:
            _progress(progress, stage, "failed", error=str(redact(exc)))
            log.error("run %s role override error: %s", run_id, exc)
            raise

        partial = (
            bool(decision.partial)
            or decision.rating == "unknown"
            or bool(ctx.skipped)
            or bool(ctx.budget_tripped)
            or hitl_blocked(traces)
            or bool(quality.partial)
        )

        state: dict[str, Any] = {
            "run_id": run_id,
            "trace_id": trace_id,
            "code": code,
            "date": as_of,
            "config_hash": self.config.get("config_hash", ""),
            "llm_provider": str(
                (((self.config.get("llm_config") or {}).get("llm") or {}).get("llm_provider"))
                or "offline"
            ),
            "roles": {
                "analysts": [spec.key for spec in registry.enabled_analysts()],
                "weights": registry.weights,
                "overrides": dict(overrides or {}),
                "skipped": list(ctx.skipped),
            },
            "data": {
                "summary": data_status,
                "domains": {name: envelope.model_dump() for name, envelope in envelopes.items()},
                "attempts": dict(data.attempts),
                "digest": digest,
            },
            "analysts": [report.model_dump() for report in analysts],
            "debate": {
                "bull_bear": debate.as_dict(),
                "risk": risk.as_dict(),
            },
            "research": research.model_dump(),
            "trade": trade.model_dump(),
            "risk": risk.as_dict(),
            "decision": decision.model_dump(),
            "quality": quality.model_dump(),
            "hitl": hitl_payload,
            "budget": budget.snapshot(),
            "partial": partial,
            "generated_at": _now(),
        }

        decision_payload: dict[str, Any] = {
            "run_id": run_id,
            "trace_id": trace_id,
            "code": code,
            "date": as_of,
            "rating": decision.rating,
            "action": _RATING_ACTION.get(decision.rating, "未知"),
            "execution": False,
            "horizon": decision.horizon,
            "confidence": decision.confidence,
            "reasons": list(decision.reasons),
            "risks": list(decision.risks),
            "partial": partial,
            "low_quality": bool(decision.low_quality or quality.low_quality),
            "quality_score": quality.score,
            "quality_threshold": quality.threshold,
            "hitl": hitl_payload,
            "data": data_status,
            "budget_tripped": ctx.budget_tripped,
            "skipped": list(ctx.skipped),
            "disclaimer": "研究评级，不构成投资建议，也不包含可执行价位或交易指令。",
        }

        # Persist the same decision payload that is returned, otherwise the
        # saved snapshot keeps only the research verdict (no action/hitl/data).
        state["decision"] = decision_payload

        report_paths = _persist(state, home)
        if report_paths:
            state["report"] = report_paths
            decision_payload["report"] = report_paths
        _progress(progress, "persist", "done", files=len(report_paths or {}))

        _progress(progress, "done", "done", run_id=run_id, rating=decision.rating)
        return state, decision_payload

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _registry(self, overrides: Mapping[str, Any] | None) -> RoleRegistry:
        registry = RoleRegistry.from_config(self.config)
        if not overrides:
            return registry
        weights = overrides.get("weights") or overrides.get("weight") or []
        return registry.with_overrides(
            enable=overrides.get("enable") or [],
            disable=overrides.get("disable") or [],
            weights=weights,
        )

    def _domains(self, gate: ToolGate) -> list[str]:
        roles = [spec.key for spec in gate.registry.enabled_analysts()]
        domains = gate.domains_for(roles)
        routing = (self.config.get("data_source") or {}).get("routing") or {}
        # Keep the analyst-driven order, then anything else routing knows about
        # only when a configured provider actually serves it.
        for domain in routing:
            if domain not in domains:
                domains.append(str(domain))
        if not domains:
            domains = list(routing)
        return domains


def _resolver(config: Mapping[str, Any], budget: Any) -> Any:
    from stock_senate.roles.llm import RoleLLMResolver

    return RoleLLMResolver(config, budget=budget)


def _progress(
    callback: ProgressCallback | None,
    stage: str,
    status: str,
    **fields: Any,
) -> None:
    """Best-effort progress notification; observability must never break a run."""
    if callback is None:
        return
    try:
        callback(str(stage), str(status), dict(fields))
    except Exception:  # noqa: BLE001 - a broken listener is not a run failure
        log.debug("progress callback failed at %s/%s", stage, status, exc_info=True)


def _now() -> str:
    from stock_senate.data.envelope import now_iso

    return now_iso()


def _persist(state: dict[str, Any], home: Path) -> dict[str, str]:
    try:
        from stock_senate.report.store import save_run

        return save_run(state, home=home)
    except Exception as exc:  # noqa: BLE001 - report files are derived, never fatal
        log.warning("report persistence skipped: %s", exc)
        return {}


__all__ = ["SenateGraph"]
