"""Human-in-the-loop checkpoints.

HITL is a workflow switch, not a prompt suggestion. The graph asks the reviewer
after the trader and again before the final verdict; each review returns a
trace record that ends up in the decision and the saved report, so skipping the
checkpoints (``--no-hitl``) leaves an audit trail instead of looking like a
normal run.

A checkpoint that times out follows ``workflow.hitl.on_timeout``. The default,
``auto_approve``, is deliberately recorded as ``auto_approved`` so a human
reading the report can tell a real approval from a timeout.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from stock_senate.data.envelope import now_iso

Reviewer = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]

DEFAULT_CHECKPOINTS = ("after_trader", "before_final")


class HitlReviewer:
    def __init__(
        self,
        config: Mapping[str, Any] | None,
        *,
        enabled: bool = True,
        handler: Reviewer | None = None,
    ) -> None:
        section = ((config or {}).get("workflow") or {}).get("hitl") or {}
        self.enabled = bool(enabled) and section.get("enabled", True) is not False
        self.checkpoints = tuple(section.get("checkpoints") or DEFAULT_CHECKPOINTS)
        self.on_timeout = str(section.get("on_timeout") or "auto_approve")
        self.timeout_seconds = float(section.get("timeout_seconds", 1800))
        self.handler = handler

    def applies(self, checkpoint: str) -> bool:
        return self.enabled and checkpoint in self.checkpoints

    def review(self, checkpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "checkpoint": checkpoint,
            "enabled": self.enabled,
            "at": now_iso(),
            "auto_approved": False,
            "decision": "skipped",
        }
        if not self.applies(checkpoint):
            record["note"] = "检查点未启用或不在配置清单内，已跳过并留痕"
            return record
        if self.handler is None:
            record.update(
                decision="auto_approve",
                auto_approved=True,
                note="未配置人工处理器，按 auto_approve 放行",
            )
            return record
        try:
            answer = self.handler(checkpoint, payload)
        except TimeoutError:
            if self.on_timeout == "abort":
                record.update(decision="abort", note="人工确认超时，按 abort 终止")
                return record
            record.update(
                decision="auto_approve",
                auto_approved=True,
                note="人工确认超时，按 auto_approve 放行",
            )
            return record
        answer = dict(answer or {})
        decision = str(answer.get("decision") or "approved")
        record["decision"] = decision
        note = answer.get("note") or answer.get("reason")
        if note:
            record["note"] = str(note)[:300]
        for key in ("rating", "direction", "horizon"):
            if key in answer:
                record[key] = answer[key]
        return record

    def summary(self, traces: list[Mapping[str, Any]]) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "checkpoints": list(self.checkpoints),
            "on_timeout": self.on_timeout,
            "traces": [dict(item) for item in traces],
        }


def hitl_blocked(traces: list[Mapping[str, Any]]) -> bool:
    return any(item.get("decision") in {"rejected", "abort"} for item in traces)


__all__ = ["HitlReviewer", "Reviewer", "hitl_blocked"]
