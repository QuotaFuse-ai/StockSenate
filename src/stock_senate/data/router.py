"""DataAccessLayer — the only door between the graph and data.

The graph never imports a provider. It asks this layer for one domain of one
code at one analysis date and gets a ``DataEnvelope`` back — always. When every
candidate source fails, the answer is a ``missing`` envelope with a readable
reason, never an exception and never a model-invented number.

Modes (``data_source.toml`` ``[global].mode``):

* ``stockdata`` — only remote底座 providers (TickHub);
* ``local``     — only local archives; no socket is opened;
* ``hybrid``    — per-domain routing with local providers attempted first;
* ``priority``  — exactly the order declared in ``[routing]``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from stock_senate.data.envelope import (
    DataEnvelope,
    SourceStatus,
    assert_no_llm_fill,
    date_only,
)
from stock_senate.data.providers import (
    Provider,
    build_providers,
    is_remote,
)

MODES = ("stockdata", "local", "hybrid", "priority")

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DataAccessLayer:
    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        home: Path | None = None,
        providers: Mapping[str, Provider] | None = None,
        as_of: str | None = None,
    ) -> None:
        section = _data_section(config)
        self.config = dict(section)
        self.home = home
        self.as_of = as_of
        self.globals: dict[str, Any] = dict(section.get("global") or {})
        self.mode = str(self.globals.get("mode") or "priority").lower()
        if self.mode not in MODES:
            raise ValueError(f"未知数据模式 {self.mode!r}；可选 {', '.join(MODES)}")
        if self.globals.get("forbid_llm_fill", True) is not True:
            raise ValueError("data_source.forbid_llm_fill 必须为 true：禁止 LLM 补数")
        self.allow_fallback = self.globals.get("allow_fallback", True) is not False
        self.forbid_llm_fill = True
        self.enforce_no_lookahead = (
            dict(section.get("freshness") or {}).get("enforce_no_lookahead", True) is not False
        )
        self.routing: dict[str, list[str]] = {
            str(domain): [str(name) for name in names]
            for domain, names in (section.get("routing") or {}).items()
        }
        self.providers: dict[str, Provider] = dict(
            providers
            if providers is not None
            else build_providers(section.get("providers"), home=home)
        )
        self.attempts: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def fetch(
        self,
        domain: str,
        code: str,
        *,
        as_of: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> DataEnvelope:
        as_of = _as_day(as_of or self.as_of)
        attempts: list[dict[str, Any]] = []
        self.attempts[domain] = attempts
        candidates = self.candidates(domain)
        if not candidates:
            return DataEnvelope.missing(
                domain,
                code,
                as_of=as_of,
                detail=f"no provider for domain {domain!r} in mode {self.mode!r}",
            )

        for index, name in enumerate(candidates):
            provider = self.providers[name]
            try:
                envelope = provider.fetch(domain, code, as_of, params)
            except Exception as exc:  # noqa: BLE001 - provider boundary, never crash the run
                attempts.append(_failure(name, exc))
                continue

            reason = self._reject(envelope, as_of)
            if reason:
                attempts.append(
                    {
                        "provider": name,
                        "error": "rejected",
                        "reason": reason,
                        "status": envelope.data_source_status.value,
                    }
                )
                continue

            attempts.append(
                {
                    "provider": name,
                    "error": None,
                    "reason": "ok",
                    "status": envelope.data_source_status.value,
                }
            )
            return self._with_trace(envelope, candidates[:index])

        detail = (
            "; ".join(f"{item['provider']}: {item['reason']}" for item in attempts)
            or "no candidate responded"
        )
        return DataEnvelope.missing(
            domain,
            code,
            as_of=as_of,
            detail=f"all sources failed in mode {self.mode}: {detail}"[:2000],
        )

    def fetch_many(
        self,
        domains: Iterable[str],
        code: str,
        *,
        as_of: str | None = None,
        params: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, DataEnvelope]:
        params = params or {}
        return {
            domain: self.fetch(domain, code, as_of=as_of, params=params.get(domain))
            for domain in domains
        }

    def bundle(
        self, code: str, *, as_of: str | None = None, domains: Iterable[str] | None = None
    ) -> dict[str, DataEnvelope]:
        chosen = list(domains) if domains is not None else self.domain_names()
        return self.fetch_many(chosen, code, as_of=as_of)

    def domain_names(self) -> list[str]:
        return list(self.routing) or sorted(
            {domain for provider in self.providers.values() for domain in provider.domains}
        )

    def candidates(self, domain: str) -> list[str]:
        names = [
            name
            for name in self.routing.get(domain, [])
            if name in self.providers and self.providers[name].supports(domain)
        ]
        if not names:
            names = [name for name, provider in self.providers.items() if provider.supports(domain)]
        if self.mode == "local":
            names = [name for name in names if not is_remote(self.providers[name])]
        elif self.mode == "stockdata":
            names = [name for name in names if is_remote(self.providers[name])]
        elif self.mode == "hybrid":
            local = [name for name in names if not is_remote(self.providers[name])]
            names = local + [name for name in names if is_remote(self.providers[name])]
        if not self.allow_fallback:
            names = names[:1]
        return names

    # ------------------------------------------------------------------ #
    # routing decisions
    # ------------------------------------------------------------------ #

    def _reject(self, envelope: DataEnvelope, as_of: str) -> str | None:
        if envelope.data_source_status is SourceStatus.MISSING:
            return "provider returned a missing envelope"
        try:
            assert_no_llm_fill(envelope.payload)
        except ValueError as exc:
            return str(exc)
        if self.enforce_no_lookahead:
            observed = date_only(envelope.data_as_of)
            if _ISO_DAY.match(observed) and _ISO_DAY.match(as_of) and observed > as_of:
                return f"lookahead: data_as_of {observed} is later than analysis date {as_of}"
        return None

    @staticmethod
    def _with_trace(envelope: DataEnvelope, skipped: list[str]) -> DataEnvelope:
        if not skipped:
            return envelope
        detail = f"{envelope.source_detail} | router: fell back from {', '.join(skipped)}"
        return envelope.model_copy(update={"source_detail": detail})

    # ------------------------------------------------------------------ #
    # reporting
    # ------------------------------------------------------------------ #

    def health(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allow_fallback": self.allow_fallback,
            "providers": {
                name: sorted(provider.domains) for name, provider in self.providers.items()
            },
            "attempts": self.attempts,
        }


def summarize(envelopes: Mapping[str, DataEnvelope]) -> dict[str, Any]:
    """One status for a whole bundle, for reports and exit codes."""
    statuses = [envelope.data_source_status for envelope in envelopes.values()]
    missing = [
        domain
        for domain, item in envelopes.items()
        if item.data_source_status is SourceStatus.MISSING
    ]
    degraded = [
        domain
        for domain, item in envelopes.items()
        if item.data_source_status is SourceStatus.DEGRADED
    ]
    if not statuses or len(missing) == len(statuses):
        overall = SourceStatus.MISSING.value
    elif missing or degraded:
        overall = SourceStatus.DEGRADED.value
    else:
        overall = SourceStatus.OK.value
    return {
        "status": overall,
        "total": len(statuses),
        "missing": missing,
        "degraded": degraded,
        "ok": len(statuses) - len(missing) - len(degraded),
    }


def _failure(name: str, exc: Exception) -> dict[str, Any]:
    return {
        "provider": name,
        "error": type(exc).__name__,
        "reason": str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__,
        "status": "missing",
    }


def _data_section(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not config:
        return {}
    if "data_source" in config and isinstance(config["data_source"], Mapping):
        return dict(config["data_source"])
    if any(key in config for key in ("providers", "routing", "global", "mode", "freshness")):
        return dict(config)
    return {}


def _as_day(value: str | None) -> str:
    if not value:
        from datetime import date

        return date.today().isoformat()
    return date_only(value)


__all__ = ["DataAccessLayer", "MODES", "summarize"]
