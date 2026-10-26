"""Local JSON provider.

Layout: ``{root}/{domain}/{code}/{date}.json`` (``date`` = ``YYYY-MM-DD``).
An exact match wins; otherwise the newest archive entry not later than the
requested date is used and the substitution is recorded in ``source_detail``.
A missing file is ``ProviderNotAvailable``; a malformed file is a
``ProviderError`` so the router can downgrade it instead of hiding it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stock_senate.data.envelope import date_only
from stock_senate.data.providers.base import (
    Provider,
    ProviderError,
    ProviderNotAvailable,
    envelope_from_payload,
)


class LocalJsonProvider(Provider):
    kind = "json_file"

    def fetch(self, domain: str, code: str, as_of: str,
              params: dict[str, Any] | None = None) -> Any:
        directory = self._root() / domain / code
        if not directory.is_dir():
            raise ProviderNotAvailable(f"{domain}/{code}: no archive directory")

        exact = directory / f"{as_of}.json"
        chosen: Path | None = exact if exact.is_file() else None
        substituted = False
        if chosen is None:
            candidates = sorted(
                (path for path in directory.glob("*.json") if path.stem <= as_of),
                key=lambda path: path.stem,
            )
            if not candidates:
                raise ProviderNotAvailable(f"{domain}/{code}: no record on or before {as_of}")
            chosen = candidates[-1]
            substituted = True

        try:
            raw = json.loads(chosen.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"{chosen.relative_to(self._root())}: {exc}") from exc

        payload, meta = _split_payload(raw)
        data_as_of = str(meta.get("data_as_of") or chosen.stem)
        detail = f"{domain}/{code}/{chosen.name}"
        if substituted:
            detail += f" (archived {chosen.stem}, requested {as_of})"
        return envelope_from_payload(
            domain=domain,
            code=code,
            payload=payload,
            source=self.name,
            source_detail=detail,
            data_as_of=date_only(data_as_of),
            meta=meta,
            default_confidence="high" if not substituted else "medium",
        )


def _split_payload(raw: Any) -> tuple[Any, dict[str, Any]]:
    """Accept a bare payload, ``{"data","meta"}`` or ``{"data","data_as_of"}``."""
    envelope_keys = (
        "data_as_of", "source", "source_detail", "confidence",
        "fetched_at", "freshness", "data_source_status",
    )
    if isinstance(raw, dict) and "data" in raw and (
        isinstance(raw.get("meta"), dict) or any(key in raw for key in envelope_keys)
    ):
        meta = dict(raw.get("meta") or {})
        for key in envelope_keys:
            if key in raw and key not in meta:
                meta[key] = raw[key]
        return raw["data"], meta
    if isinstance(raw, dict) and "payload" in raw and "data_as_of" in raw:
        meta = {key: raw[key] for key in envelope_keys if key in raw}
        return raw["payload"], meta
    return raw, {}


__all__ = ["LocalJsonProvider"]
