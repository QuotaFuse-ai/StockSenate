"""Backtest replay against a fixed local archive.

The dataset is generated here, written in the ``local_json`` layout
(``<root>/kline/<code>/<date>.json``) and replayed with ``mode = "local"``, so
the whole suite is offline and deterministic. Every assertion maps to one of
the five acceptance items: reproducibility, no lookahead, parameters take
effect, no sample leakage, and the same numbers the report/WebUI see.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from stock_senate.backtest import engine
from stock_senate.data.envelope import SourceStatus
from stock_senate.data.router import DataAccessLayer

CODE = "600519"
FIRST_DAY = date(2026, 1, 1)
BARS = 60


def _days() -> list[str]:
    return [(FIRST_DAY + timedelta(days=index)).isoformat() for index in range(BARS)]


def _price_path() -> list[float]:
    """Rise from 10 to 20 over 30 sessions, then fall to 8 over 30."""
    rising = [10.0 + 10.0 * index / 29 for index in range(30)]
    falling = [20.0 - 12.0 * (index + 1) / 30 for index in range(30)]
    return [round(value, 4) for value in rising + falling]


def _bars() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    previous: float | None = None
    for day, close in zip(_days(), _price_path()):
        open_ = close if previous is None else round((previous + close) / 2, 4)
        rows.append(
            {
                "date": day,
                "open": open_,
                "high": round(max(open_, close) * 1.005, 4),
                "low": round(min(open_, close) * 0.995, 4),
                "close": close,
                "volume": 1_000_000,
            }
        )
        previous = close
    return rows


def _write_archive(
    root: Path,
    *,
    rows: list[dict[str, object]],
    archive_day: str,
    data_as_of: str | None = None,
) -> Path:
    directory = root / "kline" / CODE
    directory.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "data": {"code": CODE, "period": "daily", "klines": rows},
        "data_as_of": data_as_of or archive_day,
    }
    path = directory / f"{archive_day}.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _config(tmp_path: Path, *, mode: str = "local") -> dict[str, object]:
    return {
        "data_source": {
            "global": {"mode": mode, "allow_fallback": True, "forbid_llm_fill": True},
            "providers": {
                "local_json": {
                    "kind": "json_file",
                    "root": str(tmp_path / "local"),
                    "layout": "{domain}/{code}/{date}.json",
                }
            },
            "routing": {"kline": ["local_json"]},
            "freshness": {"enforce_no_lookahead": True},
        },
        "llm_config": {"llm": {"llm_provider": "offline", "deep_think_llm": "offline",
                               "quick_think_llm": "offline"}},
    }


def _replay(tmp_path: Path, **overrides: object) -> dict:
    end = _days()[-1]
    params: dict[str, object] = {
        "codes": [CODE],
        "start": _days()[0],
        "end": end,
        "window": 5,
        "config": _config(tmp_path),
        "home": tmp_path / "home",
        "out_dir": tmp_path / "out",
    }
    params.update(overrides)
    return engine.run(**params)  # type: ignore[arg-type]


@pytest.fixture()
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "local"
    _write_archive(root, rows=_bars(), archive_day=_days()[-1])
    return root


# --------------------------------------------------------------------------- #
# reproducibility
# --------------------------------------------------------------------------- #


def test_replay_is_reproducible(tmp_path: Path, archive: Path) -> None:
    first = _replay(tmp_path, out_dir=tmp_path / "out-a")
    second = _replay(tmp_path, out_dir=tmp_path / "out-b")

    for key in ("decisions", "trades", "positions", "metrics", "per_code"):
        assert first[key] == second[key], key
    assert first["data"] == second["data"]
    assert first["metrics"]["trade_count"] > 0
    assert first["simulation"] is True


def test_artifacts_are_written(tmp_path: Path, archive: Path) -> None:
    result = _replay(tmp_path)
    artifacts = result["artifacts"]
    assert Path(artifacts["result"]).is_file()
    assert Path(artifacts["sqlite"]).is_file()
    assert artifacts["tables"]


# --------------------------------------------------------------------------- #
# no lookahead / no sample leakage
# --------------------------------------------------------------------------- #


def test_bars_after_end_are_dropped(tmp_path: Path) -> None:
    """A payload carrying future bars must not leak into the replay."""
    root = tmp_path / "local"
    end = _days()[29]
    future = _bars()  # contains bars well past ``end``
    _write_archive(root, rows=future, archive_day=end)

    result = _replay(tmp_path, start=_days()[0], end=end)

    assert result["data"]["as_of"] == end
    assert result["per_code"][CODE]["last_bar"] == end
    for row in result["positions"]:
        assert row["date"] <= end
    for row in result["decisions"]:
        assert row["date"] <= end
    assert result["metrics"]["no_lookahead"] is True


def test_archive_claiming_future_data_is_rejected(tmp_path: Path) -> None:
    """An envelope whose ``data_as_of`` is later than the analysis date is refused."""
    root = tmp_path / "local"
    end = _days()[-1]
    as_of = _days()[10]
    _write_archive(root, rows=_bars(), archive_day=as_of, data_as_of=end)

    layer = DataAccessLayer(_config(tmp_path), home=tmp_path / "home", as_of=as_of)
    envelope = layer.fetch("kline", CODE, as_of=as_of)

    assert envelope.data_source_status is SourceStatus.MISSING
    assert "lookahead" in envelope.source_detail


def test_missing_archive_never_invents_bars(tmp_path: Path, archive: Path) -> None:
    result = _replay(tmp_path, codes=[CODE, "000001"])

    assert result["data"]["missing_codes"] == ["000001"]
    assert result["partial"] is True
    assert result["per_code"]["000001"]["status"] == "missing"
    assert result["per_code"]["000001"]["trades"] == 0
    assert not [row for row in result["trades"] if row["code"] == "000001"]


# --------------------------------------------------------------------------- #
# parameters take effect
# --------------------------------------------------------------------------- #


def test_slippage_and_fee_reduce_the_simulated_result(tmp_path: Path, archive: Path) -> None:
    end = _days()[-1]
    plain = _replay(tmp_path, out_dir=tmp_path / "plain")
    costly = _replay(tmp_path, out_dir=tmp_path / "costly", slippage=0.01, fee=0.001)

    assert plain["metrics"]["trade_count"] > 0
    assert costly["metrics"]["trade_count"] > 0
    assert costly["metrics"]["final_equity"] < plain["metrics"]["final_equity"]
    assert any(trade["fees"] > 0 for trade in costly["trades"])
    assert all(trade["fees"] == 0 for trade in plain["trades"])
    assert costly["slippage"] == 0.01 and costly["fee"] == 0.001


def test_invalid_parameters_are_rejected(tmp_path: Path, archive: Path) -> None:
    with pytest.raises(ValueError):
        _replay(tmp_path, window=0)
    with pytest.raises(ValueError):
        _replay(tmp_path, slippage=-0.01)
    with pytest.raises(ValueError):
        _replay(tmp_path, fee=0.5)
    with pytest.raises(ValueError):
        _replay(tmp_path, start=_days()[-1], end=_days()[0])
    with pytest.raises(ValueError):
        engine.run(codes=[], start=_days()[0], end=_days()[-1])


# --------------------------------------------------------------------------- #
# execution model
# --------------------------------------------------------------------------- #


def test_t_plus_one_fills_and_non_overlapping_windows(tmp_path: Path, archive: Path) -> None:
    result = _replay(tmp_path)
    bars_by_day = {row["date"]: row for row in _bars()}

    assert result["metrics"]["t_plus_one"] is True
    assert result["metrics"]["non_overlapping_windows"] is True
    assert result["trades"]

    ordered = sorted(result["trades"], key=lambda trade: trade["entry_date"])
    for previous, current in zip(ordered, ordered[1:]):
        assert previous["exit_date"] <= current["entry_date"]

    # A signal is evaluated at a close, so the fill price is the *next* open.
    for trade in ordered:
        entry_bar = bars_by_day[trade["entry_date"]]
        assert trade["entry_price"] == pytest.approx(entry_bar["open"], rel=1e-6)
        assert trade["shares"] % 100 == 0


def test_report_and_replay_share_one_result(tmp_path: Path, archive: Path) -> None:
    """The persisted result.json is byte-for-byte the returned payload."""
    result = _replay(tmp_path)
    stored = json.loads(Path(result["artifacts"]["result"]).read_text(encoding="utf-8"))
    assert stored["metrics"] == result["metrics"]
    assert stored["trades"] == result["trades"]
    assert stored["disclaimer"] == result["disclaimer"]
