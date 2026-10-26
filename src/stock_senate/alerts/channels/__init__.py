"""Delivery channels for alerts.

A channel only ever receives a fully-built event dict; it never reads config
values on its own. Delivery failures are returned, not raised, so one broken
channel cannot silence the others.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import console, webhook, webui

_SENDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "console": console.send,
    "webui": webui.send,
    "webhook": webhook.send,
}


def dispatch(
    event: Mapping[str, Any],
    section: Mapping[str, Any],
    *,
    home: Path | str | None = None,
) -> list[dict[str, Any]]:
    channels = dict(section.get("channels") or {})
    results: list[dict[str, Any]] = []
    for name in event.get("channels") or []:
        config = dict(channels.get(name) or {})
        sender = _SENDERS.get(str(name))
        if sender is None:
            results.append({"channel": name, "ok": False, "reason": "unknown_channel"})
            continue
        if config.get("enabled", True) is False:
            results.append({"channel": name, "ok": False, "reason": "disabled"})
            continue
        try:
            results.append(sender(event, config, home=home))
        except Exception as exc:  # noqa: BLE001 - a channel may not kill the loop
            results.append({"channel": name, "ok": False, "reason": type(exc).__name__})
    return results


__all__ = ["dispatch"]
