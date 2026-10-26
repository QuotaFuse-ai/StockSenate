"""F-01: a hostile code never becomes a path and never leaves the archive root."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_senate.data.envelope import SourceStatus
from stock_senate.data.providers import Provider, ProviderNotAvailable
from stock_senate.data.providers.base import envelope_from_payload
from stock_senate.data.providers.local_json import LocalJsonProvider
from stock_senate.data.router import DataAccessLayer
from stock_senate.graph.senate_graph import SenateGraph
from stock_senate.mcp_server import tools
from tests.cli_support import DAY, cli_env, run_cli

HOSTILE_CODES = (
    "../../etc/passwd",
    "..%2f..%2f",
    "/etc/passwd",
    r"C:\Windows\win.ini",
)


class CountingProvider(Provider):
    kind = "json_file"

    def __init__(self) -> None:
        super().__init__("counting", {"domains": ["kline"]})
        self.calls: list[str] = []

    def fetch(self, domain, code, as_of, params=None):  # noqa: ANN001 - provider contract
        self.calls.append(code)
        return envelope_from_payload(
            domain=domain,
            code=code,
            payload={"code": code},
            source=self.name,
            source_detail="counting fake",
            data_as_of=as_of,
        )


def _layer(provider: Provider) -> DataAccessLayer:
    return DataAccessLayer(
        {
            "global": {"mode": "priority", "forbid_llm_fill": True},
            "routing": {"kline": ["counting"]},
        },
        providers={"counting": provider},
        as_of=DAY,
    )


@pytest.mark.parametrize("payload", HOSTILE_CODES)
def test_router_returns_missing_without_touching_a_provider(payload: str) -> None:
    provider = CountingProvider()
    envelope = _layer(provider).fetch("kline", payload)

    assert envelope.data_source_status is SourceStatus.MISSING
    assert provider.calls == []
    assert "非法标的代码" in envelope.source_detail
    assert envelope.payload is None


def test_router_passes_the_normalized_code_to_providers() -> None:
    provider = CountingProvider()
    envelope = _layer(provider).fetch("kline", "sh600519")

    assert provider.calls == ["600519"]
    assert envelope.code == "600519"


@pytest.mark.parametrize("payload", HOSTILE_CODES)
def test_local_json_provider_cannot_read_outside_its_root(tmp_path: Path, payload: str) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "kline" / "600519" / f"{DAY}.json"
    outside.parent.mkdir(parents=True)
    outside.write_text(
        json.dumps({"data": {"code": "600519", "klines": []}, "data_as_of": DAY}),
        encoding="utf-8",
    )
    provider = LocalJsonProvider("local_json", {"root": str(root), "domains": ["kline"]})

    with pytest.raises(ProviderNotAvailable):
        provider.fetch("kline", payload, DAY)

    assert outside.is_file(), "fixture must stay outside the archive root"


def test_symlinked_archive_directory_that_escapes_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    (root / "kline").mkdir(parents=True)
    outside = tmp_path / "outside" / "600519"
    outside.mkdir(parents=True)
    (outside / f"{DAY}.json").write_text(
        json.dumps({"data": {"code": "600519", "klines": []}, "data_as_of": DAY}),
        encoding="utf-8",
    )
    try:
        (root / "kline" / "600519").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    provider = LocalJsonProvider("local_json", {"root": str(root), "domains": ["kline"]})
    with pytest.raises(ProviderNotAvailable):
        provider.fetch("kline", "600519", DAY)


def test_graph_rejects_a_hostile_code_before_any_data_access() -> None:
    graph = SenateGraph(config={})

    with pytest.raises(ValueError, match="合法的 A 股代码"):
        graph.propagate("../../etc/passwd", DAY)


def test_cli_analyze_rejects_a_hostile_code(tmp_path: Path) -> None:
    result = run_cli(
        "analyze",
        "../../etc/passwd",
        "--date",
        DAY,
        "--config",
        str(tmp_path),
        "--output",
        "json",
        env=cli_env(home=tmp_path / "home"),
    )

    assert result.returncode != 0
    assert "合法的 A 股代码" in result.stderr
    assert "passwd" not in result.stderr
    assert result.stdout.strip() == ""


def test_mcp_analysis_rejects_a_hostile_code() -> None:
    result = tools.analyse("../../etc/passwd", DAY, config={})

    assert result["ok"] is False
    assert result["error"]["kind"] == "invalid_request"
    assert "passwd" not in json.dumps(result, ensure_ascii=False)


def test_missing_envelope_keeps_the_requested_code_for_traceability() -> None:
    provider = CountingProvider()
    envelope = _layer(provider).fetch("kline", "..%2f..%2f")

    assert envelope.code == "..%2f..%2f"
    assert envelope.usable is False
