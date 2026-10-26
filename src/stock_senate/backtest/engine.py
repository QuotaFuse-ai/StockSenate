"""Point-in-time backtest replay.

The replay answers one question: *if the deterministic research rules had run on
each past session using only the bars available that day, what would the
simulated long-only result have been?* It never reads a bar dated after the
decision date, never lets a model fill a gap, and never emits an order or a
price instruction for live use — every number here is a historical simulation
for review.

Execution model (documented in the delivery note):

* signal is evaluated at a session close and only then acted on;
* entry/exit happen at the next session's open (T+1), adjusted by ``slippage``;
* a position is held for ``window`` sessions unless a bearish signal exits it
  early, so windows never overlap;
* A-share ±10% price-limit days block the fill instead of assuming a price;
* size is a whole number of 100-share lots and the fee is charged per side.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from stock_senate.config.loader import ensure_home, load_config
from stock_senate.data.envelope import date_only, now_iso
from stock_senate.data.router import DataAccessLayer
from stock_senate.workflow.digest import build_digest
from stock_senate.workflow.offline import generate

from .models import Bar, Trade, write_tables

DISCLAIMER = (
    "回测推演：仅用历史行情做点时重放，输出为历史模拟成交与统计，"
    "不构成投资建议，也不提供可执行价位、止损位、目标价、仓位或交易指令。"
)

_LIMIT = 0.10
_LOT = 100
_WARMUP = 20
_BUY_RATINGS = {"buy", "overweight"}
_SELL_RATINGS = {"sell", "underweight"}


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #


def run(
    *,
    codes: Iterable[str],
    start: str,
    end: str,
    window: int = 5,
    slippage: float = 0.0,
    fee: float = 0.0,
    out_dir: Path | str | None = None,
    config: Mapping[str, Any] | str | Path | None = None,
    home: Path | str | None = None,
    capital: float = 100_000.0,
) -> dict[str, Any]:
    """Replay ``codes`` between ``start`` and ``end`` and persist the result."""
    targets = list(dict.fromkeys(str(code).strip() for code in codes if str(code).strip()))
    if not targets:
        raise ValueError("backtest 需要至少一个标的代码")
    start_day = _day(start)
    end_day = _day(end)
    if start_day > end_day:
        raise ValueError("--start 不能晚于 --end")
    if int(window) < 1:
        raise ValueError("--window 必须为正整数")
    if slippage < 0 or fee < 0:
        raise ValueError("--slippage 与 --fee 不能为负数")
    if slippage >= 0.2 or fee >= 0.2:
        raise ValueError("--slippage 与 --fee 必须是比例（0.01 表示 1%）")

    loaded = load_config(config)
    root = ensure_home(Path(home).expanduser() if home else None)
    bars_by_code, envelopes, data_info = _load_bars(targets, start_day, end_day, loaded, root)

    sleeve = float(capital) / len(targets)
    started_at = now_iso()
    sims: dict[str, dict[str, Any]] = {}
    for code in targets:
        bars = bars_by_code.get(code) or []
        if not bars:
            sims[code] = _empty_sim(code, sleeve)
            continue
        sims[code] = _simulate(
            code,
            bars,
            envelopes[code],
            start_day,
            end_day,
            window=int(window),
            slippage=float(slippage),
            fee=float(fee),
            capital=sleeve,
        )

    missing = [code for code in targets if not bars_by_code.get(code)]
    metrics = _metrics(sims, capital=float(capital), start=start_day, end=end_day)
    result: dict[str, Any] = {
        "mode": "backtest",
        "simulation": True,
        "run_key": _run_key(started_at, targets),
        "started_at": started_at,
        "start": start_day,
        "end": end_day,
        "window": int(window),
        "slippage": float(slippage),
        "fee": float(fee),
        "capital": float(capital),
        "codes": targets,
        "data": {
            **data_info,
            "missing_codes": missing,
        },
        "decisions": [row for code in targets for row in sims[code]["decisions"]],
        "trades": [row for code in targets for row in sims[code]["trades"]],
        "positions": [row for code in targets for row in sims[code]["positions"]],
        "metrics": metrics,
        "per_code": {code: sims[code]["summary"] for code in targets},
        "partial": bool(missing) or data_info["status"] != "ok",
        "disclaimer": DISCLAIMER,
    }

    artifacts = _persist(result, sims, root, Path(out_dir) if out_dir else None)
    result["artifacts"] = artifacts
    return result


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #


def _load_bars(
    codes: list[str],
    start: str,
    end: str,
    config: Mapping[str, Any],
    home: Path,
) -> tuple[dict[str, list[Bar]], dict[str, Any], dict[str, Any]]:
    """Fetch one kline series per code; never look past ``end``."""
    as_of = min(end, date_cls.today().isoformat())
    layer = DataAccessLayer(config, home=home, as_of=as_of)
    bars: dict[str, list[Bar]] = {}
    envelopes: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    statuses: list[str] = []

    for code in codes:
        envelope = layer.fetch(
            "kline",
            code,
            as_of=as_of,
            params={"lookback_days": 400, "limit": 320},
        )
        envelopes[code] = envelope
        status = envelope.data_source_status.value
        statuses.append(status)
        parsed = _parse_bars(envelope.payload) if envelope.usable else []
        parsed = [bar for bar in parsed if bar.date <= as_of]
        bars[code] = parsed
        rows.append(
            {
                "code": code,
                "status": status,
                "source": envelope.source,
                "data_as_of": envelope.data_as_of,
                "bars": len(parsed),
                "first_bar": parsed[0].date if parsed else None,
                "last_bar": parsed[-1].date if parsed else None,
                "detail": envelope.source_detail,
            }
        )

    if statuses and all(status == "missing" for status in statuses):
        overall = "missing"
    elif any(status != "ok" for status in statuses):
        overall = "degraded"
    else:
        overall = "ok"

    short_history = any(row["first_bar"] is None or row["first_bar"] > start for row in rows)
    info = {
        "status": overall,
        "as_of": as_of,
        "short_history": short_history,
        "codes": rows,
        "attempts": dict(layer.attempts),
    }
    return bars, envelopes, info


def _parse_bars(payload: Any) -> list[Bar]:
    if isinstance(payload, list):
        records: Any = payload
    elif isinstance(payload, Mapping):
        records = payload.get("klines") or payload.get("bars") or payload.get("items") or []
    else:
        return []
    if not isinstance(records, list):
        return []
    parsed: list[Bar] = []
    for row in records:
        if not isinstance(row, Mapping):
            continue
        day = date_only(row.get("date") or row.get("trade_date"))
        values = [_number(row.get(key)) for key in ("open", "high", "low", "close")]
        if not day or any(value is None for value in values):
            continue
        open_, high, low, close = (float(value) for value in values)  # type: ignore[arg-type]
        if min(open_, high, low, close) <= 0:
            continue
        volume = _number(row.get("volume")) or 0.0
        parsed.append(Bar(day, open_, high, low, close, volume))
    parsed.sort(key=lambda bar: bar.date)
    return list({bar.date: bar for bar in parsed}.values())


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #


def _simulate(
    code: str,
    bars: list[Bar],
    envelope: Any,
    start: str,
    end: str,
    *,
    window: int,
    slippage: float,
    fee: float,
    capital: float,
) -> dict[str, Any]:
    inside = [index for index, bar in enumerate(bars) if start <= bar.date <= end]
    if not inside:
        return _empty_sim(code, capital, reason="没有落在回测区间内的交易日")
    last_index = inside[-1]
    bars = bars[: last_index + 1]
    start_index = inside[0]

    cash = capital
    shares = 0
    entry: dict[str, Any] | None = None
    pending: str | None = None
    pending_reason = "window_end"
    trades: list[Trade] = []
    decisions: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    curve: list[tuple[str, float]] = []
    prices: list[dict[str, Any]] = []
    limit_up_skips = 0
    limit_down_defers = 0
    holding_days = 0
    simulated_days = 0

    for index, bar in enumerate(bars):
        previous_close = bars[index - 1].close if index else None

        if pending == "buy" and previous_close is not None:
            if bar.open >= previous_close * (1 + _LIMIT - 0.005):
                limit_up_skips += 1
                pending = None
            else:
                price = bar.open * (1 + slippage)
                lot_cost = price * (1 + fee)
                lots = int(cash / lot_cost / _LOT) if lot_cost > 0 else 0
                if lots >= 1:
                    bought = lots * _LOT
                    gross = price * bought
                    entry_fee = gross * fee
                    cash -= gross + entry_fee
                    shares = bought
                    entry = {
                        "index": index,
                        "date": bar.date,
                        "price": price,
                        "shares": bought,
                        "fee": entry_fee,
                        "reason": "signal",
                    }
                pending = None

        elif pending == "sell" and entry and previous_close is not None:
            if bar.open <= previous_close * (1 - _LIMIT + 0.005):
                limit_down_defers += 1
            else:
                price = bar.open * (1 - slippage)
                trades.append(
                    _close(code, entry, bar.date, price, shares, fee, reason=pending_reason)
                )
                cash += price * shares - price * shares * fee
                shares = 0
                entry = None
                pending = None

        if start_index <= index <= last_index:
            simulated_days += 1
            if shares:
                holding_days += 1
            prices.append({"code": code, **asdict(bar)})
            positions.append(
                {
                    "code": code,
                    "date": bar.date,
                    "shares": shares,
                    "cash": round(cash, 4),
                    "equity": round(cash + shares * bar.close, 4),
                }
            )
            curve.append((bar.date, cash + shares * bar.close))

            if index >= _WARMUP:
                signal = _decide(code, bars[: index + 1], envelope, bar.date)
                decisions.append(signal)
                if signal["signal"] > 0 and not shares and pending is None and index < last_index:
                    pending = "buy"
                elif signal["signal"] < 0 and shares and pending is None:
                    pending = "sell"
                    pending_reason = "bearish_signal"

            if (
                shares
                and entry
                and pending is None
                and index - entry["index"] + 1 >= window
                and index < last_index
            ):
                pending = "sell"
                pending_reason = "window_end"

    if shares and entry:
        last = bars[-1]
        price = last.close
        trades.append(_close(code, entry, last.date, price, shares, fee, reason="end_of_data"))
        cash += price * shares - price * shares * fee
        shares = 0
        entry = None
        curve[-1] = (last.date, cash)

    summary = _code_summary(
        code,
        bars[start_index:],
        decisions,
        trades,
        curve,
        capital=capital,
        final_equity=cash,
        holding_days=holding_days,
        simulated_days=simulated_days,
        limit_up_skips=limit_up_skips,
        limit_down_defers=limit_down_defers,
        window=window,
    )
    return {
        "decisions": decisions,
        "trades": [asdict(trade) for trade in trades],
        "positions": positions,
        "prices": prices,
        "curve": curve,
        "summary": summary,
    }


def _decide(code: str, bars: list[Bar], envelope: Any, as_of: str) -> dict[str, Any]:
    """Run the deterministic rules over the bars known at ``as_of``."""
    rows = [
        {
            "date": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]
    sliced = envelope.model_copy(
        update={
            "payload": {"code": code, "period": "daily", "klines": rows},
            "data_as_of": as_of,
        }
    )
    digest = build_digest({"kline": sliced}, as_of=as_of)
    context = {
        "digest": digest,
        "role_domains": ["kline"],
        "role": "market",
        "display_name": "market",
    }
    report = generate("AnalystReport", context)
    verdict = generate(
        "ResearchVerdict",
        {
            "analysts": [report],
            "weights": {"market": 1.0},
            "rounds_used": 1,
            "convergence_reason": "point_in_time_replay",
        },
    )
    trade = generate("TradePlan", {"verdict": verdict})
    risk = generate("RiskArgument", {"side": "neutral", "verdict": verdict, "trade": trade})
    decision = generate(
        "FinalDecision",
        {"verdict": verdict, "trade": trade, "risks": [risk], "hitl": {"status": "skipped"}},
    )
    rating = str(decision.get("rating") or "unknown")
    signal = 1 if rating in _BUY_RATINGS else -1 if rating in _SELL_RATINGS else 0
    return {
        "code": code,
        "date": as_of,
        "signal": signal,
        "source": "offline:market",
        "rating": rating,
        "stance": str(verdict.get("stance") or "unknown"),
        "confidence": float(verdict.get("confidence") or 0.0),
    }


def _close(
    code: str,
    entry: Mapping[str, Any],
    exit_date: str,
    exit_price: float,
    shares: int,
    fee: float,
    *,
    reason: str | None = None,
) -> Trade:
    gross = exit_price * shares
    exit_fee = gross * fee
    pnl = (exit_price - float(entry["price"])) * shares - float(entry["fee"]) - exit_fee
    basis = float(entry["price"]) * shares
    return Trade(
        code=code,
        entry_date=str(entry["date"]),
        entry_price=round(float(entry["price"]), 4),
        exit_date=exit_date,
        exit_price=round(exit_price, 4),
        shares=shares,
        fees=round(float(entry["fee"]) + exit_fee, 4),
        pnl=round(pnl, 4),
        return_pct=round(pnl / basis * 100, 4) if basis else 0.0,
        reason=reason or "window_end",
    )


def _empty_sim(code: str, capital: float, *, reason: str = "行情数据缺失") -> dict[str, Any]:
    return {
        "decisions": [],
        "trades": [],
        "positions": [],
        "prices": [],
        "curve": [],
        "summary": {
            "code": code,
            "status": "missing",
            "reason": reason,
            "capital": round(capital, 4),
            "final_equity": round(capital, 4),
            "total_return_pct": 0.0,
            "benchmark_return_pct": None,
            "alpha_pct": None,
            "trades": 0,
            "win_rate": None,
            "max_drawdown_pct": None,
            "direction_accuracy": None,
            "exposure_pct": 0.0,
            "limit_up_skips": 0,
            "limit_down_defers": 0,
            "no_lookahead": True,
        },
    }


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


def _code_summary(
    code: str,
    window_bars: list[Bar],
    decisions: list[dict[str, Any]],
    trades: list[Trade],
    curve: list[tuple[str, float]],
    *,
    capital: float,
    final_equity: float,
    holding_days: int,
    simulated_days: int,
    limit_up_skips: int,
    limit_down_defers: int,
    window: int,
) -> dict[str, Any]:
    benchmark = _benchmark(window_bars)
    total_return = (final_equity - capital) / capital * 100 if capital else 0.0
    wins = [trade for trade in trades if trade.pnl > 0]
    profits = sum(trade.pnl for trade in trades if trade.pnl > 0)
    losses = -sum(trade.pnl for trade in trades if trade.pnl < 0)
    forward = _forward_returns(decisions, window_bars, window)
    buy_returns = [ret for ret, signal in forward if signal > 0]
    sell_returns = [ret for ret, signal in forward if signal < 0]
    correct = [ret for ret, signal in forward if signal > 0 and ret > 0]
    bearish = [ret for ret, signal in forward if signal < 0 and ret < 0]
    directional = len(buy_returns) + len(sell_returns)
    return {
        "code": code,
        "status": "ok",
        "capital": round(capital, 4),
        "final_equity": round(final_equity, 4),
        "total_return_pct": round(total_return, 4),
        "benchmark_return_pct": _round(benchmark),
        "alpha_pct": _round(total_return - benchmark) if benchmark is not None else None,
        "trades": len(trades),
        "win_rate": _round(len(wins) / len(trades) * 100) if trades else None,
        "avg_trade_return_pct": _round(sum(trade.return_pct for trade in trades) / len(trades))
        if trades
        else None,
        "profit_factor": _round(profits / losses) if losses else (None if not profits else None),
        "max_drawdown_pct": _round(_max_drawdown(curve)),
        "direction_accuracy": _round((len(correct) + len(bearish)) / directional * 100)
        if directional
        else None,
        "buy_forward_return_pct": _round(sum(buy_returns) / len(buy_returns))
        if buy_returns
        else None,
        "sell_forward_return_pct": _round(sum(sell_returns) / len(sell_returns))
        if sell_returns
        else None,
        "signal_decay_pct": _round(
            (sum(buy_returns) / len(buy_returns) if buy_returns else 0.0)
            - (sum(sell_returns) / len(sell_returns) if sell_returns else 0.0)
        )
        if buy_returns or sell_returns
        else None,
        "avg_hold_days": _round(
            sum(_hold_days(trade, window_bars) for trade in trades) / len(trades)
        )
        if trades
        else None,
        "exposure_pct": _round(holding_days / simulated_days * 100) if simulated_days else 0.0,
        "limit_up_skips": limit_up_skips,
        "limit_down_defers": limit_down_defers,
        "bars": len(window_bars),
        "first_bar": window_bars[0].date if window_bars else None,
        "last_bar": window_bars[-1].date if window_bars else None,
        "no_lookahead": True,
        "t_plus_one": True,
        "min_lot": _LOT,
    }


def _metrics(
    sims: Mapping[str, Mapping[str, Any]],
    *,
    capital: float,
    start: str,
    end: str,
) -> dict[str, Any]:
    final_equity = sum(float(sim["summary"]["final_equity"]) for sim in sims.values())
    total_return = (final_equity - capital) / capital * 100 if capital else 0.0
    curve = _aggregate_curve(sims)
    trades = [trade for sim in sims.values() for trade in sim["trades"]]
    wins = [trade for trade in trades if trade["pnl"] > 0]
    profits = sum(trade["pnl"] for trade in trades if trade["pnl"] > 0)
    losses = -sum(trade["pnl"] for trade in trades if trade["pnl"] < 0)
    benchmarks = [
        float(sim["summary"]["benchmark_return_pct"])
        for sim in sims.values()
        if sim["summary"].get("benchmark_return_pct") is not None
    ]
    accuracies = [
        float(sim["summary"]["direction_accuracy"])
        for sim in sims.values()
        if sim["summary"].get("direction_accuracy") is not None
    ]
    return {
        "start": start,
        "end": end,
        "capital": round(capital, 4),
        "final_equity": round(final_equity, 4),
        "total_return_pct": round(total_return, 4),
        "benchmark_return_pct": _round(sum(benchmarks) / len(benchmarks)) if benchmarks else None,
        "alpha_pct": _round(total_return - sum(benchmarks) / len(benchmarks))
        if benchmarks
        else None,
        "max_drawdown_pct": _round(_max_drawdown(curve)),
        "sharpe": _round(_sharpe(curve)),
        "trade_count": len(trades),
        "win_rate": _round(len(wins) / len(trades) * 100) if trades else None,
        "avg_trade_return_pct": _round(sum(trade["return_pct"] for trade in trades) / len(trades))
        if trades
        else None,
        "profit_factor": _round(profits / losses) if losses else None,
        "direction_accuracy": _round(sum(accuracies) / len(accuracies)) if accuracies else None,
        "expectancy_pct": _round(sum(trade["return_pct"] for trade in trades) / len(trades))
        if trades
        else None,
        "non_overlapping_windows": True,
        "no_lookahead": True,
        "t_plus_one": True,
        "price_limit_pct": _LIMIT * 100,
        "min_lot": _LOT,
    }


def _benchmark(bars: list[Bar]) -> float | None:
    if len(bars) < 2:
        return None
    return (bars[-1].close / bars[0].close - 1) * 100


def _forward_returns(
    decisions: list[dict[str, Any]], bars: list[Bar], window: int
) -> list[tuple[float, int]]:
    by_date = {bar.date: index for index, bar in enumerate(bars)}
    returns: list[tuple[float, int]] = []
    for decision in decisions:
        signal = int(decision.get("signal") or 0)
        if not signal:
            continue
        index = by_date.get(str(decision.get("date")))
        if index is None or index + window >= len(bars):
            continue
        base = bars[index].close
        future = bars[index + window].close
        if base:
            returns.append(((future / base - 1) * 100, signal))
    return returns


def _hold_days(trade: Trade, bars: list[Bar]) -> int:
    dates = [bar.date for bar in bars]
    try:
        return max(0, dates.index(trade.exit_date) - dates.index(trade.entry_date))
    except ValueError:
        return 0


def _aggregate_curve(sims: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, float]]:
    series = {
        code: {day: value for day, value in sim.get("curve") or []} for code, sim in sims.items()
    }
    days = sorted({day for row in series.values() for day in row})
    latest: dict[str, float] = {}
    curve: list[tuple[str, float]] = []
    for day in days:
        for code, row in series.items():
            if day in row:
                latest[code] = float(row[day])
        curve.append((day, sum(latest.values())))
    return curve


def _max_drawdown(curve: list[tuple[str, float]]) -> float:
    peak = 0.0
    worst = 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        if peak:
            worst = min(worst, (equity / peak - 1) * 100)
    return abs(worst)


def _sharpe(curve: list[tuple[str, float]]) -> float:
    returns: list[float] = []
    for index in range(1, len(curve)):
        previous = curve[index - 1][1]
        if previous:
            returns.append(curve[index][1] / previous - 1)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    deviation = math.sqrt(variance)
    return (mean / deviation * math.sqrt(252)) if deviation else 0.0


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def _persist(
    result: dict[str, Any],
    sims: Mapping[str, Mapping[str, Any]],
    home: Path,
    out_dir: Path | None,
) -> dict[str, Any]:
    import json

    target = out_dir if out_dir is not None else home / "backtests" / result["run_key"]
    target.mkdir(parents=True, exist_ok=True)
    database = target / "backtest.sqlite"
    document = {
        "run_key": result["run_key"],
        "started_at": result["started_at"],
        "start": result["start"],
        "end": result["end"],
        "window": result["window"],
        "slippage": result["slippage"],
        "fee": result["fee"],
        "codes": result["codes"],
        "decisions": result["decisions"],
        "trades": result["trades"],
        "positions": result["positions"],
        "prices": [row for sim in sims.values() for row in sim["prices"]],
        "benchmark_rows": [
            {"code": row["code"], "date": row["date"], "close": row["close"]}
            for sim in sims.values()
            for row in sim["prices"]
        ],
        "metric_rows": list(result["metrics"].items()),
    }
    counts = write_tables(database, document)
    result_path = target / "result.json"
    artifacts = {
        "dir": str(target),
        "sqlite": str(database),
        "result": str(result_path),
        "tables": counts,
    }
    result["artifacts"] = artifacts
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return artifacts


def _run_key(started_at: str, codes: list[str]) -> str:
    stamp = started_at.replace("-", "").replace(":", "").replace("+", "T").split(".")[0]
    safe = "-".join(code.replace(os.sep, "_") for code in codes)
    return f"{stamp}-{safe}"


def _day(value: str) -> str:
    day = date_only(value)
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        raise ValueError(f"日期必须是 YYYY-MM-DD，收到 {value!r}")
    try:
        date_cls.fromisoformat(day)
    except ValueError as exc:
        raise ValueError(f"日期不合法：{value!r}") from exc
    return day


__all__ = ["DISCLAIMER", "run"]
