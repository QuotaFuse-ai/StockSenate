"""Alert rules, state machine and webhook delivery.

Covers the acceptance items behind traceability rows 13/14: cooldown and
dedup, the daily cap, quiet hours with deferred release, recovery, and the
webhook contract (HMAC signature, idempotency key, bounded retries, secrets
never echoed). The rule language is parsed, never evaluated, so a rule file
cannot execute code.

Everything runs offline: the data side uses the ``local_json`` provider and
the webhook transport is stubbed, so no test needs a network or a model.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from stock_senate.alerts import engine
from stock_senate.alerts import rules as rulebook
from stock_senate.alerts.channels import dispatch, webhook
from stock_senate.alerts.state import AlertState, in_quiet_hours

CODE = "600519"
DAY = "2026-10-24"
MORNING = datetime(2026, 10, 26, 10, 0, 0)


# --------------------------------------------------------------------------- #
# state machine
# --------------------------------------------------------------------------- #


def test_dedup_uses_the_longer_of_cooldown_and_dedup_window(tmp_path: Path) -> None:
    state = AlertState(tmp_path / "state.json", dedup_hours=12)

    status, event = state.decide("price_limit", CODE, fired=True, event={"id": "e1"}, now=MORNING)
    assert status == "triggered"
    assert event == {"id": "e1"}

    # A shorter per-rule cooldown must not shorten the global dedup window.
    status, event = state.decide(
        "price_limit",
        CODE,
        fired=True,
        cooldown_minutes=1,
        event={"id": "e2"},
        now=MORNING + timedelta(minutes=2),
    )
    assert (status, event) == ("deduped", None)

    status, event = state.decide(
        "price_limit",
        CODE,
        fired=True,
        event={"id": "e3"},
        now=MORNING + timedelta(hours=12, seconds=1),
    )
    assert status == "confirmed"
    assert event == {"id": "e3"}


def test_state_survives_a_reload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    AlertState(path, dedup_hours=12).decide(
        "price_limit", CODE, fired=True, event={"id": "e1"}, now=MORNING
    )

    reloaded = AlertState(path, dedup_hours=12)
    status, event = reloaded.decide(
        "price_limit",
        CODE,
        fired=True,
        event={"id": "e2"},
        now=MORNING + timedelta(minutes=30),
    )
    assert (status, event) == ("deduped", None)
    assert reloaded.snapshot()["entries"]["price_limit:600519"]["count"] == 1


def test_daily_cap_suppresses_across_codes(tmp_path: Path) -> None:
    state = AlertState(tmp_path / "state.json", dedup_hours=0, max_triggers_per_day=2)

    assert state.decide("r", "600519", fired=True, now=MORNING)[0] == "triggered"
    assert (
        state.decide("r", "600036", fired=True, now=MORNING + timedelta(minutes=1))[0]
        == "triggered"
    )
    assert (
        state.decide("r", "000001", fired=True, now=MORNING + timedelta(minutes=2))[0]
        == "suppressed"
    )
    # The cap is per rule and per day: a fresh day starts over.
    assert (
        state.decide("r", "000001", fired=True, now=MORNING + timedelta(days=1))[0] == "triggered"
    )


def test_quiet_hours_defer_one_event_and_release_it(tmp_path: Path) -> None:
    state = AlertState(tmp_path / "state.json", dedup_hours=0, quiet_hours=["23:30-07:00"])
    quiet = datetime(2026, 10, 26, 23, 45)

    status, event = state.decide("r", CODE, fired=True, event={"id": "e1"}, now=quiet)
    assert (status, event) == ("quiet", None)
    # A second trigger while still quiet must not queue a duplicate.
    state.decide("r", CODE, fired=True, event={"id": "e1"}, now=quiet + timedelta(minutes=5))

    assert state.flush(quiet + timedelta(minutes=10)) == []

    released = state.flush(datetime(2026, 10, 27, 7, 0))
    assert [row["id"] for row in released] == ["e1"]
    assert released[0]["released_at"].startswith("2026-10-27T07:00")
    assert state.flush(datetime(2026, 10, 27, 7, 5)) == []
    assert AlertState(state.path).snapshot()["entries"]["r:600519"]["pending"] == []


def test_recovery_and_idle(tmp_path: Path) -> None:
    state = AlertState(tmp_path / "state.json", dedup_hours=0)

    assert state.decide("r", CODE, fired=True, now=MORNING)[0] == "triggered"
    status, event = state.decide("r", CODE, fired=False, now=MORNING + timedelta(hours=1))
    assert (status, event) == ("recovered", None)
    status, event = state.decide("r", CODE, fired=False, now=MORNING + timedelta(hours=2))
    assert (status, event) == ("idle", None)


def test_missing_data_is_unknown_not_guessed(tmp_path: Path) -> None:
    state = AlertState(tmp_path / "state.json")
    status, event = state.decide("r", CODE, fired=None, now=MORNING)
    assert (status, event) == ("unknown", None)
    assert "last_status" not in state.entries["r:600519"]


@pytest.mark.parametrize(
    ("spec", "moment", "expected"),
    [
        ("23:30-07:00", "2026-10-26T23:45", True),
        ("23:30-07:00", "2026-10-27T00:05", True),
        ("23:30-07:00", "2026-10-27T07:00", False),
        ("23:30-07:00", "2026-10-26T12:00", False),
        ("09:00-15:00", "2026-10-26T10:00", True),
        ("09:00-15:00", "2026-10-26T08:59", False),
        ("not-a-range", "2026-10-26T10:00", False),
    ],
)
def test_quiet_hour_windows(spec: str, moment: str, expected: bool) -> None:
    assert in_quiet_hours(datetime.fromisoformat(moment), [spec]) is expected


# --------------------------------------------------------------------------- #
# rule language
# --------------------------------------------------------------------------- #


def test_condition_parses_and_combines_as_three_valued_or() -> None:
    condition = rulebook.parse_condition("quote.change_pct >= 9.8 or kline.volume_ratio_5d >= 2")
    # One decided true wins even when the other side is undecidable.
    assert rulebook.evaluate(condition, {"quote.change_pct": 9.9})[0] is True
    # One undecidable clause with no true clause leaves the rule unknown.
    assert rulebook.evaluate(condition, {"quote.change_pct": 1.0})[0] is None
    assert rulebook.evaluate(condition, {})[0] is None
    assert (
        rulebook.evaluate(condition, {"quote.change_pct": 1.0, "kline.volume_ratio_5d": 1.2})[0]
        is False
    )


@pytest.mark.parametrize("text", ["", "   ", "rm -rf /", "__import__('os').system('x') == 1"])
def test_invalid_conditions_raise_instead_of_evaluating(text: str) -> None:
    with pytest.raises(ValueError, match="无法解析告警条件"):
        rulebook.parse_condition(text)


def test_referenced_domains_skips_envelope_and_invalid_rules() -> None:
    rules = [
        {"id": "a", "when": "quote.change_pct >= 9.8"},
        {"id": "b", "when": "kline.volume_ratio_5d >= 2 or quote.price > 10"},
        {"id": "c", "when": 'data_envelope.data_source_status == "missing"'},
        {"id": "d", "when": "not a condition"},
    ]
    assert rulebook.referenced_domains(rules) == ["quote", "kline"]


def test_dispatch_reports_unknown_and_disabled_channels() -> None:
    section = {"channels": {"console": {"enabled": False}}}
    assert dispatch({"channels": ["console", "nope"]}, section) == [
        {"channel": "console", "ok": False, "reason": "disabled"},
        {"channel": "nope", "ok": False, "reason": "unknown_channel"},
    ]


# --------------------------------------------------------------------------- #
# webhook channel
# --------------------------------------------------------------------------- #

EVENT = {"id": "evt-1", "rule": {"id": "price_limit"}, "code": CODE}
SECRET = "top-secret-value"


def _hook_config(**extra: object) -> dict:
    return {
        "url_env": "STOCKSENATE_TEST_HOOK_URL",
        "secret_env": "STOCKSENATE_TEST_HOOK_SECRET",
        **extra,
    }


@pytest.fixture()
def hook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCKSENATE_TEST_HOOK_URL", "https://alerts.example.test/hook")
    monkeypatch.setenv("STOCKSENATE_TEST_HOOK_SECRET", SECRET)


def test_signature_and_idempotency_key() -> None:
    body = b'{"a": 1}'
    expected = "sha256=" + hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert webhook.sign(SECRET, body) == expected
    assert webhook.idempotency_key(EVENT) == webhook.idempotency_key(dict(EVENT))
    assert webhook.idempotency_key({**EVENT, "code": "600036"}) != webhook.idempotency_key(EVENT)


def test_send_signs_and_never_leaks_the_secret(
    monkeypatch: pytest.MonkeyPatch, hook_env: None
) -> None:
    calls: list[tuple[str, bytes, dict[str, str]]] = []

    def fake_post(url: str, body: bytes, headers: dict[str, str]) -> int:
        calls.append((url, body, dict(headers)))
        return 200

    monkeypatch.setattr(webhook, "_post", fake_post)

    result = webhook.send(EVENT, _hook_config())

    assert result == {"channel": "webhook", "ok": True, "attempts": 1, "status": 200}
    url, body, headers = calls[0]
    assert url == "https://alerts.example.test/hook"
    assert headers["X-StockSenate-Signature"] == webhook.sign(SECRET, body)
    assert headers["X-StockSenate-Idempotency-Key"] == webhook.idempotency_key(EVENT)
    assert SECRET.encode("utf-8") not in body
    assert json.loads(body)["id"] == "evt-1"


def test_send_reports_missing_env_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STOCKSENATE_TEST_HOOK_URL", raising=False)
    monkeypatch.delenv("STOCKSENATE_TEST_HOOK_SECRET", raising=False)

    result = webhook.send(EVENT, _hook_config())

    assert result["ok"] is False
    assert result["reason"] == "missing_env"
    assert "STOCKSENATE_TEST_HOOK_URL" in result["detail"]


def test_send_rejects_a_non_http_url(monkeypatch: pytest.MonkeyPatch, hook_env: None) -> None:
    monkeypatch.setenv("STOCKSENATE_TEST_HOOK_URL", "ftp://alerts.example.test/hook")
    assert webhook.send(EVENT, _hook_config()) == {
        "channel": "webhook",
        "ok": False,
        "reason": "invalid_url",
    }


def test_send_retries_then_reports_failure(monkeypatch: pytest.MonkeyPatch, hook_env: None) -> None:
    attempts: list[str] = []

    def failing(url: str, body: bytes, headers: dict[str, str]) -> int:
        attempts.append(url)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(webhook, "_post", failing)

    result = webhook.send(EVENT, _hook_config(max_retries=2, backoff_seconds=[0]))

    assert result == {
        "channel": "webhook",
        "ok": False,
        "attempts": 3,
        "reason": "URLError",
    }
    assert len(attempts) == 3


def test_send_retries_after_an_http_error(monkeypatch: pytest.MonkeyPatch, hook_env: None) -> None:
    codes = iter([500, 200])

    def flaky(url: str, body: bytes, headers: dict[str, str]) -> int:
        code = next(codes)
        if code >= 400:
            raise urllib.error.HTTPError(url, code, "boom", {}, None)
        return code

    monkeypatch.setattr(webhook, "_post", flaky)

    result = webhook.send(EVENT, _hook_config(max_retries=1, backoff_seconds=[0]))

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert result["status"] == 200


# --------------------------------------------------------------------------- #
# engine integration (offline, local archive)
# --------------------------------------------------------------------------- #


def _write_quote(root: Path, code: str, *, change_pct: float) -> None:
    directory = root / "quote" / code
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "data": {"code": code, "change_pct": change_pct, "price": 1800.0},
        "data_as_of": DAY,
    }
    (directory / f"{DAY}.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


def _alert_config(root: Path) -> dict:
    return {
        "data_source": {
            "global": {"mode": "local", "allow_fallback": True, "forbid_llm_fill": True},
            "providers": {
                "local_json": {
                    "kind": "json_file",
                    "root": str(root),
                    "layout": "{domain}/{code}/{date}.json",
                    # The shipped provider declares kline/financial/...; this
                    # fixture overrides the domains too, or quote is skipped.
                    "domains": ["quote"],
                }
            },
            "routing": {"quote": ["local_json"]},
            "freshness": {"enforce_no_lookahead": True},
        },
        "alert_rules": {
            "global": {
                "enabled": True,
                "dedup_hours": 12,
                "quiet_hours": [],
                "max_triggers_per_day": 5,
            },
            "channels": {"console": {"enabled": True}},
            "rules": [
                {
                    "id": "limit_move",
                    "name": "涨跌停",
                    "level": "high",
                    "when": "quote.change_pct >= 9.8 or quote.change_pct <= -9.8",
                    "channels": ["console"],
                }
            ],
        },
    }


@pytest.fixture()
def alert_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.delenv("STOCKSENATE_DATA_MODE", raising=False)
    monkeypatch.delenv("STOCKSENATE_ALERT_CODES", raising=False)
    root = tmp_path / "local"
    _write_quote(root, CODE, change_pct=10.0)
    return _alert_config(root)


def test_dry_run_reports_would_fire_and_unknown(alert_config: dict, tmp_path: Path) -> None:
    result = engine.dry_run(
        "limit_move",
        config=alert_config,
        home=tmp_path / "home",
        codes=[CODE, "000001"],
        as_of=DAY,
        now=MORNING,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["triggered"] == 1
    assert result["unknown"] == 1
    by_code = {row["code"]: row for row in result["results"]}
    assert by_code[CODE]["status"] == "would_fire"
    assert by_code["000001"]["status"] == "unknown"
    assert result["data"][CODE]["status"] == "ok"
    # A dry run never writes state.
    assert not (tmp_path / "home" / "alerts" / "state.json").exists()
    assert "不下单" in result["disclaimer"]


def test_dry_run_rejects_an_unknown_rule(alert_config: dict, tmp_path: Path) -> None:
    result = engine.dry_run(
        "nope",
        config=alert_config,
        home=tmp_path / "home",
        codes=[CODE],
        as_of=DAY,
    )
    assert result["ok"] is False
    assert result["available"] == ["limit_move"]


def test_dry_run_marks_a_broken_condition_invalid(alert_config: dict, tmp_path: Path) -> None:
    alert_config["alert_rules"]["rules"][0]["when"] = "quote.change_pct ~ 9.8"
    result = engine.dry_run(
        "limit_move",
        config=alert_config,
        home=tmp_path / "home",
        codes=[CODE],
        as_of=DAY,
        now=MORNING,
    )
    assert [row["status"] for row in result["results"]] == ["invalid"]
    assert result["triggered"] == 0


def test_evaluate_once_delivers_then_dedups(alert_config: dict, tmp_path: Path, capsys) -> None:
    home = tmp_path / "home"

    first = engine.evaluate_once(
        config=alert_config, home=home, codes=[CODE], as_of=DAY, now=MORNING
    )
    assert first["statuses"] == {"triggered": 1}
    assert len(first["delivered"]) == 1
    delivered = first["delivered"][0]
    assert delivered["event"]["code"] == CODE
    assert delivered["event"]["status"] == "triggered"
    assert delivered["delivery"] == [{"channel": "console", "ok": True}]
    # The console channel writes to stderr, keeping stdout machine-readable.
    assert delivered["event"]["id"] in capsys.readouterr().err

    second = engine.evaluate_once(
        config=alert_config,
        home=home,
        codes=[CODE],
        as_of=DAY,
        now=MORNING + timedelta(minutes=5),
    )
    assert second["statuses"] == {"deduped": 1}
    assert second["delivered"] == []


def test_evaluate_once_respects_the_global_enable_switch(
    alert_config: dict, tmp_path: Path
) -> None:
    alert_config["alert_rules"]["global"]["enabled"] = False
    result = engine.evaluate_once(
        config=alert_config, home=tmp_path / "home", codes=[CODE], as_of=DAY
    )
    assert result == {
        "ok": True,
        "enabled": False,
        "as_of": DAY,
        "delivered": [],
        "statuses": {},
    }
