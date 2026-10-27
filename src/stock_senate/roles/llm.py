"""Model resolution and structured-output handling.

A role maps to exactly one of the sixteen legal ``role_llms`` keys (aliases are
normalised first). Custom roles fall back to the default model instead of
inventing a new key, so the sixteen-key contract stays intact.

``offline`` is a real provider: it calls the deterministic engine in
``workflow.offline`` and never touches the network. Remote providers are
OpenAI-compatible or Anthropic-compatible and are imported lazily, which keeps
``ssenate --help`` working when no optional SDK is installed.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stock_senate.config.schema import ROLE_ALIASES, ROLE_LLM_KEYS
from stock_senate.observability.logging import get_logger
from stock_senate.observability.tracing import span as trace_span
from stock_senate.workflow import offline
from stock_senate.workflow.budget import Budget
from stock_senate.workflow.compress import compress_context, settings_from_config


class LLMUnavailable(RuntimeError):
    """The configured model cannot be reached or is not installed."""


class LLMConfigError(LLMUnavailable):
    """Configuration can never work (missing SDK, key or base URL): do not retry."""


class LLMRateLimited(LLMUnavailable):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMResponseInvalid(ValueError):
    """The model answered, but not with a JSON object for the schema."""


_DEFAULT_BASE_URLS = {
    "minimax": "https://api.minimax.chat/v1",
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "ollama": "http://127.0.0.1:11434/v1",
}
_PROVIDER_KEY_ENV = {
    "minimax": "MINIMAX_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
}
_ANTHROPIC_PROVIDERS = {"anthropic", "claude"}
_SDK_PROVIDERS = {"claude_agent_sdk", "claude-agent-sdk"}


@dataclass(frozen=True)
class ModelTarget:
    role: str
    provider: str
    model: str


def _call_span(target: ModelTarget, schema_name: str) -> Any:
    """One span per role model call; the target names which model answered."""
    return trace_span(
        f"llm.{schema_name}",
        {"role": target.role, "provider": target.provider, "model": target.model},
    )


def _json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise LLMResponseInvalid("模型输出中没有 JSON 对象")
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMResponseInvalid(f"JSON 解析失败：{exc}") from exc
    if not isinstance(value, dict):
        raise LLMResponseInvalid("模型输出不是 JSON 对象")
    return value


def _truncate(value: Any, *, max_string: int = 4000, max_list: int = 40) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "…"
    if isinstance(value, list):
        return [
            _truncate(item, max_string=max_string, max_list=max_list) for item in value[:max_list]
        ]
    if isinstance(value, dict):
        return {
            str(key): _truncate(item, max_string=max_string, max_list=max_list)
            for key, item in value.items()
        }
    return value


class OpenAICompatibleClient:
    def __init__(
        self,
        provider: str,
        *,
        model: str,
        base_url: str | None,
        api_key: str | None,
        timeout: float,
        max_tokens: int,
    ) -> None:
        self.provider = provider
        self.model = model
        self.base_url = base_url or _DEFAULT_BASE_URLS.get(provider, "")
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._client: Any = None

    def _ensure(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise LLMConfigError(
                "缺少 openai SDK；请安装 `openai` 或把 llm_provider 设为 offline"
            ) from exc
        if not self.base_url:
            raise LLMConfigError(
                f"provider {self.provider!r} 没有默认 base_url，"
                "请在 llm_config.toml 配置 backend_url"
            )
        if not self.api_key:
            names = [
                "STOCKSENATE_LLM_API_KEY",
                _PROVIDER_KEY_ENV.get(self.provider, "OPENAI_API_KEY"),
            ]
            raise LLMConfigError(
                f"provider {self.provider!r} 缺少 API key；"
                f"请设置 {' 或 '.join(dict.fromkeys(names))}"
            )
        self._client = openai.OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=0,
        )
        return self._client

    def complete(
        self, *, system: str, user: str, json_mode: bool = True
    ) -> tuple[str, dict[str, int]]:
        client = self._ensure()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.2,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - SDK exception surface differs
            name = type(exc).__name__.lower()
            if "ratelimit" in name or "429" in str(exc):
                raise LLMRateLimited(str(exc)[:400]) from exc
            raise LLMUnavailable(str(exc)[:400]) from exc
        text = (response.choices[0].message.content or "") if response.choices else ""
        usage = getattr(response, "usage", None)
        tokens = {
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        }
        return text, tokens


class AnthropicClient:
    def __init__(self, *, model: str, api_key: str | None, timeout: float, max_tokens: int) -> None:
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._client: Any = None

    def _ensure(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMConfigError("缺少 anthropic SDK；请安装 `anthropic` 或改用离线模式") from exc
        if not self.api_key:
            raise LLMConfigError("缺少 ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)
        return self._client

    def complete(
        self, *, system: str, user: str, json_mode: bool = True
    ) -> tuple[str, dict[str, int]]:
        client = self._ensure()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system + ("\n只输出一个 JSON 对象，不要 Markdown。" if json_mode else ""),
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__.lower()
            if "ratelimit" in name or "429" in str(exc):
                raise LLMRateLimited(str(exc)[:400]) from exc
            raise LLMUnavailable(str(exc)[:400]) from exc
        parts = getattr(response, "content", None) or []
        text = "".join(getattr(part, "text", "") for part in parts)
        usage = getattr(response, "usage", None)
        tokens = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }
        return text, tokens


class RoleLLMResolver:
    """Resolve a role to a model, then to validated structured output."""

    def __init__(self, config: Mapping[str, Any], *, budget: Budget | None = None) -> None:
        llm_section = (config or {}).get("llm_config") or {}
        self.llm = dict(llm_section.get("llm") or {})
        self.role_llms = dict(self.llm.get("role_llms") or {})
        self.provider = str(self.llm.get("llm_provider") or "offline")
        self.default_model = str(
            self.llm.get("deep_think_llm") or self.llm.get("quick_think_llm") or "offline"
        )
        self.max_tokens = int(self.llm.get("max_tokens", 4096))
        self.timeout = float(self.llm.get("llm_timeout", 150))
        self.transport_retries = max(0, int(self.llm.get("llm_max_retries", 3)))
        self.retry_delay = max(0.0, float(self.llm.get("llm_retry_delay", 5)))
        structured = dict(llm_section.get("structured_output") or {})
        self.validation_retries = max(0, int(structured.get("max_local_retries", 2)))
        self.json_mode = structured.get("enabled", True) is not False
        self.fallback_chain = list((llm_section.get("fallback") or {}).get("chain") or [])
        self.compression = settings_from_config(config)
        self.budget = budget
        self._clients: dict[tuple[str, str], Any] = {}
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # target resolution
    # ------------------------------------------------------------------ #

    def target(self, role: str) -> ModelTarget:
        key = self._canonical(role)
        entry = self.role_llms.get(key)
        if isinstance(entry, Mapping) and entry.get("provider") and entry.get("model"):
            return ModelTarget(role=key, provider=str(entry["provider"]), model=str(entry["model"]))
        return ModelTarget(role=key, provider=self.provider, model=self.default_model)

    def _canonical(self, role: str) -> str:
        key = str(role or "").strip()
        key = ROLE_ALIASES.get(key, key)
        if key in ROLE_LLM_KEYS:
            return key
        # Custom roles reuse the default model; they never introduce a new key.
        return key

    def client(self, target: ModelTarget) -> Any:
        cache_key = (target.provider, target.model)
        if cache_key in self._clients:
            return self._clients[cache_key]
        provider = target.provider.lower()
        if provider in _SDK_PROVIDERS:
            raise LLMConfigError(
                f"provider {target.provider!r} 需要 claude-agent-sdk，当前未安装；"
                "请改用 anthropic 或把 llm_provider 设为 offline"
            )
        api_key = self._api_key(provider)
        if provider in _ANTHROPIC_PROVIDERS:
            client: Any = AnthropicClient(
                model=target.model,
                api_key=api_key,
                timeout=self.timeout,
                max_tokens=self.max_tokens,
            )
        else:
            client = OpenAICompatibleClient(
                provider,
                model=target.model,
                base_url=self._base_url(provider),
                api_key=api_key,
                timeout=self.timeout,
                max_tokens=self.max_tokens,
            )
        self._clients[cache_key] = client
        return client

    def _api_key(self, provider: str) -> str | None:
        configured = self.llm.get("api_key_env")
        names = ["STOCKSENATE_LLM_API_KEY"]
        if configured:
            names.insert(0, str(configured))
        names.append(_PROVIDER_KEY_ENV.get(provider, "OPENAI_API_KEY"))
        for name in dict.fromkeys(names):
            value = os.environ.get(name)
            if value:
                return value
        return None

    def _base_url(self, provider: str) -> str:
        return str(self.llm.get("backend_url") or "") or _DEFAULT_BASE_URLS.get(provider, "")

    # ------------------------------------------------------------------ #
    # completion
    # ------------------------------------------------------------------ #

    def complete(
        self,
        *,
        role: str,
        schema_name: str,
        system: str,
        context: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        target = self.target(role)
        with _call_span(target, schema_name) as current:
            if target.provider.lower() == "offline":
                payload = offline.generate(schema_name, context)
                self.calls.append(
                    {"role": target.role, "provider": "offline", "model": target.model}
                )
                return payload, {
                    "provider": "offline",
                    "model": target.model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                }

            client = self.client(target)
            user, compression = self._prompt(schema_name, context)
            current.set_attributes(
                {"compression": bool(compression.get("triggered")), "local_retries": 0}
            )
            last_error: Exception | None = None
            for attempt in range(self.validation_retries + 1):
                text, tokens = self._invoke(client, system=system, user=user)
                if self.budget is not None:
                    self.budget.charge(**tokens)
                self.calls.append(
                    {
                        "role": target.role,
                        "provider": target.provider,
                        "model": target.model,
                        **tokens,
                        "compression": compression,
                    }
                )
                current.set_attributes({**tokens, "local_retries": attempt})
                try:
                    return _json_object(text), {
                        "provider": target.provider,
                        "model": target.model,
                        **tokens,
                        "compression": compression,
                    }
                except LLMResponseInvalid as exc:
                    last_error = exc
                    get_logger("llm").warning(
                        "structured output invalid; retrying locally",
                        extra={
                            "event": "schema_retry",
                            "role": target.role,
                            "attempt": attempt + 1,
                        },
                    )
                    user = (
                        user
                        + f"\n\n上一次输出无法解析（{exc}）。只返回一个符合 "
                        + f"{schema_name} 的 JSON 对象，不要解释。"
                    )
            raise LLMUnavailable(f"{target.role} 结构化输出校验失败：{last_error}")

    def _invoke(self, client: Any, *, system: str, user: str) -> tuple[str, dict[str, int]]:
        attempt = 0
        while True:
            try:
                return client.complete(system=system, user=user, json_mode=self.json_mode)
            except LLMConfigError:
                raise
            except LLMRateLimited:
                attempt += 1
                if attempt > self.transport_retries:
                    raise
                time.sleep(min(self.retry_delay, 5.0))
            except LLMUnavailable:
                attempt += 1
                if attempt > self.transport_retries:
                    raise
                time.sleep(min(self.retry_delay, 5.0))

    def _prompt(self, schema_name: str, context: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        compressed, record = compress_context(context, self.compression)
        # Below the trigger the historical truncation still applies; once
        # compression ran it already bounded strings and lists and its
        # citation-preserving shape must not be flattened again.
        payload = compressed if record.triggered else _truncate(compressed)
        blob = json.dumps(payload, ensure_ascii=False, default=str)
        prompt = (
            f"请输出一个符合 {schema_name} 的 JSON 对象。\n"
            "硬性约束：不给出任何可执行价格、价位、仓位比例或订单字段；"
            "数据缺失只能写入 data_gaps，禁止补数或猜测数值；"
            "所有数值必须能在输入数据中找到。\n"
            f"输入数据：\n{blob}"
        )
        return prompt, record.as_dict()

    def _user_prompt(self, schema_name: str, context: Mapping[str, Any]) -> str:
        return self._prompt(schema_name, context)[0]


__all__ = [
    "AnthropicClient",
    "LLMConfigError",
    "LLMRateLimited",
    "LLMResponseInvalid",
    "LLMUnavailable",
    "ModelTarget",
    "OpenAICompatibleClient",
    "RoleLLMResolver",
]
