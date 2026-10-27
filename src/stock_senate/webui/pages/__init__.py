"""WebUI page modules. Each module exposes a ``routes()`` factory."""

from __future__ import annotations

from typing import Any

from . import (
    alerts,
    analysis,
    backtest,
    diff,
    history,
    ops,
    progress,
    queue,
    reports,
    reviews,
    settings,
)

_MODULES = (
    analysis,
    progress,
    reports,
    diff,
    backtest,
    alerts,
    queue,
    reviews,
    settings,
    history,
    ops,
)


def all_routes() -> list[Any]:
    """Every page route in navigation order (auth routes are added separately)."""
    routes: list[Any] = []
    for module in _MODULES:
        routes.extend(module.routes())
    return routes


__all__ = ["all_routes"]
