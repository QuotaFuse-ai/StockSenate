"""Per-run budget and circuit breaker bookkeeping."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class BudgetExceeded(RuntimeError):
    """Raised when a run hits its call, token or wall-clock ceiling."""


@dataclass
class Budget:
    max_calls: int = 60
    max_tokens: int = 400_000
    max_seconds: float = 1800.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    started: float = field(default_factory=time.monotonic)
    stopped_reason: str = ""

    def charge(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.calls += 1
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))
        if self.calls > self.max_calls:
            self.stopped_reason = f"max_llm_calls={self.max_calls}"
            raise BudgetExceeded(f"超过单次分析调用上限 {self.max_calls}")
        if self.tokens > self.max_tokens:
            self.stopped_reason = f"max_total_tokens={self.max_tokens}"
            raise BudgetExceeded(f"超过单次分析 token 上限 {self.max_tokens}")
        if self.elapsed > self.max_seconds:
            self.stopped_reason = f"max_wall_clock_seconds={self.max_seconds:g}"
            raise BudgetExceeded(f"超过单次分析时长上限 {self.max_seconds:g}s")

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed_seconds": round(self.elapsed, 3),
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
            "stopped_reason": self.stopped_reason,
        }


def budget_from_config(config: dict[str, Any]) -> Budget:
    workflow = (config.get("workflow") or {}).get("budget") or {}
    per_run = (config.get("budget") or {}).get("per_run") or {}
    max_tokens = workflow.get("max_total_tokens")
    if max_tokens is None:
        max_tokens = int(per_run.get("max_input_tokens", 300_000)) + int(
            per_run.get("max_output_tokens", 100_000)
        )
    return Budget(
        max_calls=int(workflow.get("max_llm_calls", per_run.get("max_llm_calls", 60))),
        max_tokens=int(max_tokens),
        max_seconds=float(workflow.get("max_wall_clock_seconds", per_run.get("max_wall_clock_seconds", 1800))),
    )


__all__ = ["Budget", "BudgetExceeded", "budget_from_config"]
