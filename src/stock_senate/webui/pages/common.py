"""Shared helpers for WebUI page modules."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from stock_senate.webui.auth import csrf_ok
from stock_senate.webui.state import WebRuntime


def runtime(request: Request) -> WebRuntime:
    return request.app.state.runtime


def redirect(path: str, *, notice: str | None = None) -> RedirectResponse:
    if notice:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}notice={quote(notice)}"
    return RedirectResponse(path, status_code=303)


def csrf_failure() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "csrf"}, status_code=400)


async def checked_form(request: Request) -> tuple[Any, Response | None]:
    """Parse a form and enforce the session CSRF token."""
    form = await request.form()
    if not csrf_ok(request, form):
        return form, csrf_failure()
    return form, None


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def split_csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


__all__ = ["as_bool", "checked_form", "csrf_failure", "redirect", "runtime", "split_csv"]
