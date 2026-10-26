"""WebUI authentication, CSRF protection and the audited-write trail.

The WebUI session is deliberately independent from the MCP bearer token:
``STOCKSENATE_WEBUI_PASSWORD`` is the only thing that unlocks a session, and an
``STOCKSENATE_MCP_HTTP_TOKEN`` value is never accepted as a password. When no
password is configured the service runs in loopback-only trust mode and says so
on every page.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from stock_senate.config.loader import env
from stock_senate.observability.logging import audit
from stock_senate.webui.templates import csrf_input, esc, page

COOKIE_NAME = "ssenate_webui"
SESSION_TTL_SECONDS = 12 * 3600
PUBLIC_PATHS = frozenset({"/login", "/healthz", "/favicon.ico"})
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_FAILURES = 5


class WebAuth:
    """Signed-cookie sessions without an external session dependency."""

    def __init__(self, password: str | None = None) -> None:
        self.password = password or None
        self.login_required = bool(self.password)
        seed = self.password.encode("utf-8") if self.password else secrets.token_bytes(32)
        self._key = hashlib.sha256(b"stocksenate-webui-session:" + seed).digest()
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # -- password ---------------------------------------------------------

    def check_password(self, value: str | None) -> bool:
        if not self.login_required:
            return True
        if not value:
            return False
        return hmac.compare_digest(value.encode("utf-8"), self.password.encode("utf-8"))

    def throttled(self, client: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [stamp for stamp in self._failures.get(client, []) if now - stamp < _LOGIN_WINDOW_SECONDS]
            self._failures[client] = recent
            return len(recent) >= _LOGIN_MAX_FAILURES

    def note_failure(self, client: str) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(time.time())

    def clear_failures(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    # -- session ----------------------------------------------------------

    def _sign(self, blob: str) -> str:
        return hmac.new(self._key, blob.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self) -> tuple[str, dict[str, Any]]:
        session = {
            "exp": int(time.time()) + SESSION_TTL_SECONDS,
            "csrf": secrets.token_urlsafe(24),
            "nonce": secrets.token_hex(8),
        }
        raw = json.dumps(session, separators=(",", ":"), sort_keys=True).encode("utf-8")
        blob = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return f"{blob}.{self._sign(blob)}", session

    def verify(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        blob, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(blob)):
            return None
        try:
            padded = blob + "=" * (-len(blob) % 4)
            session = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(session, dict):
            return None
        try:
            expired = int(session.get("exp", 0)) < int(time.time())
        except (TypeError, ValueError):
            return None
        return None if expired else session


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def csrf_ok(request: Request, form: Any) -> bool:
    session = getattr(request.state, "session", None) or {}
    supplied = form.get("csrf") if form is not None else None
    expected = session.get("csrf")
    if not supplied or not expected:
        return False
    return hmac.compare_digest(str(supplied), str(expected))


class SessionMiddleware(BaseHTTPMiddleware):
    """Reject anonymous requests and attach the signed session to the scope."""

    def __init__(self, app: Any, auth: WebAuth) -> None:
        super().__init__(app)
        self.auth = auth

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        session = self.auth.verify(request.cookies.get(COOKIE_NAME))
        issued: str | None = None
        if session is None:
            if self.auth.login_required:
                if path.startswith("/api/"):
                    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
                target = path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
            issued, session = self.auth.issue()

        request.state.session = session
        request.state.csrf = session.get("csrf", "")
        request.state.login_required = self.auth.login_required
        response = await call_next(request)
        if issued:
            set_session_cookie(response, issued)
        return response


# --------------------------------------------------------------------------- #
# audited writes
# --------------------------------------------------------------------------- #


def audit_log_path(home: Path | str) -> Path:
    return Path(home) / "webui" / "audit.jsonl"


def audit_write(home: Path | str, action: str, **fields: Any) -> dict[str, Any]:
    """Append one audited write both to the structured log and the WebUI trail."""
    from stock_senate.observability.logging import redact

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": "webui.write",
        "actor": "webui",
        "action": action,
        **redact(fields),
    }
    path = audit_log_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    audit("webui.write", action=action, **fields)
    return payload


def recent_writes(home: Path | str, limit: int = 20) -> list[dict[str, Any]]:
    path = audit_log_path(home)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(rows[-limit:]))


# --------------------------------------------------------------------------- #
# login / logout routes
# --------------------------------------------------------------------------- #


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _login_page(request: Request, *, error: str | None = None, next_path: str = "/") -> HTMLResponse:
    body = f"""
<div class="panel" style="max-width:420px;margin:0 auto">
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{esc(next_path)}">
    <label for="password">登录口令（STOCKSENATE_WEBUI_PASSWORD）</label>
    <input id="password" type="password" name="password" autocomplete="current-password" autofocus>
    <p><button type="submit">登录</button></p>
  </form>
  <p class="muted">WebUI 口令与 MCP token 不共用；口令只从环境变量读取，不落盘、不回显。</p>
</div>"""
    return HTMLResponse(
        str(page(request, "登录", body, error=error, chrome=False)),
        status_code=401 if error else 200,
    )


async def login_view(request: Request) -> Response:
    auth: WebAuth = request.app.state.runtime.auth
    next_path = _safe_next(request.query_params.get("next"))
    if not auth.login_required:
        return RedirectResponse(next_path, status_code=303)
    if auth.verify(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(next_path, status_code=303)
    return _login_page(request, next_path=next_path)


async def login_submit(request: Request) -> Response:
    runtime = request.app.state.runtime
    auth: WebAuth = runtime.auth
    form = await request.form()
    client = _client(request)
    next_path = _safe_next(str(form.get("next") or "/"))
    if auth.throttled(client):
        audit("webui.login", outcome="throttled", client=client)
        return _login_page(request, error="尝试过于频繁，请稍后再试。", next_path=next_path)
    if not auth.check_password(str(form.get("password") or "")):
        auth.note_failure(client)
        audit("webui.login", outcome="failed", client=client)
        return _login_page(request, error="口令不正确。", next_path=next_path)
    auth.clear_failures(client)
    audit("webui.login", outcome="ok", client=client)
    token, _ = auth.issue()
    response = RedirectResponse(next_path, status_code=303)
    set_session_cookie(response, token)
    return response


async def logout_view(request: Request) -> Response:
    form = await request.form()
    if not csrf_ok(request, form):
        return JSONResponse({"ok": False, "error": "csrf"}, status_code=400)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


async def healthz(_: Request) -> Response:
    from stock_senate.version import __version__

    return JSONResponse({"ok": True, "service": "stock-senate-webui", "version": __version__})


def auth_routes() -> list[Route]:
    return [
        Route("/login", login_view, methods=["GET"]),
        Route("/login", login_submit, methods=["POST"]),
        Route("/logout", logout_view, methods=["POST"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/favicon.ico", lambda _: Response(status_code=204), methods=["GET"]),
    ]


__all__ = [
    "COOKIE_NAME",
    "PUBLIC_PATHS",
    "SessionMiddleware",
    "WebAuth",
    "audit_log_path",
    "audit_write",
    "auth_routes",
    "clear_session_cookie",
    "csrf_input",
    "csrf_ok",
    "recent_writes",
    "set_session_cookie",
]
