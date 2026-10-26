"""WebUI: routing, CSRF, auth separation, audited writes, offline run.

Ported from the manual smoke scripts and turned into assertions. Everything is
driven through Starlette's ``TestClient`` in-process, so no port is bound and no
browser is needed; the offline run uses ``STOCKSENATE_LLM_PROVIDER=offline``
with ``STOCKSENATE_DATA_MODE=local`` and therefore never reaches the network.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from stock_senate.webui.auth import COOKIE_NAME, audit_log_path, recent_writes
from stock_senate.webui.launch import create_app, is_loopback, run
from stock_senate.webui.state import WebRuntime

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "unit-test-password"
MCP_TOKEN = "mcp-only-token"

PAGES = (
    "/",
    "/reports",
    "/diff",
    "/backtest",
    "/alerts",
    "/queue",
    "/settings",
    "/history",
    "/ops",
    "/healthz",
)


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """A throwaway copy: role edits must never touch the repository config."""
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    return target


@pytest.fixture()
def app_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("STOCKSENATE_HOME", str(home))
    monkeypatch.delenv("STOCKSENATE_WEBUI_PASSWORD", raising=False)
    monkeypatch.delenv("STOCKSENATE_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.delenv("STOCKSENATE_DATA_MODE", raising=False)
    monkeypatch.delenv("STOCKSENATE_LLM_PROVIDER", raising=False)
    return home


def _csrf(runtime: WebRuntime, client: TestClient) -> str:
    session = runtime.auth.verify(client.cookies.get(COOKIE_NAME))
    assert session is not None
    return str(session["csrf"])


def test_every_page_renders_in_loopback_trust_mode(
    config_dir: Path, app_home: Path
) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    assert runtime.auth.login_required is False
    app = create_app(runtime)

    with TestClient(app, follow_redirects=False) as client:
        for path in PAGES:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code, response.text[:300])
        assert client.get("/healthz").json()["service"] == "stock-senate-webui"


def test_writes_require_csrf_and_reject_bad_input(
    config_dir: Path, app_home: Path
) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)

    with TestClient(app, follow_redirects=False) as client:
        client.get("/")
        csrf = _csrf(runtime, client)

        # No token, no write.
        response = client.post("/analysis", data={"code": "600519", "date": "2026-10-24"})
        assert response.status_code == 400

        assert client.post("/history/favorite", data={"run_id": "fake-run"}).status_code == 400
        assert runtime.favorites() == []

        # Favoriting is a real, audited write.
        response = client.post(
            "/history/favorite", data={"run_id": "fake-run", "csrf": csrf}
        )
        assert response.status_code == 303
        assert runtime.favorites() == ["fake-run"]
        writes = recent_writes(runtime.home)
        assert writes and writes[0]["action"] == "history.favorite"
        assert json.loads(audit_log_path(runtime.home).read_text(encoding="utf-8").splitlines()[0])["run_id"] == "fake-run"

        # Toggling off removes it again.
        assert (
            client.post("/history/favorite", data={"run_id": "fake-run", "csrf": csrf}).status_code
            == 303
        )
        assert runtime.favorites() == []

        assert (
            client.post("/settings/roles", data={"name": "nope", "enabled": "0", "csrf": csrf}).status_code
            == 400
        )
        assert client.post("/queue/submit", data={"csv": "", "csrf": csrf}).status_code == 400
        assert (
            client.post("/queue/cancel", data={"job_id": "missing", "csrf": csrf}).status_code
            == 303
        )
        assert client.get("/reports/bad!id").status_code in {400, 404}


def test_login_gate_and_token_separation(
    config_dir: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKSENATE_WEBUI_PASSWORD", PASSWORD)
    monkeypatch.setenv("STOCKSENATE_MCP_HTTP_TOKEN", MCP_TOKEN)
    runtime = WebRuntime(config_dir=config_dir)
    assert runtime.auth.login_required is True
    app = create_app(runtime)

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        assert client.get("/api/progress/xyz").status_code == 401

        assert client.post("/login", data={"password": "wrong", "next": "/"}).status_code == 401
        # The MCP bearer token is not a WebUI password.
        assert (
            client.post("/login", data={"password": MCP_TOKEN, "next": "/"}).status_code
            == 401
        )

        response = client.post("/login", data={"password": PASSWORD, "next": "/"})
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert COOKIE_NAME in client.cookies
        assert client.get("/").status_code == 200

        # An open redirect is ignored.
        response = client.post("/login", data={"password": PASSWORD, "next": "//evil.test"})
        assert response.headers["location"] == "/"

        csrf = _csrf(runtime, client)
        assert client.post("/logout", data={"csrf": csrf}).status_code == 303
        assert client.get("/").status_code == 303


def test_role_toggle_is_persisted_to_config(
    config_dir: Path, app_home: Path
) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)

    with TestClient(app, follow_redirects=False) as client:
        client.get("/")
        csrf = _csrf(runtime, client)

        response = client.post(
            "/settings/roles", data={"name": "social", "enabled": "0", "csrf": csrf}
        )
        assert response.status_code == 303
        runtime.reload_config()
        assert runtime.config["agent_roles"]["roles"]["social"]["enabled"] is False

        response = client.post(
            "/settings/roles", data={"name": "social", "enabled": "1", "csrf": csrf}
        )
        assert response.status_code == 303
        runtime.reload_config()
        assert runtime.config["agent_roles"]["roles"]["social"]["enabled"] is True


def test_offline_analysis_round_trip(
    config_dir: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)

    with TestClient(app, follow_redirects=False) as client:
        client.get("/")
        csrf = _csrf(runtime, client)

        response = client.post(
            "/analysis", data={"code": "600519", "date": "2026-10-24", "csrf": csrf}
        )
        assert response.status_code == 303
        job_id = response.headers["location"].split("/progress/")[1].split("?")[0]

        snapshot: dict = {}
        deadline = time.time() + 180
        while time.time() < deadline:
            snapshot = client.get(f"/api/progress/{job_id}").json()
            if snapshot.get("status") in {"done", "failed"}:
                break
            time.sleep(0.2)

        assert snapshot.get("status") == "done", snapshot
        assert snapshot.get("percent") == 100
        run_id = snapshot["run_id"]
        assert run_id

        report = client.get(f"/reports/{run_id}")
        assert report.status_code == 200
        assert "600519" in report.text

        markdown = client.get(f"/reports/{run_id}/markdown")
        assert markdown.status_code == 200
        assert "600519" in markdown.text

        # The run is visible on the history page too.
        assert run_id in client.get("/history").text


def test_non_loopback_bind_requires_a_password(
    app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert is_loopback("127.0.0.1")
    assert is_loopback("127.0.0.5")
    assert is_loopback("localhost")
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("192.168.1.5")

    with pytest.raises(ValueError, match="STOCKSENATE_WEBUI_PASSWORD"):
        run(host="0.0.0.0", port=8788)


def test_non_loopback_bind_is_allowed_with_a_password(
    app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    monkeypatch.setenv("STOCKSENATE_WEBUI_PASSWORD", PASSWORD)
    seen: dict = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda app, host, port, **kwargs: seen.update(host=host, port=port)
    )

    run(host="0.0.0.0", port=8799)

    assert seen == {"host": "0.0.0.0", "port": 8799}
