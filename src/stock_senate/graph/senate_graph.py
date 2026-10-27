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
from datetime import date as _date
from pathlib import Path
from typing import Any

from stock_senate.config.loader import ensure_home, load_config
from stock_senate.data.codes import normalize_code
from stock_senate.data.envelope import date_only
from stock_senate.data.router import DataAccessLayer, summarize
from stock_senate.observability.logging import configure_from_config, new_trace_id, redact
from stock_senate.observability.tracing import configure_tracing
from stock_senate.observability.tracing import span as trace_span
from stock_senate.roles.llm import LLMConfigError, LLMRateLimited
from stock_senate.roles.registry import RoleError, RoleRegistry
from stock_senate.roles.schemas import (
    AnalystReport,
    FinalDecision,
    QualityVerdict,
    ResearchArgument,
    ResearchVerdict,
    RiskArgument,
    TradePlan,
)
from stock_senate.roles.tool_gate import ToolGate
from stock_senate.workflow.budget import Budget, budget_from_config
from stock_senate.workflow.checkpoint import CheckpointState, store_from_config
from stock_senate.workflow.context import RunContext
from stock_senate.workflow.digest import build_digest
from stock_senate.workflow.hitl import HitlReviewer, Reviewer, hitl_blocked
from stock_senate.workflow.memory import (
    MemoryStore,
    build_reflection,
    injectable,
    memory_settings,
)
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
from stock_senate.workflow.nodes.debate_nodes import DebateResult, RiskDebateResult

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
        # Opt-in file sink; stdout stays untouched and a stock install keeps
        # writing to stderr only.
        configure_from_config(self.config)
        # Opt-in tracing; a collector that cannot be reached never fails a run.
        configure_tracing(self.config)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def propagate(
        self,
        code: str,
        date: str,
        *,
        hitl: bool = True,
        hitl_handler: Reviewer | None = None,
        overrides: Mapping[str, Any] | None = None,
        progress: ProgressCallback | None = None,
        resume: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run the full graph for ``code`` as of ``date``.

        ``hitl=False`` records every checkpoint as skipped. ``hitl_handler`` is
        the reviewer asked at each checkpoint; without one the checkpoint
        auto-approves and the trace says so, which keeps queue, MCP and WebUI
        runs non-blocking. Timeouts follow ``workflow.hitl.on_timeout`` and are
        recorded as ``auto_approved`` unless configured to abort. ``overrides``
        carries ``enable`` / ``disable`` / ``weights`` from the CLI.
        ``progress`` is an optional ``(stage, status, fields)`` callback used by
        the WebUI progress page; a failing callback never interrupts a run.
        ``resume`` uses a checkpoint written by an interrupted run of the same
        code, date and configuration instead of re-running completed stages;
        it has no effect unless ``workflow.checkpoint.enabled`` is true.
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
        try:
            _date.fromisoformat(as_of)
        except ValueError as exc:
            # Shape alone is not enough: 2026-13-99 looks right but is not a day.
            raise ValueError(f"分析日期必须是 YYYY-MM-DD，收到 {date!r}") from exc

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

        memory_store = MemoryStore(home, memory_settings(self.config))
        prior_reflections = memory_store.recent(code=code, limit=memory_store.max_entries)

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
            memory=injectable(prior_reflections, memory_store.max_entries),
        )

        reviewer = HitlReviewer(self.config, enabled=hitl, handler=hitl_handler)
        traces: list[dict[str, Any]] = []
        hitl_payload: dict[str, Any] = reviewer.summary(traces)

        config_hash = str(self.config.get("config_hash") or "")
        checkpoints = store_from_config(self.config, home)
        cached = CheckpointState()
        if checkpoints is not None and resume:
            cached = checkpoints.load(code=code, date=as_of, config_hash=config_hash)
            _restore_budget(budget, cached.budget)
            run_state = cached.stages.get("_run_state") or {}
            ctx.skipped[:] = [dict(item) for item in run_state.get("skipped") or []]
            ctx.budget_tripped = str(run_state.get("budget_tripped") or "")

        def _discard_checkpoint() -> None:
            """Drop rows no later run can reach.

            A configuration or role error is only fixed by editing the
            configuration, which changes ``config_hash``: the rows left under
            the old key can never be resumed, so keeping them is pure litter.
            Interrupts and rate limits keep their rows because the retry
            resumes under the same key.
            """
            if checkpoints is not None:
                checkpoints.clear(code=code, date=as_of, config_hash=config_hash)

        def saved(stage_name: str, payload: Mapping[str, Any]) -> None:
            if checkpoints is None:
                return
            checkpoints.save(
                code=code,
                date=as_of,
                config_hash=config_hash,
                stage=stage_name,
                payload=payload,
                budget=budget.snapshot(),
            )
            # ``skipped`` and ``budget_tripped`` accumulate across stages, so
            # they live in their own row that always reflects the latest stage.
            checkpoints.save(
                code=code,
                date=as_of,
                config_hash=config_hash,
                stage="_run_state",
                payload={
                    "skipped": list(ctx.skipped),
                    "budget_tripped": ctx.budget_tripped,
                },
                budget=budget.snapshot(),
            )

        def hitl_trace(stage_name: str, checkpoint: str, payload: Mapping[str, Any]) -> None:
            """Reuse a recorded review only when the HITL mode did not change."""
            restored = cached.stages.get(stage_name)
            if restored is not None and restored.get("mode") == bool(hitl):
                traces.append(dict(restored.get("trace") or {}))
                _progress(progress, stage_name, "done", checkpoint=checkpoint, restored=True)
                return
            # A row recorded under the other HITL mode is not a resume; drop it
            # so the reported ``resumed`` list only names what was reused.
            cached.stages.pop(stage_name, None)
            _progress(progress, stage_name, "running", checkpoint=checkpoint)
            record = reviewer.review(checkpoint, payload)
            traces.append(record)
            saved(stage_name, {"mode": bool(hitl), "trace": record})
            # A fresh review invalidates the verdict a previous run derived
            # from the old one; only the loop above may reuse it.
            cached.stages.pop("portfolio_manager", None)
            cached.stages.pop("quality_gate", None)
            _progress(progress, stage_name, "done", checkpoint=checkpoint)

        stage = "analysts"
        try:
            with _stage_span(stage):
                if "analysts" in cached.stages:
                    analysts = _restore_analysts(ctx, cached.stages["analysts"])
                    _progress(progress, stage, "done", seats=len(analysts), restored=True)
                else:
                    _progress(progress, stage, "running")
                    analysts = run_analysts(ctx)
                    saved(
                        stage,
                        {
                            "reports": [report.model_dump() for report in analysts],
                            "skipped": list(ctx.skipped),
                        },
                    )
                    _progress(progress, stage, "done", seats=len(analysts))

            stage = "debate"
            with _stage_span(stage):
                if "debate" in cached.stages:
                    debate = _restore_debate(cached.stages["debate"])
                    _progress(progress, stage, "done", restored=True)
                else:
                    _progress(progress, stage, "running")
                    debate = run_bull_bear_debate(ctx, analysts)
                    saved(stage, debate.as_dict())
                    _progress(progress, stage, "done")

            stage = "research"
            with _stage_span(stage):
                if "research" in cached.stages:
                    research = ResearchVerdict.model_validate(cached.stages["research"])
                    _progress(progress, stage, "done", restored=True)
                else:
                    _progress(progress, stage, "running")
                    research = run_research_manager(ctx, analysts, debate)
                    saved(stage, research.model_dump())
                    _progress(progress, stage, "done")

            stage = "trader"
            with _stage_span(stage):
                if "trader" in cached.stages:
                    trade = TradePlan.model_validate(cached.stages["trader"])
                    _progress(progress, stage, "done", restored=True)
                else:
                    _progress(progress, stage, "running")
                    trade = run_trader(ctx, research, analysts)
                    saved(stage, trade.model_dump())
                    _progress(progress, stage, "done")

            stage = "hitl_after_trader"
            with _stage_span(stage, checkpoint="after_trader"):
                hitl_trace(
                    stage,
                    "after_trader",
                    {
                        "code": code,
                        "date": as_of,
                        "research": research.model_dump(),
                        "trade": trade.model_dump(),
                        "data": ctx.data_summary(),
                    },
                )

            stage = "risk"
            with _stage_span(stage):
                if "risk" in cached.stages:
                    risk = _restore_risk_debate(cached.stages["risk"])
                    _progress(progress, stage, "done", arguments=len(risk.arguments), restored=True)
                else:
                    _progress(progress, stage, "running")
                    risk = run_risk_debate(ctx, research, trade)
                    saved(stage, risk.as_dict())
                    _progress(progress, stage, "done", arguments=len(risk.arguments))

            stage = "hitl_before_final"
            with _stage_span(stage, checkpoint="before_final"):
                hitl_trace(
                    stage,
                    "before_final",
                    {
                        "code": code,
                        "date": as_of,
                        "research": research.model_dump(),
                        "trade": trade.model_dump(),
                        "risks": [argument.model_dump() for argument in risk.arguments],
                    },
                )

            hitl_payload = reviewer.summary(traces)
            stage = "portfolio_manager"
            with _stage_span(stage):
                if "portfolio_manager" in cached.stages:
                    decision = FinalDecision.model_validate(cached.stages["portfolio_manager"])
                    _progress(progress, stage, "done", rating=decision.rating, restored=True)
                else:
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
                    saved(stage, decision.model_dump())
                    _progress(progress, stage, "done", rating=decision.rating)

            stage = "quality_gate"
            with _stage_span(stage):
                if "quality_gate" in cached.stages:
                    quality = QualityVerdict.model_validate(cached.stages["quality_gate"])
                    _progress(progress, stage, "done", score=quality.score, restored=True)
                else:
                    _progress(progress, stage, "running")
                    quality = run_quality_gate(
                        ctx, analysts, research, trade, risk.arguments, decision
                    )
                    saved(stage, quality.model_dump())
                    _progress(progress, stage, "done", score=quality.score)
            if quality.low_quality and not decision.low_quality:
                decision = decision.model_copy(update={"low_quality": True})
        except LLMRateLimited as exc:
            _progress(progress, stage, "failed", error=str(redact(exc)))
            log.warning("run %s rate limited: %s", run_id, exc)
            raise
        except LLMConfigError as exc:
            _discard_checkpoint()
            _progress(progress, stage, "failed", error=str(redact(exc)))
            log.error("run %s configuration error: %s", run_id, exc)
            raise
        except NodeConfigError as exc:
            _discard_checkpoint()
            _progress(progress, stage, "failed", error=str(redact(exc)))
            log.error("run %s role configuration error: %s", run_id, exc)
            raise
        except RoleError as exc:
            _discard_checkpoint()
            _progress(progress, stage, "failed", error=str(redact(exc)))
            log.error("run %s role override error: %s", run_id, exc)
            raise

        # Only stages whose output was actually reused: a review recorded under
        # another HITL mode, or a verdict invalidated by a fresh review, is gone
        # from ``cached`` by now.
        resumed_stages = [name for name in cached.names if name != "_run_state"]

        reflection = build_reflection(
            code=code,
            date=as_of,
            run_id=run_id,
            decision=decision.model_dump(),
            quality=quality.model_dump(),
            research=research.model_dump(),
            trade=trade.model_dump(),
            hitl=hitl_payload,
        )
        memory_saved = memory_store.remember(reflection)

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
            "memory": {
                "enabled": memory_store.enabled,
                "injected": len(ctx.memory),
                "stored": memory_saved,
            },
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
        if checkpoints is not None:
            state["checkpoint"] = {"enabled": True, "resumed": resumed_stages}

        report_paths = _persist(state, home)
        if report_paths:
            state["report"] = report_paths
            decision_payload["report"] = report_paths
        if checkpoints is not None:
            # A finished run must never be served from a checkpoint: the next
            # analysis of the same code/date starts from the data again.
            checkpoints.clear(code=code, date=as_of, config_hash=config_hash)
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


def _restore_analysts(ctx: RunContext, payload: Mapping[str, Any]) -> list[AnalystReport]:
    ctx.skipped[:] = [dict(item) for item in payload.get("skipped") or []]
    return [AnalystReport.model_validate(item) for item in payload.get("reports") or []]


def _restore_debate(payload: Mapping[str, Any]) -> DebateResult:
    return DebateResult(
        arguments=[
            ResearchArgument.model_validate(item) for item in payload.get("arguments") or []
        ],
        rounds_used=int(payload.get("rounds_used") or 0),
        convergence_reason=str(payload.get("convergence_reason") or ""),
        new_point_ratio=float(payload.get("new_point_ratio") or 0.0),
        history=[dict(item) for item in payload.get("history") or []],
    )


def _restore_risk_debate(payload: Mapping[str, Any]) -> RiskDebateResult:
    return RiskDebateResult(
        arguments=[RiskArgument.model_validate(item) for item in payload.get("arguments") or []],
        rounds_used=int(payload.get("rounds_used") or 0),
        convergence_reason=str(payload.get("convergence_reason") or ""),
        new_point_ratio=float(payload.get("new_point_ratio") or 0.0),
        history=[dict(item) for item in payload.get("history") or []],
    )


def _restore_budget(budget: Budget, snapshot: Mapping[str, Any]) -> None:
    """Carry consumed calls/tokens across a resume; downtime is not charged."""
    if not snapshot:
        return
    budget.calls = max(budget.calls, int(snapshot.get("calls") or 0))
    budget.input_tokens = max(budget.input_tokens, int(snapshot.get("input_tokens") or 0))
    budget.output_tokens = max(budget.output_tokens, int(snapshot.get("output_tokens") or 0))


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


def _stage_span(stage: str, **fields: Any) -> Any:
    """One span per pipeline stage; tracing off means a no-op context manager."""
    return trace_span(f"stage.{stage}", fields)


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
