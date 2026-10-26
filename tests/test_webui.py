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

from stock_senate.webui.auth import (
    COOKIE_NAME,
    _safe_next,
    audit_log_path,
    audit_write,
    recent_writes,
)
from stock_senate.webui.launch import allowed_hosts, create_app, is_loopback, run
from stock_senate.webui.state import (
    MAX_ACTIVE_JOBS,
    MAX_PENDING_REVIEWS,
    AnalysisJob,
    JobLimitExceeded,
    PendingReview,
    WebRuntime,
)

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
    "/reviews",
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


def test_every_page_renders_in_loopback_trust_mode(config_dir: Path, app_home: Path) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    assert runtime.auth.login_required is False
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
        for path in PAGES:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code, response.text[:300])
        assert client.get("/healthz").json()["service"] == "stock-senate-webui"


def test_browser_hardening_rejects_foreign_hosts_and_cross_site_writes(
    config_dir: Path, app_home: Path
) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
        page = client.get("/")
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        csrf = _csrf(runtime, client)

        for headers in ({"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}):
            blocked = client.post(
                "/history/favorite", data={"run_id": "fake-run", "csrf": csrf}, headers=headers
            )
            assert blocked.status_code == 403
        assert runtime.favorites() == []

        assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_session_cookie_is_secure_behind_tls(config_dir: Path, app_home: Path) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)

    with TestClient(app, base_url="https://127.0.0.1", follow_redirects=False) as client:
        cookie = client.get("/").headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "; secure" in cookie


def test_allowed_hosts_can_be_extended_from_environment(
    app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKSENATE_WEBUI_ALLOWED_HOSTS", "console.example, 10.0.0.5")
    hosts = allowed_hosts()
    assert {"127.0.0.1", "localhost", "[::1]", "console.example", "10.0.0.5"} <= set(hosts)


def test_writes_require_csrf_and_reject_bad_input(config_dir: Path, app_home: Path) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
        client.get("/")
        csrf = _csrf(runtime, client)

        # No token, no write.
        response = client.post("/analysis", data={"code": "600519", "date": "2026-10-24"})
        assert response.status_code == 400

        assert client.post("/history/favorite", data={"run_id": "fake-run"}).status_code == 400
        assert runtime.favorites() == []

        # Favoriting is a real, audited write.
        response = client.post("/history/favorite", data={"run_id": "fake-run", "csrf": csrf})
        assert response.status_code == 303
        assert runtime.favorites() == ["fake-run"]
        writes = recent_writes(runtime.home)
        assert writes and writes[0]["action"] == "history.favorite"
        assert (
            json.loads(audit_log_path(runtime.home).read_text(encoding="utf-8").splitlines()[0])[
                "run_id"
            ]
            == "fake-run"
        )

        # Toggling off removes it again.
        assert (
            client.post("/history/favorite", data={"run_id": "fake-run", "csrf": csrf}).status_code
            == 303
        )
        assert runtime.favorites() == []

        assert (
            client.post(
                "/settings/roles", data={"name": "nope", "enabled": "0", "csrf": csrf}
            ).status_code
            == 400
        )
        assert client.post("/queue/submit", data={"csv": "", "csrf": csrf}).status_code == 400
        assert (
            client.post("/queue/cancel", data={"job_id": "missing", "csrf": csrf}).status_code
            == 303
        )
        assert (
            client.post(
                "/reviews/decide", data={"review_id": "x", "decision": "approve"}
            ).status_code
            == 400
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

    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
        response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        assert client.get("/api/progress/xyz").status_code == 401

        assert client.post("/login", data={"password": "wrong", "next": "/"}).status_code == 401
        # The MCP bearer token is not a WebUI password.
        assert client.post("/login", data={"password": MCP_TOKEN, "next": "/"}).status_code == 401

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


@pytest.mark.parametrize(
    "target",
    [
        "//evil.test/reports",
        "/\\evil.test",
        "\\\\evil.test",
        "%2F%2Fevil.test",
        "https://evil.test/reports",
        "/reports with space",
    ],
)
def test_login_next_never_leaves_the_origin(target: str) -> None:
    assert _safe_next(target) == "/"


def test_login_next_keeps_a_same_origin_path() -> None:
    assert _safe_next("/reports/2026-10-24-600519-abcdef") == "/reports/2026-10-24-600519-abcdef"
    assert _safe_next("/reviews?status=pending") == "/reviews?status=pending"


def test_audit_trail_rotates_and_reads_only_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("stock_senate.webui.auth.AUDIT_MAX_BYTES", 400)
    monkeypatch.setattr("stock_senate.webui.auth.AUDIT_TAIL_BYTES", 300)
    home = tmp_path / "home"

    for index in range(20):
        audit_write(home, "test.write", index=index, detail="x" * 80)

    path = audit_log_path(home)
    backup = path.with_name(path.name + ".1")
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8").strip()

    rows = recent_writes(home, limit=100)
    assert rows and rows[0]["index"] == 19
    indices = [row["index"] for row in rows]
    assert indices == list(range(19, 19 - len(indices), -1))
    assert recent_writes(home, limit=0) == []


def test_role_toggle_is_persisted_to_config(config_dir: Path, app_home: Path) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
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

    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
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


def _start_hitl_job(client: TestClient, csrf: str) -> str:
    response = client.post(
        "/analysis",
        data={"code": "600519", "date": "2026-10-24", "hitl": "1", "csrf": csrf},
    )
    assert response.status_code == 303
    return response.headers["location"].split("/progress/")[1].split("?")[0]


def _next_pending(runtime: WebRuntime, job_id: str, deadline: float) -> PendingReview | None:
    while time.time() < deadline:
        for review in runtime.reviews(limit=100):
            if review.job_id == job_id and review.status == "pending":
                return review
        time.sleep(0.05)
    return None


def _wait_job(client: TestClient, job_id: str, deadline: float) -> dict:
    snapshot: dict = {}
    while time.time() < deadline:
        snapshot = client.get(f"/api/progress/{job_id}").json()
        if snapshot.get("status") in {"done", "failed"}:
            break
        time.sleep(0.1)
    return snapshot


def test_reviews_page_approves_and_releases_the_run(
    config_dir: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
        client.get("/")
        csrf = _csrf(runtime, client)
        job_id = _start_hitl_job(client, csrf)

        deadline = time.time() + 180
        decisions = 0
        while time.time() < deadline:
            snapshot = client.get(f"/api/progress/{job_id}").json()
            if snapshot.get("status") in {"done", "failed"}:
                break
            review = _next_pending(runtime, job_id, min(deadline, time.time() + 1))
            if review is None:
                continue
            assert review.code == "600519"
            assert review.payload["research"]["stance"]
            assert review.payload["trade"]["direction"]
            listing = client.get("/reviews")
            assert listing.status_code == 200
            assert f"待复核（{len(runtime.pending_reviews())}）" in listing.text
            assert "600519" in listing.text
            assert 'action="/reviews/decide"' in listing.text
            assert 'value="reject"' in listing.text
            response = client.post(
                "/reviews/decide",
                data={
                    "review_id": review.review_id,
                    "decision": "approve",
                    "note": "测试同意",
                    "csrf": csrf,
                },
            )
            assert response.status_code == 303
            decisions += 1

        snapshot = _wait_job(client, job_id, deadline)
        assert snapshot["status"] == "done", snapshot
        assert snapshot["rating"]
        assert decisions == 2
        assert runtime.pending_reviews() == []
        approved = [review for review in runtime.reviews() if review.status == "approved"]
        assert {review.checkpoint for review in approved} == {"after_trader", "before_final"}
        assert all(review.note == "测试同意" for review in approved)
        actions = [row["action"] for row in recent_writes(runtime.home)]
        assert actions.count("review.decide") == 2


def test_reviews_rejection_blocks_the_final_rating(
    config_dir: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
        client.get("/")
        csrf = _csrf(runtime, client)
        job_id = _start_hitl_job(client, csrf)

        deadline = time.time() + 180
        first_id: str | None = None
        decisions = 0
        while time.time() < deadline:
            snapshot = client.get(f"/api/progress/{job_id}").json()
            if snapshot.get("status") in {"done", "failed"}:
                break
            review = _next_pending(runtime, job_id, min(deadline, time.time() + 1))
            if review is None:
                continue
            first_id = first_id or review.review_id
            response = client.post(
                "/reviews/decide",
                data={
                    "review_id": review.review_id,
                    "decision": "reject",
                    "note": "人工驳回",
                    "csrf": csrf,
                },
            )
            assert response.status_code == 303
            decisions += 1

        snapshot = _wait_job(client, job_id, deadline)
        assert snapshot["status"] == "done", snapshot
        assert snapshot["rating"] == "unknown"
        assert snapshot["low_quality"] is True
        assert decisions == 2
        assert {review.status for review in runtime.reviews()} == {"rejected"}

        missing = client.post(
            "/reviews/decide",
            data={"review_id": "missing", "decision": "approve", "csrf": csrf},
        )
        assert missing.status_code == 404
        assert first_id is not None
        repeat = client.post(
            "/reviews/decide",
            data={"review_id": first_id, "decision": "approve", "csrf": csrf},
        )
        assert repeat.status_code == 409


def test_failed_job_expires_its_pending_reviews(
    config_dir: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    job = AnalysisJob(job_id="job-fails", code="600519", date="2026-10-27")

    class _FailingGraph:
        def __init__(self, *, config: object) -> None:
            pass

        def propagate(self, code: str, date: str, **kwargs: object) -> None:
            runtime._open_review(job, "after_trader", {"research": {}, "trade": {}})
            raise RuntimeError("graph exploded")

    monkeypatch.setattr("stock_senate.graph.senate_graph.SenateGraph", _FailingGraph)

    runtime._run_job(job, True, [], [], [])

    assert job.status == "failed"
    assert "graph exploded" in (job.error or "")
    assert runtime.pending_reviews() == []
    assert {review.status for review in runtime.reviews()} == {"expired"}


def test_pending_review_queue_is_bounded_and_expires_the_oldest(
    config_dir: Path, app_home: Path
) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    job = AnalysisJob(job_id="job", code="600519", date="2026-10-27")
    reviews = [
        runtime._open_review(job, "after_trader", {}) for _ in range(MAX_PENDING_REVIEWS + 2)
    ]

    assert len(runtime.pending_reviews()) == MAX_PENDING_REVIEWS
    assert reviews[0].status == "expired"
    assert reviews[1].status == "expired"
    assert reviews[-1].status == "pending"


def test_start_analysis_refuses_beyond_the_active_limit(config_dir: Path, app_home: Path) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    for index in range(MAX_ACTIVE_JOBS):
        job = AnalysisJob(
            job_id=f"active-{index}", code="600519", date="2026-10-27", status="running"
        )
        runtime._jobs[job.job_id] = job

    with pytest.raises(JobLimitExceeded):
        runtime.start_analysis("600519", "2026-10-27", hitl=False)


def test_reviews_page_lists_pending_outside_the_recent_window(
    config_dir: Path, app_home: Path
) -> None:
    runtime = WebRuntime(config_dir=config_dir)
    job = AnalysisJob(job_id="job", code="600519", date="2026-10-27")
    waiting = runtime._open_review(job, "after_trader", {})
    waiting.created_at = "2026-01-01T00:00:00+00:00"
    for _ in range(60):
        decided = runtime._open_review(job, "before_final", {})
        decided.status = "approved"
        decided.created_at = "2026-02-01T00:00:00+00:00"

    app = create_app(runtime)
    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
        listing = client.get("/reviews")

    assert listing.status_code == 200
    assert "待复核（1）" in listing.text
    assert waiting.review_id in listing.text


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
