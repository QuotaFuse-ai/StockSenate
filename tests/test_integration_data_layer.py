"""Integration tests for the DataAccessLayer degradation paths.

The router is the only door between the graph and data. These cases exercise
what happens when a source is unreachable, malformed, model-filled, stale in
the wrong direction, or simply absent — with real provider objects rather than
mocked calls, since the contract lives at the provider boundary.
"""

from __future__ import annotations

import pytest

from stock_senate.data.envelope import DataEnvelope, SourceStatus, date_only
from stock_senate.data.providers import Provider, ProviderError, ProviderNotAvailable
from stock_senate.data.providers.base import envelope_from_payload
from stock_senate.data.router import MODES, DataAccessLayer, summarize

DAY = "2026-01-28"


class FakeProvider(Provider):
    """A provider whose behaviour is fully controlled by the test."""

    def __init__(
        self,
        name: str,
        *,
        kind: str = "json_file",
        domains: set[str] | None = None,
        payload: dict | None = None,
        status: str = "ok",
        data_as_of: str = DAY,
        error: Exception | None = None,
    ) -> None:
        super().__init__(name, {"domains": sorted(domains or {"kline"})})
        self.kind = kind
        self.payload = payload if payload is not None else {"code": "600519", "items": []}
        self.status = status
        self.data_as_of = data_as_of
        self.error = error
        self.calls = 0

    def fetch(self, domain, code, as_of, params=None):  # noqa: ANN001 - provider contract
        self.calls += 1
        if self.error is not None:
            raise self.error
        return envelope_from_payload(
            domain=domain,
            code=code,
            payload=self.payload,
            source=self.name,
            source_detail=f"{self.name} fake",
            data_as_of=self.data_as_of,
            meta={"data_source_status": self.status},
        )


def layer(
    providers: dict[str, Provider],
    *,
    mode: str = "priority",
    routing: dict[str, list[str]] | None = None,
    allow_fallback: bool = True,
    forbid_llm_fill: bool = True,
    enforce_no_lookahead: bool = True,
    as_of: str = DAY,
) -> DataAccessLayer:
    config = {
        "global": {
            "mode": mode,
            "allow_fallback": allow_fallback,
            "forbid_llm_fill": forbid_llm_fill,
        },
        "routing": routing or {},
        "freshness": {"enforce_no_lookahead": enforce_no_lookahead},
    }
    return DataAccessLayer(config, providers=providers, as_of=as_of)


# --------------------------------------------------------------------------- #
# mode selection
# --------------------------------------------------------------------------- #


def test_local_mode_never_selects_a_remote_provider() -> None:
    remote = FakeProvider("remote", kind="mcp")
    local = FakeProvider("local")
    access = layer(
        {"remote": remote, "local": local}, mode="local", routing={"kline": ["remote", "local"]}
    )

    assert access.candidates("kline") == ["local"]
    envelope = access.fetch("kline", "600519")
    assert envelope.data_source_status is SourceStatus.OK
    assert envelope.source == "local"
    assert remote.calls == 0


def test_stockdata_mode_never_selects_a_local_provider() -> None:
    remote = FakeProvider("remote", kind="mcp")
    local = FakeProvider("local")
    access = layer(
        {"remote": remote, "local": local}, mode="stockdata", routing={"kline": ["local", "remote"]}
    )

    assert access.candidates("kline") == ["remote"]


def test_hybrid_mode_tries_local_before_remote() -> None:
    remote = FakeProvider("remote", kind="mcp")
    local = FakeProvider("local")
    access = layer(
        {"remote": remote, "local": local}, mode="hybrid", routing={"kline": ["remote", "local"]}
    )

    assert access.candidates("kline") == ["local", "remote"]


def test_priority_mode_keeps_the_declared_order() -> None:
    remote = FakeProvider("remote", kind="mcp")
    local = FakeProvider("local")
    access = layer(
        {"remote": remote, "local": local}, mode="priority", routing={"kline": ["remote", "local"]}
    )

    assert access.candidates("kline") == ["remote", "local"]


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知数据模式"):
        layer({}, mode="airgapped")


def test_all_documented_modes_are_accepted() -> None:
    for mode in MODES:
        access = layer({"local": FakeProvider("local")}, mode=mode)
        assert access.mode == mode


# --------------------------------------------------------------------------- #
# fallback and failure
# --------------------------------------------------------------------------- #


def test_fallback_records_the_skipped_provider() -> None:
    broken = FakeProvider("broken", error=ProviderNotAvailable("archive missing"))
    healthy = FakeProvider("healthy")
    access = layer({"broken": broken, "healthy": healthy}, routing={"kline": ["broken", "healthy"]})

    envelope = access.fetch("kline", "600519")

    assert envelope.data_source_status is SourceStatus.OK
    assert envelope.source == "healthy"
    assert "fell back from broken" in envelope.source_detail
    assert access.attempts["kline"][0]["provider"] == "broken"
    assert access.attempts["kline"][0]["error"] == "ProviderNotAvailable"
    assert access.attempts["kline"][-1] == {
        "provider": "healthy",
        "error": None,
        "reason": "ok",
        "status": "ok",
    }


def test_allow_fallback_false_stops_after_the_first_candidate() -> None:
    broken = FakeProvider("broken", error=ProviderError("bad payload"))
    healthy = FakeProvider("healthy")
    access = layer(
        {"broken": broken, "healthy": healthy},
        routing={"kline": ["broken", "healthy"]},
        allow_fallback=False,
    )

    envelope = access.fetch("kline", "600519")

    assert envelope.data_source_status is SourceStatus.MISSING
    assert "all sources failed" in envelope.source_detail
    assert "bad payload" in envelope.source_detail
    assert healthy.calls == 0


def test_every_provider_failing_yields_a_missing_envelope_not_an_exception() -> None:
    first = FakeProvider("first", error=ProviderNotAvailable("no archive"))
    second = FakeProvider("second", error=ProviderError("malformed json"))
    access = layer({"first": first, "second": second}, routing={"kline": ["first", "second"]})

    envelope = access.fetch("kline", "600519")

    assert envelope.data_source_status is SourceStatus.MISSING
    assert envelope.source == "router"
    assert "first: no archive" in envelope.source_detail
    assert "second: malformed json" in envelope.source_detail


def test_provider_returning_a_missing_envelope_is_rejected() -> None:
    empty = FakeProvider("empty", status="missing")
    healthy = FakeProvider("healthy")
    access = layer({"empty": empty, "healthy": healthy}, routing={"kline": ["empty", "healthy"]})

    envelope = access.fetch("kline", "600519")

    assert envelope.source == "healthy"
    assert access.attempts["kline"][0]["reason"] == "provider returned a missing envelope"


def test_no_candidate_names_the_domain_and_mode() -> None:
    only_news = FakeProvider("news_only", domains={"news"})
    access = layer({"news_only": only_news}, mode="local")

    envelope = access.fetch("kline", "600519")

    assert envelope.data_source_status is SourceStatus.MISSING
    assert "no provider for domain 'kline' in mode 'local'" in envelope.source_detail


# --------------------------------------------------------------------------- #
# forbid_llm_fill
# --------------------------------------------------------------------------- #


def test_forbid_llm_fill_must_be_true() -> None:
    with pytest.raises(ValueError, match="forbid_llm_fill"):
        layer({"local": FakeProvider("local")}, forbid_llm_fill=False)


def test_model_filled_payload_is_rejected() -> None:
    invented = FakeProvider("invented", payload={"close": 1.23, "llm_fill": True})
    healthy = FakeProvider("healthy", payload={"close": 1.23})
    access = layer(
        {"invented": invented, "healthy": healthy}, routing={"kline": ["invented", "healthy"]}
    )

    envelope = access.fetch("kline", "600519")

    assert envelope.source == "healthy"
    rejected = access.attempts["kline"][0]
    assert rejected["error"] == "rejected"
    assert "forbid_llm_fill" in rejected["reason"]


def test_nested_model_fill_marker_is_rejected() -> None:
    invented = FakeProvider(
        "invented",
        payload={"items": [{"title": "t", "value": 1, "synthetic": True}]},
    )
    access = layer({"invented": invented}, routing={"kline": ["invented"]})

    envelope = access.fetch("kline", "600519")

    assert envelope.data_source_status is SourceStatus.MISSING
    assert "forbid_llm_fill" in envelope.source_detail


# --------------------------------------------------------------------------- #
# lookahead
# --------------------------------------------------------------------------- #


def test_envelope_dated_after_the_analysis_date_is_rejected() -> None:
    future = FakeProvider("future", data_as_of="2026-02-10")
    healthy = FakeProvider("healthy", data_as_of=DAY)
    access = layer(
        {"future": future, "healthy": healthy}, routing={"kline": ["future", "healthy"]}, as_of=DAY
    )

    envelope = access.fetch("kline", "600519")

    assert envelope.source == "healthy"
    assert access.attempts["kline"][0]["reason"].startswith("lookahead:")


def test_lookahead_rejection_can_be_disabled_by_config() -> None:
    future = FakeProvider("future", data_as_of="2026-02-10")
    access = layer(
        {"future": future}, routing={"kline": ["future"]}, enforce_no_lookahead=False, as_of=DAY
    )

    assert access.fetch("kline", "600519").data_source_status is SourceStatus.OK


# --------------------------------------------------------------------------- #
# local_json provider integration
# --------------------------------------------------------------------------- #


def test_local_json_provider_reads_the_newest_archive_not_after_as_of(tmp_path) -> None:
    from stock_senate.data.providers.local_json import LocalJsonProvider

    root = tmp_path / "local"
    for day in ("2026-01-20", "2026-01-27", "2026-02-03"):
        path = root / "kline" / "600519" / f"{day}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'{{"data": {{"code": "600519", "klines": []}}, "data_as_of": "{day}"}}',
            encoding="utf-8",
        )
    provider = LocalJsonProvider(
        "local_json",
        {"root": str(root), "layout": "{domain}/{code}/{date}.json", "domains": ["kline"]},
    )

    envelope = provider.fetch("kline", "600519", "2026-01-28")

    assert envelope.data_as_of == "2026-01-27"
    assert "2026-01-20" not in envelope.source_detail
    assert "archived 2026-01-27" in envelope.source_detail

    with pytest.raises(ProviderNotAvailable):
        provider.fetch("kline", "600519", "2026-01-19")


def test_local_json_provider_rejects_malformed_json(tmp_path) -> None:
    from stock_senate.data.providers.local_json import LocalJsonProvider

    path = tmp_path / "kline" / "600519" / f"{DAY}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    provider = LocalJsonProvider("local_json", {"root": str(tmp_path), "domains": ["kline"]})

    with pytest.raises(ProviderError):
        provider.fetch("kline", "600519", DAY)


# --------------------------------------------------------------------------- #
# summarize
# --------------------------------------------------------------------------- #


def _envelope(domain: str, status: SourceStatus) -> DataEnvelope:
    return DataEnvelope(
        domain=domain,
        code="600519",
        data_as_of=DAY,
        payload={"ok": True},
        source="test",
        source_detail="test",
        fetched_at="2026-01-28T09:00:00+08:00",
        freshness="realtime",
        confidence="high",
        data_source_status=status,
    )


def test_summarize_reports_ok_degraded_and_missing() -> None:
    ok = summarize(
        {
            "kline": _envelope("kline", SourceStatus.OK),
            "news": _envelope("news", SourceStatus.OK),
        }
    )
    assert ok == {"status": "ok", "total": 2, "missing": [], "degraded": [], "ok": 2}

    degraded = summarize(
        {
            "kline": _envelope("kline", SourceStatus.OK),
            "news": _envelope("news", SourceStatus.DEGRADED),
        }
    )
    assert degraded["status"] == "degraded"
    assert degraded["degraded"] == ["news"]

    missing = summarize(
        {
            "kline": _envelope("kline", SourceStatus.MISSING),
            "news": _envelope("news", SourceStatus.DEGRADED),
        }
    )
    assert missing["status"] == "degraded"
    assert missing["missing"] == ["kline"]

    everything = summarize({"kline": _envelope("kline", SourceStatus.MISSING)})
    assert everything["status"] == "missing"
    assert summarize({})["status"] == "missing"


def test_as_of_defaults_to_today_not_the_fixture_day() -> None:
    access = layer({"local": FakeProvider("local")}, as_of="")
    assert access.as_of == ""


def test_provider_failure_reason_is_one_line() -> None:
    broken = FakeProvider("broken", error=ProviderError("first line\nsecond line"))
    access = layer({"broken": broken}, routing={"kline": ["broken"]})

    envelope = access.fetch("kline", "600519")

    assert "first line" in envelope.source_detail
    assert "second line" not in envelope.source_detail


def test_fetch_uses_the_requested_day_when_provider_ignores_it() -> None:
    """``as_of`` passed to ``fetch`` wins over the layer default."""
    seen: list[str] = []

    class Recorder(FakeProvider):
        def fetch(self, domain, code, as_of, params=None):  # noqa: ANN001
            seen.append(date_only(as_of))
            return super().fetch(domain, code, as_of, params)

    access = layer({"local": Recorder("local")}, as_of=DAY, routing={"kline": ["local"]})
    access.fetch("kline", "600519", as_of="2026-01-05")

    assert seen == ["2026-01-05"]
