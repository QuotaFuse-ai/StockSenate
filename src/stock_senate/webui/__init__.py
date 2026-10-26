"""Standalone WebUI on port 8788 (DEC-003).

Built on Starlette + uvicorn — both arrive transitively with the ``mcp`` extra —
instead of Streamlit, so the console adds no new direct dependency. The session,
CSRF token and audit trail live in :mod:`stock_senate.webui.auth`; the login
password is ``STOCKSENATE_WEBUI_PASSWORD`` and is never shared with the MCP
bearer token.
"""

from stock_senate.webui.auth import WebAuth
from stock_senate.webui.launch import create_app, run
from stock_senate.webui.state import WebRuntime

__all__ = ["WebAuth", "WebRuntime", "create_app", "run"]
