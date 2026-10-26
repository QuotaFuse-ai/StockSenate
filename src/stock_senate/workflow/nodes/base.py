"""Shared node behaviour: prompt loading, schema validation, scrubbing.

Every role goes through this path. A model (remote or offline) returns a
mapping, the mapping is validated against the role's Pydantic schema, unknown
keys are dropped rather than accommodated, and any sentence that smuggles an
executable price or order instruction is removed. A role that still cannot
produce a valid object becomes an explicit ``partial`` output instead of a
silent hole in the report.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from stock_senate.data.sanitize import compliance_filter, compliance_scan
from stock_senate.roles.llm import LLMConfigError, LLMRateLimited, LLMUnavailable
from stock_senate.roles.registry import RoleSpec
from stock_senate.roles.schemas import schema_for
from stock_senate.workflow.budget import BudgetExceeded
from stock_senate.workflow.context import RunContext

BUILTIN_PROMPT = (
    "你是 StockSenate 的{display}。只做基于给定数据的研究分析："
    "所有数值必须来自输入，缺失数据写入 data_gaps，禁止补数；"
    "不输出任何可执行价格、价位、仓位比例或订单/交易指令；"
    "严格输出符合 {schema} 的 JSON 对象。"
)


class NodeConfigError(ValueError):
    """A role cannot run because its own configuration is incomplete."""


class RoleOutputError(RuntimeError):
    """A role failed and the failure cannot be represented as partial output."""


def prompt_root(config: Mapping[str, Any]) -> Path | None:
    config_dir = config.get("config_dir")
    if config_dir:
        candidate = Path(str(config_dir)).expanduser().parent
        if (candidate / "prompts").is_dir():
            return candidate
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "prompts").is_dir():
            return parent
    return None


def load_system_prompt(spec: RoleSpec, config: Mapping[str, Any]) -> str:
    if spec.prompt:
        path = Path(spec.prompt).expanduser()
        if not path.is_absolute():
            root = prompt_root(config)
            path = (root / path) if root else path
        if path.is_file():
            return path.read_text(encoding="utf-8")
        if spec.custom:
            raise NodeConfigError(f"自定义角色 {spec.key} 的 prompt 文件不存在：{spec.prompt}")
    return BUILTIN_PROMPT.format(display=spec.display_name, schema=spec.output_schema)


def _fallback_payload(spec: RoleSpec, context: Mapping[str, Any], reason: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "role": spec.key,
        "partial": True,
        "notes": f"角色输出不可用：{reason}"[:500],
    }
    if spec.output_schema == "AnalystReport":
        base.update(
            display_name=spec.display_name,
            stance="unknown",
            confidence=0.0,
            summary=f"{spec.display_name}本次未能产出可用结论。",
            key_points=[],
            facts=[],
            citations=[],
            data_gaps=[reason[:300]],
        )
    elif spec.output_schema == "ResearchArgument":
        base.update(
            side=str(context.get("side") or "bull"), points=[], rebuttals=[], confidence=0.0
        )
    elif spec.output_schema == "ResearchVerdict":
        base.update(
            stance="unknown", confidence=0.0, rationale=[reason[:300]], unresolved=[reason[:200]]
        )
    elif spec.output_schema == "TradePlan":
        base.update(
            direction="unknown",
            horizon="unknown",
            rationale=[reason[:300]],
            risks=[],
            confidence=0.0,
        )
    elif spec.output_schema == "RiskArgument":
        base.update(
            side=str(context.get("side") or "neutral"),
            stance="unknown",
            points=[],
            severity="unknown",
        )
    elif spec.output_schema == "FinalDecision":
        base.update(
            rating="unknown",
            horizon="unknown",
            reasons=[reason[:300]],
            risks=[],
            confidence=0.0,
            low_quality=True,
            hitl=dict(context.get("hitl") or {}),
        )
    elif spec.output_schema == "QualityVerdict":
        base.update(score=0.0, low_quality=True, threshold=70.0, dimensions={}, notes=reason[:300])
    return base


def _coerce(
    payload: Any, schema: type[BaseModel], spec: RoleSpec, context: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RoleOutputError("模型输出不是 JSON 对象")
    allowed = set(schema.model_fields)
    cleaned = {key: value for key, value in payload.items() if key in allowed}
    cleaned.setdefault("role", spec.key)
    if spec.output_schema == "ResearchArgument":
        cleaned.setdefault("side", context.get("side") or "bull")
    if spec.output_schema == "RiskArgument":
        cleaned.setdefault("side", context.get("side") or "neutral")
    return cleaned


def _scrub(value: Any, *, changed: list[str]) -> Any:
    if isinstance(value, str):
        hits = compliance_scan(value)
        if not hits:
            return value
        filtered = compliance_filter(value)
        changed.extend(hits)
        return filtered
    if isinstance(value, list):
        return [_scrub(item, changed=changed) for item in value]
    if isinstance(value, dict):
        return {str(key): _scrub(item, changed=changed) for key, item in value.items()}
    return value


def run_role(ctx: RunContext, role: str, context: Mapping[str, Any]) -> BaseModel:
    """Run one role and return a validated output model."""
    spec = ctx.registry.get(role)
    system = load_system_prompt(spec, ctx.config)
    schema = schema_for(spec.output_schema)
    payload: Any
    try:
        payload, _usage = ctx.resolver.complete(
            role=spec.key,
            schema_name=spec.output_schema,
            system=system,
            context=context,
        )
    except BudgetExceeded as exc:
        ctx.budget_tripped = str(exc)
        payload = _fallback_payload(spec, context, f"预算熔断：{exc}")
    except LLMRateLimited:
        raise
    except LLMConfigError:
        raise
    except LLMUnavailable as exc:
        ctx.skipped.append({"role": spec.key, "reason": f"模型不可用：{exc}"[:300]})
        payload = _fallback_payload(spec, context, f"模型不可用：{exc}")

    changed: list[str] = []
    worked = _scrub(copy.deepcopy(payload), changed=changed)
    try:
        cleaned = _coerce(worked, schema, spec, context)
        model = schema.model_validate(cleaned)
    except (ValidationError, RoleOutputError) as exc:
        fallback = schema.model_validate(
            _fallback_payload(spec, context, f"结构化输出校验失败：{exc}")
        )
        ctx.skipped.append({"role": spec.key, "reason": "结构化输出校验失败"})
        return fallback
    if changed:
        note = "；已移除可执行价位或指令表述"
        existing = str(getattr(model, "notes", "") or "")
        model = model.model_copy(update={"notes": (existing + note).strip("；")})
    return model


__all__ = [
    "NodeConfigError",
    "RoleOutputError",
    "load_system_prompt",
    "prompt_root",
    "run_role",
]
