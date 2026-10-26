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

import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO

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
            answer = self._ask(checkpoint, payload)
        except TimeoutError:
            if self.on_timeout == "abort":
                record.update(
                    decision="abort",
                    note=f"人工确认超时（{self.timeout_seconds:g}s），按 abort 终止",
                )
                return record
            record.update(
                decision="auto_approve",
                auto_approved=True,
                note=f"人工确认超时（{self.timeout_seconds:g}s），按 auto_approve 放行",
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

    def _ask(self, checkpoint: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Ask the handler, enforcing ``timeout_seconds`` with a real deadline.

        The handler runs in a daemon thread so a stalled reviewer cannot hold the
        run past its deadline; the thread is abandoned, not joined, on timeout.
        A non-positive ceiling disables the deadline.
        """
        handler = self.handler
        assert handler is not None
        if self.timeout_seconds <= 0:
            return handler(checkpoint, payload)
        box: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["answer"] = handler(checkpoint, payload)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=run, name="hitl-review", daemon=True).start()
        if not self._await(done):
            raise TimeoutError(f"人工确认超过 {self.timeout_seconds:g}s")
        error = box.get("error")
        if error is not None:
            raise error
        return box.get("answer") or {}

    def _await(self, done: threading.Event) -> bool:
        try:
            return done.wait(self.timeout_seconds)
        except KeyboardInterrupt as exc:
            # Ctrl-C lands on the waiting thread, not on ``input()`` in the
            # worker; treat it as "no answer" so ``on_timeout`` still governs.
            raise TimeoutError("收到中断信号") from exc

    def summary(self, traces: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "checkpoints": list(self.checkpoints),
            "on_timeout": self.on_timeout,
            "traces": [dict(item) for item in traces],
        }


def hitl_blocked(traces: Sequence[Mapping[str, Any]]) -> bool:
    return any(item.get("decision") in {"rejected", "abort"} for item in traces)


def interactive_handler(*, out: TextIO | None = None) -> Reviewer:
    """Build the terminal reviewer used by ``ssenate analyze``.

    Prompts go to stderr so ``--output json`` stays machine readable. EOF or
    Ctrl-C means the human did not answer and is raised as ``TimeoutError``, so
    the checkpoint follows ``workflow.hitl.on_timeout`` instead of hanging.
    """
    stream = out or sys.stderr
    labels = {"after_trader": "交易计划已完成", "before_final": "终审前"}
    allowed = {"rating", "direction", "horizon"}

    def ask(prompt: str) -> str:
        print(prompt, file=stream, end="", flush=True)
        return input()

    def handler(checkpoint: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        research = payload.get("research") or {}
        trade = payload.get("trade") or {}
        print(
            f"\n[人工复核] {labels.get(checkpoint, checkpoint)}："
            f"{payload.get('code', '')} {payload.get('date', '')}"
            f"  研究立场={research.get('stance', 'unknown')}"
            f"  交易方向={trade.get('direction', 'unknown')}",
            file=stream,
        )
        for _ in range(3):
            try:
                choice = (
                    ask("请选择：a 同意 / r 驳回 / m 修改 / q 中止（回车=同意）> ").strip().lower()
                )
            except (EOFError, KeyboardInterrupt) as exc:
                raise TimeoutError("未收到人工确认") from exc
            if choice in {"", "a", "y", "同意"}:
                return {"decision": "approved"}
            if choice in {"r", "n", "驳回"}:
                return {"decision": "rejected", "note": "人工驳回"}
            if choice in {"q", "中止"}:
                return {"decision": "abort", "note": "人工中止"}
            if choice in {"m", "修改"}:
                try:
                    raw = ask("修改项（如 rating=hold direction=hold，回车跳过）> ").strip()
                except (EOFError, KeyboardInterrupt) as exc:
                    raise TimeoutError("未收到人工修改") from exc
                answer: dict[str, Any] = {"decision": "approved", "note": "人工修改"}
                for part in raw.split():
                    key, _, value = part.partition("=")
                    if key in allowed and value:
                        answer[key] = value
                return answer
            print("未识别的选项，请重新输入。", file=stream)
        raise TimeoutError("连续多次未收到有效选择")

    return handler


__all__ = ["HitlReviewer", "Reviewer", "hitl_blocked", "interactive_handler"]
