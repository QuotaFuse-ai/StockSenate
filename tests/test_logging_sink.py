"""Rotating file sink: opt-in, redacted, and never on stdout."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from stock_senate.observability.logging import (
    LOGGER_NAME,
    configure,
    configure_file_sink,
    configure_from_config,
    get_logger,
)


@pytest.fixture(autouse=True)
def _clean_logger() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    configure(force=True)
    logger.setLevel(logging.INFO)
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _file_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger(LOGGER_NAME).handlers
        if getattr(handler, "_stock_senate_sink", None) == "file"
    ]


def test_file_sink_writes_redacted_json_and_leaves_stdout_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "logs" / "stock_senate.jsonl"
    configure(force=True)  # bind stderr to the capture this test installed
    configure_file_sink(target)
    get_logger("test").info(
        "model call", extra={"api_key": "sk-abcdefghijkl", "detail": {"token": "Bearer abcdefghij"}}
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["message"] == "model call"
    assert payload["api_key"] == "***"
    assert payload["detail"]["token"] == "***"

    out, err = capsys.readouterr()
    assert out == ""  # stdout is reserved for machine output
    assert "model call" in err  # stderr keeps its copy


def test_configure_from_config_is_off_unless_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STOCKSENATE_LOG_FILE", raising=False)
    config = {"home": str(tmp_path), "workflow": {"logging": {"file_enabled": False}}}

    assert configure_from_config(config) is None
    assert _file_handlers() == []
    assert not list((tmp_path / "logs").glob("*.jsonl"))


def test_relative_path_resolves_against_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STOCKSENATE_LOG_FILE", raising=False)
    config = {
        "home": str(tmp_path),
        "workflow": {"logging": {"file_enabled": True, "file_path": "logs/run.jsonl"}},
    }

    path = configure_from_config(config)

    assert path == tmp_path / "logs" / "run.jsonl"
    get_logger("test").info("hello")
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["message"] == "hello"


def test_env_var_enables_the_sink_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "env.jsonl"
    monkeypatch.setenv("STOCKSENATE_LOG_FILE", str(target))

    path = configure_from_config({"workflow": {"logging": {"file_enabled": False}}})

    assert path == target
    get_logger("test").info("env driven")
    assert json.loads(target.read_text(encoding="utf-8").splitlines()[0])["message"] == "env driven"


def test_repeated_configuration_keeps_one_sink(tmp_path: Path) -> None:
    target = tmp_path / "app.log"

    configure_file_sink(target)
    configure_file_sink(target)

    assert len(_file_handlers()) == 1
